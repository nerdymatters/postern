"""Admin certificate evidence stays private, bounded and stable across restored event order."""
import json

import pytest

from conftest import ADMIN, READER, events, sign_in
from postern import honors
from postern.db import AuditUnavailable, Database, set_meta
from postern.recover import import_events


PATH = '/api/admin/certificate-history'


def seed(service, names=0):
    records = [{'event_id': 'first-pass', 'event': 'quiz_attempt', 'ts': 100, 'email': READER,
                'name': 'Original Account Name',
                'detail': {'chapter': 0, 'score': 16, 'total': 20, 'passed': True}}]
    records += [{'event_id': f'name-{i:03}', 'event': honors.NAMED, 'ts': 200, 'email': READER,
                 'detail': {'name': f'Printed Name {i}'}} for i in range(names)]
    service.db.submit(lambda db: import_events(db, records)).result()
    return records


def fetch(client, headers, **params):
    return client.get(PATH, headers=headers, params={'email': READER, **params})


def test_history_requires_admin_consent_fresh_roster_and_verified_recovery(client, service, clock):
    seed(service)
    assert fetch(client, {}).status_code == 401
    assert fetch(client, sign_in(client)).status_code == 403
    admin = sign_in(client, ADMIN, consent=False)
    assert fetch(client, admin).json()['error'] == 'consent_required'
    admin = sign_in(client, ADMIN)
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', True)).result()
    assert fetch(client, admin).status_code == 409
    service.db.submit(lambda db: set_meta(db, 'recovery_mode', False)).result()
    clock.advance(61)
    assert fetch(client, admin).status_code == 503
    assert events(service, 'certificate_history_viewed') == []


def test_unsaved_suggestion_is_not_invented_history_and_access_is_audited(client, service):
    admin = sign_in(client, ADMIN)
    seed(service)
    response = fetch(client, admin)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['entries'] == [] and data['next_cursor'] is None
    assert data['certificate']['name_confirmed'] is False
    assert data['certificate']['name'] == 'Original Account Name'
    assert data['history_incomplete'] is False
    assert response.headers['cache-control'] == 'no-store'
    audit = events(service, 'certificate_history_viewed')
    assert len(audit) == 1 and audit[0]['email'] == ADMIN
    assert json.loads(audit[0]['payload'])['detail'] == {'email': READER, 'number': data['certificate']['number']}


def test_keyset_pages_keep_ties_malformed_records_and_new_saves_from_skipping_names(client, service, tmp_path):
    admin = sign_in(client, ADMIN)
    records = seed(service, 53)
    # A malformed imported choice consumes a bounded slot but cannot become a name.
    bad = {'event_id': 'name-999', 'event': honors.NAMED, 'ts': 200, 'email': READER, 'detail': {'name': 7}}
    records.append(bad)
    service.db.submit(lambda db: (import_events(db, [bad]), set_meta(db, 'history_incomplete', True))).result()
    first = fetch(client, admin).json()
    assert first['certificate']['name'] == 'Printed Name 52'
    assert first['history_incomplete'] is True
    assert len(first['entries']) == 49
    assert first['next_cursor'] == 'name-004'
    assert first['entries'][0] == {'id': 'name-052', 'name': 'Printed Name 52', 'saved_at': 200}
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'new-save', 'event': honors.NAMED, 'ts': 300, 'email': READER, 'detail': {'name': 'New Name'}}])).result()
    second = fetch(client, admin, before=first['next_cursor']).json()
    assert [entry['id'] for entry in second['entries']] == ['name-003', 'name-002', 'name-001', 'name-000']
    assert second['next_cursor'] is None and second['certificate']['name'] == 'New Name'
    assert len({entry['id'] for page in (first, second) for entry in page['entries']}) == 53
    restored = Database(tmp_path/'restored.sqlite3')
    try:
        def write(db):
            db.execute('INSERT INTO members VALUES (?,?,?)', (READER, 'Demo reader', 'reader'))
            import_events(db, list(reversed(records)))
            set_meta(db, 'history_incomplete', True)
        restored.submit(write).result()
        assert restored.read(lambda db: honors.name_history(db, READER)) == first
    finally:
        restored.close()


