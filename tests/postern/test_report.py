"""ENG-D12: the opened / not opened report, asserted as a unit against a fixed event set.

Nothing here is inferred from what the reader routes happen to emit. Every member, view and event
below is written deliberately through the service's own write queue, so a reader path that stopped
recording opens (or started fabricating them) cannot make these pass.
"""
from __future__ import annotations
import uuid
from types import SimpleNamespace

import pytest

from conftest import ADMIN, READER, sign_in, events
from postern.auth import parse_allowlist
from postern.db import event, set_meta

PUBLISHED = 1_700_000_000.0
SKIMMER = 'skimmer@example.invalid'
TWICE = 'twice@example.invalid'
GONE = 'gone@example.invalid'
LATE = 'late@example.invalid'
STRANGER = 'stranger@example.invalid'

ROSTER = [['email', 'name', 'role'],
          [ADMIN, 'Demo administrator', 'admin'],
          [READER, 'Demo reader', 'reader'],
          [SKIMMER, 'Sam Skimmer', 'reader'],
          [TWICE, 'Tina Twice', 'reader'],
          [GONE, 'Gary Gone', 'reader']]

# email, name, role, status, attempts, first_opened_at, seconds_to_open
EXPECTED = [
    (ADMIN, 'Demo administrator', 'admin', 'opened', 1, PUBLISHED + 120, 120.0),
    (READER, 'Demo reader', 'reader', 'opened', 3, PUBLISHED + 90, 90.0),
    (SKIMMER, 'Sam Skimmer', 'reader', 'not_opened', 2, None, None),
    (TWICE, 'Tina Twice', 'reader', 'opened', 2, PUBLISHED + 30, 30.0),
    (GONE, 'Gary Gone', 'reader', 'opened', 1, PUBLISHED + 45, 45.0),
    (LATE, 'Late Joiner', 'reader', 'not_opened', 0, None, None),
]


def write(service, operation):
    return service.db.submit(operation).result(timeout=5)


def apply_roster(service, store, roster):
    """Put a roster in the allowlist source and apply it through the real Permissions.apply."""
    store.write_allowlist(roster)
    snapshot = parse_allowlist(roster, service.config.email_header)
    write(service, lambda db: service.permissions.apply(db, snapshot))


def seed_post(service, published_at=PUBLISHED, title='Tip pooling policy'):
    post_id, item_id = str(uuid.uuid4()), str(uuid.uuid4())

    def insert(db):
        db.execute("INSERT INTO posts(id,title,body,category,status,created_at,created_by,published_at)"
                   " VALUES (?,?,'Effective Oct 6.','Policies','published',?,?,?)",
                   (post_id, title, published_at - 600, ADMIN, published_at))
        db.execute("INSERT INTO items(id,post_id,filename,kind,sha256,bytes,pages,derive_status,sort)"
                   " VALUES (?,?,'policy.pdf','pdf',?,1,2,'ready',0)", (item_id, post_id, 'x' * 64))
    write(service, insert)
    return post_id, item_id


def seed_attempt(db, email, post_id, item_id, requested_at, opened_at=None):
    """One reading attempt: the view row plus exactly the events the server writes for it."""
    session, view = str(uuid.uuid4()), str(uuid.uuid4())
    db.execute("INSERT INTO sessions(id,token_hash,subject,email,name,created_at,expires_at,client)"
               " VALUES (?,?,?,?,?,?,?,'web')", (session, session, 'sub-' + email, email, email, PUBLISHED, PUBLISHED + 86400))
    db.execute('INSERT INTO views(id,session_id,email,item_id,post_id,attempt_id,requested_at,first_served_at)'
               ' VALUES (?,?,?,?,?,?,?,?)', (view, session, email, item_id, post_id, str(uuid.uuid4()), requested_at, opened_at))
    event(db, 'view_requested', {'email': email, 'id': session}, post_id=post_id, item_id=item_id, view_id=view, timestamp=requested_at)
    if opened_at is not None:
        event(db, 'item_open', {'email': email, 'id': session}, post_id=post_id, item_id=item_id, view_id=view, timestamp=opened_at)
    return view


