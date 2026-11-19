"""Deadline cancellation covers producer work and the fallback transport itself."""
import asyncio
import time

import pytest

from postern.content_delivery import ContentDeliveryDeadline


SCOPE = {'type': 'http', 'path': '/v1/spaces/s/resources/r/video/content.mp4'}


def test_emission_deadline_cancels_a_producer_waiting_between_sends():
    async def scenario():
        cancelled = asyncio.Event(); messages = []
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async def send(message): messages.append(message)
        async def receive(): await asyncio.Event().wait()
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            await ContentDeliveryDeadline(app, prepare_seconds=1, emit_seconds=.05)(SCOPE, receive, send)
        assert time.monotonic()-began < .5 and cancelled.is_set()
        assert len(messages) == 2
    asyncio.run(scenario())


def test_prepare_timeout_fallback_cannot_wait_forever_on_send():
    async def scenario():
        async def app(*args): await asyncio.Event().wait()
        async def send(message): await asyncio.Event().wait()
        async def receive(): await asyncio.Event().wait()
        began = time.monotonic()
        with pytest.raises(TimeoutError):
            await ContentDeliveryDeadline(app, prepare_seconds=.02, send_seconds=.02)(SCOPE, receive, send)
        assert time.monotonic()-began < .5
    asyncio.run(scenario())