@pytest.mark.parametrize('before', ['missing', 'first-pass', 'someone-elses-name'])
def test_cursor_cannot_read_another_person_or_another_event_type(client, service, before):
    admin = sign_in(client, ADMIN)
    seed(service, 1)
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'someone-elses-name', 'event': honors.NAMED, 'ts': 200, 'email': ADMIN,
         'detail': {'name': 'Private Admin Name'}}])).result()
    assert fetch(client, admin, before=before).status_code == 422


def test_history_is_only_for_current_roster_certificate_holders(client, service):
    admin = sign_in(client, ADMIN)
    assert fetch(client, admin).status_code == 404
    seed(service)
    service.db.submit(lambda db: db.execute('DELETE FROM members WHERE email=?', (READER,))).result()
    assert fetch(client, admin).status_code == 404
    assert events(service, 'certificate_history_viewed') == []


def test_history_preserves_saved_spelling_and_flags_unusable_restored_rows(client, service):
    admin = sign_in(client, ADMIN)
    seed(service)
    saved = 'A\u0301sha'
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'original-spelling', 'event': honors.NAMED, 'ts': 200, 'email': READER, 'detail': {'name': saved}},
        {'event_id': 'bad-date', 'event': honors.NAMED, 'ts': 1e30, 'email': READER, 'detail': {'name': 'Bad Timestamp'}},
        {'event_id': 'bad-unicode', 'event': honors.NAMED, 'ts': 201, 'email': READER, 'detail': {'name': 'Broken\ud800 Name'}},
    ])).result()
    response = fetch(client, admin)
    assert response.status_code == 200, response.text
    assert response.json()['entries'] == [{'id': 'original-spelling', 'name': saved, 'saved_at': 200}]
    assert response.json()['history_incomplete'] is True


def test_oversized_optional_save_time_uses_the_valid_event_clock(client, service):
    admin = sign_in(client, ADMIN)
    seed(service)
    service.db.submit(lambda db: import_events(db, [
        {'event_id': 'huge-save-time', 'event': honors.NAMED, 'ts': 200, 'email': READER,
         'detail': {'name': 'Restored Reader', 'saved_at': 10**309}}])).result()
    response = fetch(client, admin)
    assert response.status_code == 200, response.text
    assert response.json()['entries'] == [{'id': 'huge-save-time', 'name': 'Restored Reader', 'saved_at': 200}]


@pytest.mark.parametrize('params', [{'email': 'invalid'}, {'email': 'x'*255}, {'before': ''}, {'before': 'x'*257}])
def test_invalid_history_parameters_are_refused_without_server_errors(client, params):
    assert fetch(client, sign_in(client, ADMIN), **params).status_code == 422


@pytest.mark.parametrize('change,status', [('admin', 403), ('target', 404), ('recovery', 409)])
def test_snapshot_does_not_hold_writer_and_rechecks_before_disclosure(client, service, monkeypatch, change, status):
    import concurrent.futures
    import threading
    admin = sign_in(client, ADMIN)
    seed(service, 1)
    started, release = threading.Event(), threading.Event()
    original = honors.name_history
    def slow(db, *args, **kwargs):
        assert db.execute('PRAGMA query_only').fetchone()[0] == 1 and db.in_transaction
        result = original(db, *args, **kwargs)
        started.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(honors, 'name_history', slow)
    def mutate(db):
        if change == 'admin':
            db.execute("UPDATE members SET role='reader' WHERE email=?", (ADMIN,))
        elif change == 'target':
            db.execute('DELETE FROM members WHERE email=?', (READER,))
        else:
            set_meta(db, 'recovery_mode', True)
    with concurrent.futures.ThreadPoolExecutor() as pool:
        pending = pool.submit(fetch, client, admin)
        assert started.wait(3)
        try:
            service.db.submit(mutate).result(timeout=1)
        finally:
            release.set()
        response = pending.result(timeout=5)
    assert response.status_code == status
    assert events(service, 'certificate_history_viewed') == []


def test_audit_failure_withholds_history(client, service, monkeypatch):
    import postern.service as service_module
    admin = sign_in(client, ADMIN)
    seed(service, 1)
    original = service_module.event
    def unavailable(db, kind, *args, **kwargs):
        if kind == 'certificate_history_viewed':
            raise AuditUnavailable('synthetic writer failure')
        return original(db, kind, *args, **kwargs)
    monkeypatch.setattr(service_module, 'event', unavailable)
    response = fetch(client, admin)
    assert response.status_code == 503 and response.json()['error'] == 'audit_unavailable'
    assert 'entries' not in response.json()