@pytest.fixture
def scenario(client, service, store):
    """One published post, one fixed history, one admin who can fetch the report repeatedly."""
    apply_roster(service, store, ROSTER)
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post_id, item_id = seed_post(service)
    other_id, other_item = seed_post(service, published_at=PUBLISHED - 86_400, title='Last month')

    def history(db):
        # An admin who read the post is just another reader here.
        seed_attempt(db, ADMIN, post_id, item_id, PUBLISHED + 100, PUBLISHED + 120)
        # Three attempts, one of which served content.
        seed_attempt(db, READER, post_id, item_id, PUBLISHED + 60)
        seed_attempt(db, READER, post_id, item_id, PUBLISHED + 80, PUBLISHED + 90)
        seed_attempt(db, READER, post_id, item_id, PUBLISHED + 300)
        # Attempts only. The two detail-tier rows are not opens either.
        seed_attempt(db, SKIMMER, post_id, item_id, PUBLISHED + 20)
        seed_attempt(db, SKIMMER, post_id, item_id, PUBLISHED + 40)
        event(db, 'page_served', {'email': SKIMMER}, post_id=post_id, item_id=item_id, summary=False, timestamp=PUBLISHED + 41)
        event(db, 'thumbnail_served', {'email': SKIMMER}, post_id=post_id, item_id=item_id, summary=False, timestamp=PUBLISHED + 42)
        # Two views of the same post; the open written second is the earlier one.
        seed_attempt(db, TWICE, post_id, item_id, PUBLISHED + 590, PUBLISHED + 600)
        seed_attempt(db, TWICE, post_id, item_id, PUBLISHED + 25, PUBLISHED + 30)
        seed_attempt(db, GONE, post_id, item_id, PUBLISHED + 40, PUBLISHED + 45)
        # Decoys: another post nobody may be credited for, and an open by someone off the list.
        seed_attempt(db, SKIMMER, other_id, other_item, PUBLISHED - 80_000, PUBLISHED - 79_000)
        seed_attempt(db, READER, other_id, other_item, PUBLISHED - 80_000, PUBLISHED - 79_000)
        seed_attempt(db, STRANGER, post_id, item_id, PUBLISHED + 5, PUBLISHED + 10)
    write(service, history)
    apply_roster(service, store, ROSTER + [[LATE, 'Late Joiner', 'reader']])

    def fetch():
        response = client.get(f'/api/admin/posts/{post_id}/report', headers=admin)
        assert response.status_code == 200, response.text
        return response.json()
    return SimpleNamespace(fetch=fetch, post_id=post_id, service=service, store=store)


def people(report):
    return {person['email']: person for person in report['people']}


@pytest.mark.parametrize('email,name,role,status,attempts,first_opened_at,seconds_to_open', EXPECTED,
                         ids=[row[0].split('@')[0] for row in EXPECTED])
def test_each_person_renders_from_the_fixed_history(scenario, email, name, role, status, attempts, first_opened_at, seconds_to_open):
    person = people(scenario.fetch())[email]
    assert (person['name'], person['role']) == (name, role)
    assert person['status'] == status
    assert person['attempts'] == attempts
    assert person['first_opened_at'] == first_opened_at
    assert person['seconds_to_open'] == seconds_to_open


def test_the_report_counts_everyone_currently_on_the_list(scenario):
    report = scenario.fetch()
    assert sorted(people(report)) == sorted(row[0] for row in EXPECTED)
    assert report['total'] == 6
    # A reader with three attempts and one open counts once; attempts never inflate the tally.
    assert report['opened'] == 4
    # The decoys are self-verifying: they exist in the history and still change nothing above.
    opens = events(scenario.service, 'item_open')
    assert len([e for e in opens if e['post_id'] != scenario.post_id]) == 2
    assert [e['email'] for e in opens if e['email'] == STRANGER] == [STRANGER]
    assert STRANGER not in people(report), 'an open by someone off the list must not create a row'


def test_attempts_alone_never_count_as_opened(scenario):
    skimmer = people(scenario.fetch())[SKIMMER]
    assert skimmer['status'] == 'not_opened' and skimmer['attempts'] == 2
    assert skimmer['first_opened_at'] is None


def test_removal_leaves_the_denominator_while_the_history_survives(scenario):
    apply_roster(scenario.service, scenario.store, [row for row in ROSTER if row[0] != GONE] + [[LATE, 'Late Joiner', 'reader']])
    report = scenario.fetch()
    assert GONE not in people(report)
    assert (report['total'], report['opened']) == (5, 3)
    survived = [e for e in events(scenario.service, 'item_open') if e['email'] == GONE and e['post_id'] == scenario.post_id]
    assert [e['ts'] for e in survived] == [PUBLISHED + 45]


def test_incomplete_history_renders_as_incomplete_never_as_not_opened(scenario):
    write(scenario.service, lambda db: set_meta(db, 'history_incomplete', True))
    report = scenario.fetch()
    assert report['history_incomplete'] is True
    assert {person['status'] for person in report['people'] if person['email'] in (SKIMMER, LATE)} == {'unknown'}
    assert not any(person['status'] == 'not_opened' for person in report['people'])
    # Evidence that survives the gap is still evidence.
    assert people(report)[READER]['status'] == 'opened'
    assert (report['total'], report['opened']) == (6, 4)


def test_the_report_says_opened_means_served_not_read(scenario):
    meaning = scenario.fetch()['meaning'].lower()
    assert 'served' in meaning
    assert 'not proof' in meaning and 'read' in meaning
