"""ENG-D6: a document the converter cannot handle fails closed, with a message that carries no parser output.

The real sandboxed converter runs here, because the failure being tested is the child process exiting
badly and the parent deciding what an administrator gets to see about it.
"""
from __future__ import annotations
import io
import time

from conftest import ADMIN, READER, create_post, events, sign_in

SAFE_MESSAGE = 'The attachment could not be converted safely. Check its format, size and page count.'


def admin_post(client, headers, post_id):
    return next(p for p in client.get('/api/admin/posts', headers=headers).json()['posts'] if p['id'] == post_id)


def wait_for(client, admin, post_id, status, tries=300):
    items = []
    for _ in range(tries):
        items = admin_post(client, admin, post_id)['items']
        if items and items[0]['derive_status'] == status:
            return items[0]
        time.sleep(0.05)
    raise AssertionError(f'attachment never became {status}: {items}')


def test_an_unconvertible_upload_fails_safely_and_blocks_publication(client, service):
    admin = sign_in(client, ADMIN, 'Demo administrator')
    post = create_post(client, admin)
    poison = b'%PDF-1.7\n' + b'/Type /Catalog <<garbage' * 64 + b'\nTHIS IS NOT A PDF BODY\n%%EOF\n'
    response = client.post(f"/api/admin/posts/{post['id']}/items", headers=admin,
                           files={'file': ('broken.pdf', io.BytesIO(poison), 'application/pdf')})
    assert response.status_code == 200 and response.json()['derive_status'] == 'queued', response.text

    item = wait_for(client, admin, post['id'], 'failed')
    assert item['derive_error'] == SAFE_MESSAGE
    for leak in ('Traceback', 'pdfinfo', 'pdftoppm', 'Syntax', 'garbage', 'THIS IS NOT'):
        assert leak not in item['derive_error'], leak
    assert not (service.config.data_dir / 'derived' / item['id']).exists()
    # The staged original stays: a repaired converter or a later retry can still make this post publishable.
    assert (service.config.data_dir / 'staging' / f"{item['id']}.pdf").read_bytes() == poison

    reader = sign_in(client, READER)
    assert client.post(f"/api/admin/posts/{post['id']}/publish", headers=admin).status_code == 202
    client.portal.call(service.process_operation)
    blocked = admin_post(client, admin, post['id'])
    assert blocked['status'] == 'publishing' and 'failed to prepare' in blocked['error']
    assert client.get('/api/posts', headers=reader).json()['posts'] == []
    assert events(service, 'admin_publish') == []
    assert events(service, 'item_open') == []
