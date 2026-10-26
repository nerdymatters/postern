"""ENG-D16: the Sheet is the trust root, so a snapshot is validated whole or not applied at all.

Everything here is about the grammar of that sheet plus the address canonicalization that has to
agree with what Google hands back at sign-in.
"""
from __future__ import annotations

import pytest
from conftest import ADMIN, READER, events, sign_in

from postern.auth import canonical_email, parse_allowlist


def sheet(*data):
    return [['email', 'name', 'role'], *data]


def refresh(client):
    """One allowlist refresh on the app's own loop. True when it validated and applied."""
    return client.portal.call(client.service.refresh)


# --- the header row -------------------------------------------------------------------------

@pytest.mark.parametrize('values', [[], [[]], [['']]])
def test_a_sheet_without_a_header_row_is_invalid(values):
    with pytest.raises(ValueError):
        parse_allowlist(values)


def test_the_email_column_is_matched_after_trimming_and_case_folding():
    people = parse_allowlist([['  EMAIL  ', 'Name'], ['Reader@Example.Invalid', 'R']])
    assert list(people) == [READER]


def test_the_configured_header_label_is_the_one_that_counts():
    people = parse_allowlist([['name', 'staff address'], ['R', READER]], email_header='  Staff Address ')
    assert list(people) == [READER]
    with pytest.raises(ValueError):
        parse_allowlist(sheet([READER, 'R', 'reader']), email_header='staff address')


def test_the_email_column_need_not_be_column_a():
    assert parse_allowlist([['name', 'role', 'email'], ['R', 'admin', READER]])[READER]['role'] == 'admin'


def test_a_missing_email_column_never_falls_back_to_column_a():
    with pytest.raises(ValueError):
        parse_allowlist([['address', 'role'], [READER, 'admin']])


def test_a_renamed_email_column_invalidates_rather_than_switching_columns():
    with pytest.raises(ValueError):
        parse_allowlist([['e-mail', 'name', 'role'], [READER, 'R', 'reader']])


def test_two_email_columns_are_ambiguous_and_invalidate():
    with pytest.raises(ValueError):
        parse_allowlist([['email', 'name', 'Email'], [READER, 'R', ADMIN]])


@pytest.mark.parametrize('column', ['name', 'role'])
def test_a_duplicated_optional_column_invalidates(column):
    with pytest.raises(ValueError):
        parse_allowlist([['email', column, column], [READER, 'reader', 'admin']])


# --- data rows ------------------------------------------------------------------------------

def test_wholly_blank_rows_are_ignored():
    people = parse_allowlist(sheet([READER, 'R', 'reader'], [], ['', '  ', ''], [ADMIN, 'A', 'admin']))
    assert sorted(people) == sorted([ADMIN, READER])


def test_a_nonblank_row_without_an_email_invalidates_the_whole_refresh():
    with pytest.raises(ValueError):
        parse_allowlist(sheet([READER, 'R', 'reader'], ['', 'No address yet', 'admin']))


def test_a_short_row_that_stops_before_the_email_column_invalidates():
    with pytest.raises(ValueError):
        parse_allowlist([['name', 'email'], ['Half a row']])


def test_a_row_that_omits_its_trailing_cells_is_still_a_reader():
    # Sheets drops trailing empty cells, so a reader row often arrives as one cell.
    assert parse_allowlist(sheet([READER]))[READER] == {'email': READER, 'name': '', 'role': 'reader'}


@pytest.mark.parametrize('value', ['not-an-address', 'reader@', 'reader@example', 'two words@example.invalid'])
def test_a_malformed_email_invalidates_the_whole_refresh(value):
    with pytest.raises(ValueError):
        parse_allowlist(sheet([READER, 'R', 'reader'], [value, 'Typo', 'reader']))


def test_surrounding_whitespace_in_a_cell_is_trimmed_not_rejected():
    assert list(parse_allowlist(sheet(['  reader@example.invalid  ', ' R ', ' admin ']))) == [READER]


# --- roles ----------------------------------------------------------------------------------

@pytest.mark.parametrize('cell,role', [('', 'reader'), ('   ', 'reader'), ('reader', 'reader'),
                                       (' Reader ', 'reader'), ('admin', 'admin'), (' Admin ', 'admin'),
                                       ('ADMIN', 'admin')])
