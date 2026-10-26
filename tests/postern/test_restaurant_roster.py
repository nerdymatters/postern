"""Restaurant membership changes grouping, never identity or the role policy."""
from types import SimpleNamespace

import pytest

from conftest import ADMIN, READER, sign_in
from postern.auth import parse_allowlist
from postern.config import Config
from postern.db import event, meta
from postern.storage import GoogleStore, StoreUnavailable
from test_training_progress import seed_item


def sheet(*rows):
    return [['Name', 'Email ', 'Location', 'Role'], *rows]


def test_one_identity_across_tabs_and_pending_contacts_have_no_grant():
    warnings = []
    members = parse_allowlist({
        'Team One': sheet(['Owner', 'Person.One@gmail.com', '', 'Admin']),
        'Restaurant': sheet(['Owner', 'personone+work@googlemail.com', '', 'Admin'],
                            ['Same person', 'personone@gmail.com', '', 'Admin'],
                            ['Pending contact', '', 'Restaurant']),
        'Empty': sheet(),
    }, warnings=warnings)
    assert members == {'personone@gmail.com': {'email': 'personone@gmail.com', 'name': 'Owner',
                       'role': 'admin', 'groups': ['Team One', 'Restaurant']}}
    assert warnings == [{'group': 'Restaurant', 'row': 4, 'message': 'Missing email address'}]


@pytest.mark.parametrize('bad,complaint', [
    (sheet(['Other role', READER, '', 'Admin']), 'conflicting roles across worksheets'),
    (sheet(['Typo', 'not-an-email', '', 'Staff']), 'Row 2: enter a valid email'),
    ([['Name', 'Address'], ['Person', READER]], 'exactly one'),
    ([], 'header row'),
])
def test_bad_tab_invalidates_the_whole_snapshot_with_location(bad, complaint):
    with pytest.raises(ValueError, match='Restaurant: .*' + complaint):
        parse_allowlist({'Team One': sheet(['Reader', READER, '', 'Staff']), 'Restaurant': bad})


def test_refresh_keeps_history_and_revokes_only_when_removed_from_every_tab(client, store, service):
    admin = sign_in(client, ADMIN)
    reader = sign_in(client, READER)
    post, item = seed_item(service, pages=2)
    service.db.submit(lambda db: event(db, 'page_served', email=READER, post_id=post,
                                      item_id=item, detail={'page': 0})).result()
    owner = ['Admin', ADMIN, '', 'Admin']
    staff = ['Reader', READER, '', 'Staff']
    roster = {'Team One': sheet(owner, staff), 'Restaurant': sheet(staff), 'Empty': sheet()}
    store.write_allowlist(roster)
    assert client.portal.call(service.refresh)
    report = client.get('/api/admin/progress', headers=admin).json()
    assert report['groups'] == ['Team One', 'Restaurant', 'Empty']
    person = next(p for p in report['people'] if p['email'] == READER)
    assert person['groups'] == ['Team One', 'Restaurant'] and person['percent'] == 50
    assert len(report['people']) == 2
    # A move between locations does not revoke the shared login or reset history.
    roster['Team One'] = sheet(owner)
    store.write_allowlist(roster)
    assert client.portal.call(service.refresh)
    assert client.get('/api/posts', headers=reader).status_code == 200
    person = next(p for p in client.get('/api/admin/progress', headers=admin).json()['people'] if p['email'] == READER)
    assert person['groups'] == ['Restaurant'] and person['percent'] == 50
    # A malformed refresh cannot partially replace the roster or its groups.
    roster['Restaurant'] = sheet(['Reader', READER, '', 'Owner'])
    store.write_allowlist(roster)
    assert client.portal.call(service.refresh) is False
    assert service.db.read(lambda db: meta(db, 'member_groups')[READER]) == ['Restaurant']
    assert client.get('/api/posts', headers=reader).status_code == 200
    # Removal from every tab revokes the existing session; audit evidence survives.
    roster['Restaurant'] = sheet()
    store.write_allowlist(roster)
    assert client.portal.call(service.refresh)
    assert client.get('/api/posts', headers=reader).status_code == 403
    assert service.db.read(lambda db: db.execute("SELECT count(*) FROM events WHERE email=? AND type='page_served'", (READER,)).fetchone()[0]) == 1
    # A new location account may sign in as Staff, with no administration rights.
    roster['Restaurant'] = sheet(['New person', 'new@example.invalid', '', 'Staff'])
    store.write_allowlist(roster)
    assert client.portal.call(service.refresh)
    newcomer = sign_in(client, 'new@example.invalid')
    assert client.get('/api/session', headers=newcomer).json()['role'] == 'reader'
    assert client.get('/api/admin/progress', headers=newcomer).status_code == 403


def test_missing_email_warning_and_empty_tab_reach_admin_report(client, store, service):
    store.write_allowlist({'Team One': sheet(['Admin', ADMIN, '', 'Admin']),
                          'Restaurant': sheet(['Pending contact', '', 'Restaurant'])})
    assert client.portal.call(service.refresh)
    report = client.get('/api/admin/progress', headers=sign_in(client, ADMIN)).json()
    assert report['groups'] == ['Team One', 'Restaurant']
    assert report['roster_warnings'] == [{'group': 'Restaurant', 'row': 2, 'message': 'Missing email address'}]
    assert len(report['people']) == 1


def test_google_reads_all_configured_tabs_together_and_rejects_missing_results(tmp_path, monkeypatch):
    calls = []
    response = {'valueRanges': [{'values': sheet()}, {'values': sheet()}]}
    class Values:
        def batchGet(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(execute=lambda: response)
    values = Values()
    config = Config(tmp_path, allowlist_sheet_id='roster', allowlist_tabs=['Team One', "Owner's restaurant"])
    store = GoogleStore(config)
    monkeypatch.setattr(store, '_service', lambda *args: SimpleNamespace(spreadsheets=lambda: SimpleNamespace(values=lambda: values)))
    assert list(store.allowlist()) == ['Team One', "Owner's restaurant"]
    assert calls == [{'spreadsheetId': 'roster', 'ranges': ["'Team One'!A:Z", "'Owner''s restaurant'!A:Z"], 'majorDimension': 'ROWS'}]
    response['valueRanges'].pop()
    with pytest.raises(StoreUnavailable, match='every configured'):
        store.allowlist()


@pytest.mark.parametrize('tabs', ['Team One', [None], [''], ['Team One', 'Team One']])
def test_invalid_tab_configuration_is_rejected(tmp_path, tabs):
    with pytest.raises(ValueError, match='allowlist_tabs'):
        Config(tmp_path, mode='demo', allowlist_tabs=tabs).validate()
