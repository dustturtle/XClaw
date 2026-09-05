"""Image inputs use the same trusted chat and execution boundary as text."""

import base64
import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from xclaw.agent_engine import AgentContext, agent_loop
from xclaw.channels.web import create_web_app
from xclaw.config import Settings
from xclaw.powercore import PowerCoreClient
from xclaw.tools import ToolRegistry


def picture():
    buffer = io.BytesIO()
    Image.new("RGB", (12, 12), "blue").save(buffer, "PNG")
    return {"name": "sample.png", "media_type": "image/png", "data": base64.b64encode(buffer.getvalue()).decode()}


def test_image_request_rejects_unsupported_backend_without_dropping_input():
    handler = AsyncMock(return_value="wrong: ignored attachment")
    client = TestClient(create_web_app(message_handler=handler))
    response = client.post("/api/chat", json={"message": "看图", "attachments": [picture()]})
    assert response.status_code == 400
    handler.assert_not_awaited()


@pytest.mark.parametrize("stream", [False, True])
def test_image_request_keeps_authenticated_chat_mapping(stream):
    received = []

    async def handler(chat_id, text, *, attachments=None):
        received.append((chat_id, text, attachments))
        return "蓝色"

    async def streaming(chat_id, text, *, attachments=None):
        yield await handler(chat_id, text, attachments=attachments)

    client = TestClient(create_web_app(
        message_handler=handler, stream_handler=streaming, supports_images=True,
        multi_user_mode=True, auth_token="test-only-token",
    ))
    path = "/api/chat/stream" if stream else "/api/chat"
    payload = {"chat_id": "image-chat", "message": "看图", "attachments": [picture()]}
    assert client.post(path, json=payload).status_code == 401
    response = client.post(path, json=payload, headers={"Authorization": "Bearer test-only-token"})
    assert response.status_code == 200
    assert "蓝色" in response.text
    assert received[0][0].endswith("_image-chat") and received[0][0] != "image-chat"
    assert received[0][2][0].data == payload["attachments"][0]["data"]


@pytest.mark.parametrize("mutation", [
    {"data": "not-base64-private-content"},
    {"media_type": "image/jpeg"},
    {"data": base64.b64encode(b"not an image").decode()},
    {"url": "http://127.0.0.1/private"},
])
def test_malformed_images_are_rejected_without_echoing_payload(mutation):
    handler = AsyncMock(return_value="must not run")
    client = TestClient(create_web_app(message_handler=handler, supports_images=True))
    attachment = picture() | mutation
    response = client.post("/api/chat", json={"message": "看图", "attachments": [attachment]})
    assert response.status_code == 422
    assert attachment["data"] not in response.text
    handler.assert_not_awaited()


def test_too_many_images_and_oversized_body_are_rejected():
    handler = AsyncMock(return_value="must not run")
    client = TestClient(create_web_app(message_handler=handler, supports_images=True))
    assert client.post("/api/chat", json={"message": "看图", "attachments": [picture()] * 5}).status_code == 422
    response = client.post("/api/chat", content=b"x" * (29 * 1024 * 1024))
    assert response.status_code == 413
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_chunked_upload_is_bounded_without_content_length():
    handler = AsyncMock(return_value="must not run")
    app = create_web_app(message_handler=handler, supports_images=True)

    async def chunks():
        yield b"x" * (15 * 1024 * 1024)
        yield b"x" * (15 * 1024 * 1024)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.post("/api/chat", content=chunks())
    assert response.status_code == 413
    handler.assert_not_awaited()


def test_oversized_image_dimensions_are_rejected_before_decoding_pixels():
    buffer = io.BytesIO()
    Image.new("1", (5001, 5000), 1).save(buffer, "PNG")
    handler = AsyncMock(return_value="must not run")
    client = TestClient(create_web_app(message_handler=handler, supports_images=True))
    response = client.post("/api/chat", json={"message": "看图", "attachments": [{
        "name": "large.png", "media_type": "image/png", "data": base64.b64encode(buffer.getvalue()).decode(),
    }]})
    assert response.status_code == 422
    handler.assert_not_awaited()


def test_image_only_message_is_forwarded():
    handler = AsyncMock(return_value="蓝色")
    client = TestClient(create_web_app(message_handler=handler, supports_images=True))
    response = client.post("/api/chat", json={"attachments": [picture()]})
    assert response.status_code == 200
    assert handler.call_args.args[1] == ""
    assert len(handler.call_args.kwargs["attachments"]) == 1


@pytest.mark.asyncio
async def test_image_during_pending_text_question_is_not_silently_submitted(db):
    from xclaw.attachments import ImageAttachment

    def server(request):
        pytest.fail("An image must not be submitted to a text-only input request")

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    chat_id = await db.get_or_create_chat("web", "pending-image")
    ctx = AgentContext(chat_id=chat_id, channel="web", db=db, llm=AsyncMock(), tools=ToolRegistry(), settings=backend.settings, powercore=backend)
    pending = {"input_request_id": "input_test", "response_schema": {"required": ["answer"]}}
    await db.bind_powercore_run(chat_id, "run_pending", "session_pending", [])
    await db.update_powercore_run("run_pending", "waiting_input", 3, pending)
    reply = await agent_loop(ctx, "看图", attachments=[ImageAttachment(**picture())])
    assert "只接受文字" in reply
    assert (await db.get_powercore_session(chat_id))["status"] == "waiting_input"
    await backend.close()


@pytest.mark.asyncio
async def test_powercore_forwards_image_blocks_and_does_not_store_base64_in_chat_log(db):
    from xclaw.attachments import ImageAttachment

    requests = []

    def server(request):
        requests.append(request)
        if request.method == "POST":
            return httpx.Response(202, json={"schema_version": 1, "run_id": "run_image", "session_id": "session_image"})
        return httpx.Response(200, text='data: {"schema_version":1,"sequence":1,"run_id":"run_image","type":"run.completed","response":"蓝色"}\n\n')

    backend = PowerCoreClient(Settings(), db, transport=httpx.MockTransport(server))
    chat_id = await db.get_or_create_chat("web", "image-chat")
    ctx = AgentContext(chat_id=chat_id, channel="web", db=db, llm=AsyncMock(), tools=ToolRegistry(), settings=backend.settings, powercore=backend)
    attachment = ImageAttachment(**picture())
    assert await agent_loop(ctx, "看图", attachments=[attachment]) == "蓝色"
    payload = json.loads(requests[0].content)
    assert "prompt" not in payload
    assert payload["input"][0]["type"] == "text"
    assert payload["input"][1] == {"type": "image", "source": {"source_type": "base64", "media_type": "image/png", "data": attachment.data}, "alt_text": attachment.name}
    assert attachment.data not in json.dumps(await db.get_recent_messages(chat_id))
    assert "sample.png" in (await db.get_recent_messages(chat_id))[0]["content"]
    ctx.llm.chat.assert_not_awaited()
    await backend.close()