def test_roles_are_trimmed_and_lowercased(cell, role):
    assert parse_allowlist(sheet([READER, 'R', cell]))[READER]['role'] == role


def test_an_absent_role_column_means_reader():
    assert parse_allowlist([['email'], [READER]])[READER]['role'] == 'reader'


@pytest.mark.parametrize('cell', ['owner', 'administrator', 'read', 'admin,reader', 'yes', 'Admin (temp)'])
def test_any_other_nonempty_role_invalidates_the_whole_refresh(cell):
    with pytest.raises(ValueError):
        parse_allowlist(sheet([ADMIN, 'A', 'admin'], [READER, 'R', cell]))


def test_a_display_name_never_decides_privilege():
    person = parse_allowlist(sheet([READER, 'Admin', '']))[READER]
    assert person['role'] == 'reader' and person['name'] == 'Admin'


# --- duplicates -----------------------------------------------------------------------------

def test_duplicate_normalized_emails_with_the_same_grant_deduplicate():
    people = parse_allowlist(sheet(['Person.One+staff@Gmail.com', 'P', 'admin'],
                                   ['personone@googlemail.com', 'P again', ' ADMIN ']))
    assert list(people) == ['personone@gmail.com'] and people['personone@gmail.com']['role'] == 'admin'


def test_duplicate_normalized_emails_with_conflicting_roles_invalidate():
    with pytest.raises(ValueError):
        parse_allowlist(sheet(['person.one@gmail.com', 'P', 'admin'], ['personone@gmail.com', 'P', 'reader']))


def test_a_blank_role_conflicting_with_admin_still_invalidates():
    with pytest.raises(ValueError):
        parse_allowlist(sheet([ADMIN, 'A', 'admin'], [ADMIN, 'A', '']))


# --- the valid empty list -------------------------------------------------------------------

def test_a_header_only_sheet_is_a_valid_empty_allowlist():
    assert parse_allowlist(sheet()) == {}


def test_no_minimum_count_heuristic_blocks_a_bulk_removal():
    before = parse_allowlist(sheet(*[[f'person{n}@example.invalid', f'P{n}', 'reader'] for n in range(40)]))
    assert len(before) == 40 and parse_allowlist([['email']]) == {}


# --- canonicalization, which has to match what Google returns at sign-in --------------------

@pytest.mark.parametrize('typed,canonical', [
    ('  Reader@Example.Invalid  ', READER),
    ('First.Last@Gmail.com', 'firstlast@gmail.com'),
    ('first.last+postern@gmail.com', 'firstlast@gmail.com'),
    ('First.Last@GoogleMail.com', 'firstlast@gmail.com'),
    ('firstlast+a+b@googlemail.com', 'firstlast@gmail.com'),
    ('First.Last+work@Example.com', 'first.last+work@example.com'),
])
def test_canonical_email(typed, canonical):
    assert canonical_email(typed) == canonical


@pytest.mark.parametrize('value', ['', '   ', 'reader', 'reader@', '@example.invalid', 'reader@example',
                                   'a b@example.invalid', '+@gmail.com', '.@googlemail.com'])
def test_canonical_email_rejects_what_is_not_an_address(value):
    with pytest.raises(ValueError):
        canonical_email(value)


def test_a_dotted_gmail_row_admits_the_account_google_actually_names(client, store):
    store.write_allowlist(sheet([' First.Last+staff@GoogleMail.com ', 'First Last', ' Admin ']))
    assert refresh(client) is True
    headers = sign_in(client, 'FirstLast@gmail.com', 'First Last')
    assert client.get('/api/session', headers=headers).json()['role'] == 'admin'


def test_an_emptied_sheet_revokes_everyone(client, store, service):
    reader = sign_in(client, READER)
    admin = sign_in(client, ADMIN, 'Demo administrator')
    assert client.get('/api/posts', headers=reader).status_code == 200
    store.write_allowlist(sheet())
    assert refresh(client) is True
    assert client.get('/api/posts', headers=reader).status_code == 403
    assert client.get('/api/admin/posts', headers=admin).status_code == 403
    assert sorted(e['email'] for e in events(service, 'allowlist_removed')) == sorted([ADMIN, READER])
