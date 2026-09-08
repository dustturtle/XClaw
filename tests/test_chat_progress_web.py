"""Web progress is an authenticated public projection, never a replay of POST."""

import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from xclaw.channels.web import _build_chat_page_html, create_web_app
from xclaw.config import Settings


def snapshot(**overrides):
    return {
        "run_id": "run_current", "status": "running", "stage": "executing",
        "message": "Working", "elapsed_seconds": 12.5, "wall_time_ms": 600000,
        "sequence": 7, **overrides,
    }


def test_lifespan_invokes_background_recovery_once():
    startup = AsyncMock()
    with TestClient(create_web_app(message_handler=AsyncMock(), startup_handler=startup)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health").status_code == 200
    startup.assert_awaited_once_with()


@pytest.mark.parametrize("multi_user", [False, True])
def test_progress_and_cancel_share_chat_authentication_and_namespace(multi_user):
    message = AsyncMock(return_value="ok")
    progress = AsyncMock(return_value=snapshot())
    cancel = AsyncMock(return_value=snapshot(status="cancelled"))
    client = TestClient(create_web_app(
        message_handler=message, progress_handler=progress, cancel_handler=cancel,
        auth_token="test-token", multi_user_mode=multi_user,
    ))
    for authorization in ["", "Bearer wrong", "Basic test-token"]:
        headers = {"Authorization": authorization}
        assert client.get("/api/chat/progress?chat_id=room", headers=headers).status_code == 401
        assert client.post("/api/chat/cancel", json={"chat_id": "room", "run_id": "run_current"}, headers=headers).status_code == 401
    progress.assert_not_awaited()
    cancel.assert_not_awaited()
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/api/chat", json={"chat_id": "room", "message": "hello"}, headers=headers).status_code == 200
    effective_id = message.await_args.args[0]
    assert effective_id != "room" if multi_user else effective_id == "room"
    response = client.get("/api/chat/progress?chat_id=room&include_history=true", headers=headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    progress.assert_awaited_once_with(effective_id, include_history=True)
    response = client.post("/api/chat/cancel", json={"chat_id": "room", "run_id": "run_current"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    cancel.assert_awaited_once_with(effective_id, "run_current")


@pytest.mark.parametrize("query, expected", [("", False), ("&include_history=false", False), ("&include_history=true", True)])
def test_progress_history_is_opt_in_and_public_fields_only(query, expected):
    progress = AsyncMock(return_value=snapshot(
        trace={"secret": "private-trace"},
        tool_arguments={"api_key": "private-key"},
        messages=[
            {"role": "user", "content": "hello", "metadata": "private-metadata"},
            {"role": "assistant", "content": "answer", "run_id": "run_current", "tool_calls": ["private-call"]},
            {"role": "tool", "content": "private-output"},
            {"role": "system", "content": "private-instructions"},
        ],
    ))
    client = TestClient(create_web_app(message_handler=AsyncMock(), progress_handler=progress))
    response = client.get("/api/chat/progress?chat_id=room" + query)
    assert response.status_code == 200
    assert "private-" not in response.text
    assert ("messages" in response.json()) is expected
    if expected:
        assert response.json()["messages"] == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "answer", "run_id": "run_current"},
        ]
    progress.assert_awaited_once_with("room", include_history=expected)


def test_invalid_history_flag_is_rejected():
    handler = AsyncMock(return_value=snapshot())
    client = TestClient(create_web_app(message_handler=AsyncMock(), progress_handler=handler))
    assert client.get("/api/chat/progress?include_history=maybe").status_code == 422
    handler.assert_not_awaited()


@pytest.mark.parametrize("path, method, payload", [
    ("/api/chat/progress", "get", None),
    ("/api/chat/cancel", "post", {"run_id": "run_current"}),
])
def test_unsupported_progress_backend_returns_501_after_auth(path, method, payload):
    client = TestClient(create_web_app(message_handler=AsyncMock(), auth_token="token"))
    kwargs = {"json": payload} if payload else {}
    assert getattr(client, method)(path, **kwargs).status_code == 401
    assert getattr(client, method)(path, headers={"Authorization": "Bearer token"}, **kwargs).status_code == 501


@pytest.mark.parametrize("run_id", ["", "../private-run", "run/other", "run?key=private-key", "run id", "a" * 129, "run\n"])
def test_cancel_run_id_validation_never_calls_handler_or_echoes_input(run_id):
    handler = AsyncMock(return_value=snapshot())
    client = TestClient(create_web_app(message_handler=AsyncMock(), cancel_handler=handler))
    response = client.post("/api/chat/cancel", json={"run_id": run_id})
    assert response.status_code == 422
    assert "input" not in response.json()["detail"][0]
    assert "private-key" not in response.text
    handler.assert_not_awaited()


@pytest.mark.parametrize("payload, options", [
    ({}, {}), ({"execution_mode": None}, {}),
    ({"execution_mode": "standard"}, {"execution_mode": "standard"}),
    ({"execution_mode": "analysis"}, {"execution_mode": "analysis"}),
])
def test_execution_mode_only_forwarded_when_non_null(payload, options):
    handler = AsyncMock(return_value="reply")
    client = TestClient(create_web_app(message_handler=handler))
    response = client.post("/api/chat", json={"chat_id": "room", "message": "hello", **payload})
    assert response.status_code == 200
    handler.assert_awaited_once_with("room", "hello", **options)


def test_legacy_two_argument_handler_and_invalid_mode():
    async def legacy(chat_id, text):
        return text

    client = TestClient(create_web_app(message_handler=legacy))
    assert client.post("/api/chat", json={"message": "hello"}).json()["reply"] == "hello"
    assert client.post("/api/chat", json={"message": "hello", "execution_mode": "unlimited"}).status_code == 422


def test_invalid_handler_reply_is_a_safe_500():
    client = TestClient(create_web_app(message_handler=AsyncMock(return_value={"api_key": "private-key"})))
    response = client.post("/api/chat", json={"message": "hello"})
    assert response.status_code == 500
    assert "private-key" not in response.text


@pytest.mark.parametrize("endpoint", ["chat", "progress", "cancel", "stream"])
def test_chat_failures_do_not_expose_or_log_exception_secrets(endpoint):
    private = "Authorization: Bearer private-key; trace private-trace"

    async def broken(*args, **kwargs):
        raise RuntimeError(private)

    async def stream(*args, **kwargs):
        raise RuntimeError(private)
        yield ""  # pragma: no cover

    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        client = TestClient(create_web_app(
            message_handler=broken, progress_handler=broken, cancel_handler=broken, stream_handler=stream,
        ))
        if endpoint == "progress":
            response = client.get("/api/chat/progress")
        elif endpoint == "cancel":
            response = client.post("/api/chat/cancel", json={"run_id": "run_current"})
        else:
            response = client.post("/api/chat" + ("/stream" if endpoint == "stream" else ""), json={"message": "hello"})
        assert response.status_code == (200 if endpoint == "stream" else 500)
        assert "private-" not in response.text
        assert "private-" not in "".join(logs)
    finally:
        logger.remove(sink)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_after_disconnect", [False, True])
async def test_message_task_survives_cancelled_http_request(fail_after_disconnect):
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    async def handler(chat_id, text):
        calls.append((chat_id, text))
        started.set()
        await release.wait()
        finished.set()
        if fail_after_disconnect:
            raise RuntimeError("private-key")
        return "complete"

    app = create_web_app(message_handler=handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        request = asyncio.create_task(client.post("/api/chat", json={"message": "hello"}))
        await asyncio.wait_for(started.wait(), 2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert len(app.state.chat_tasks) == 1
        assert not finished.is_set()
        tasks = list(app.state.chat_tasks)
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 2)
        await asyncio.sleep(0)
    assert not app.state.chat_tasks
    assert calls == [("web_default", "hello")]


@pytest.mark.parametrize("enabled", [False, True])
def test_page_controls_and_config_are_capability_driven(enabled):
    client = TestClient(create_web_app(
        message_handler=AsyncMock(), settings=Settings(agent_backend="native"),
        progress_handler=AsyncMock(return_value=snapshot()) if enabled else None,
    ))
    page = client.get("/").text
    assert '<option value="analysis" selected>' in page
    assert "普通 · 3 分钟" in page and "分析 · 10 分钟" in page
    assert 'id="cancel-run-btn"' in page and 'id="run-time"' in page
    assert 'id="resume-progress-btn"' in page
    assert ('id="run-controls" hidden' in page) is not enabled
    assert "const SUPPORTS_PROGRESS = " + str(enabled).lower() in page
    assert client.get("/api/config").json()["supports_progress"] is enabled


# Execute the actual generated page script, with deterministic DOM/fetch/timers.
# This catches cross-request races without starting or restarting a local service.
NODE_HARNESS = r"""
const assert = require("node:assert/strict");
const vm = require("node:vm");
const {createHash} = require("node:crypto");
const input = JSON.parse(require("node:fs").readFileSync(0, "utf8"));
const requests = [];
const timers = new Map();
const local = new Map([["xclaw.chat_id", "room"]]);
const session = new Map();
let nextTimer = 1, elements, context, events;
function storage(map) {
  return {getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, value)};
}
class Element {
  constructor() {
    this.value = ""; this.disabled = false; this.hidden = false;
    this.children = []; this.listeners = {}; this.files = []; this._text = "";
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(""); }
  replaceChildren() { this._text = ""; this.children = []; }
  appendChild(child) { this.children.push(child); }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
function fetchMock(url, options = {}) {
  return new Promise((resolve, reject) => {
    const request = {
      url: String(url), options, handled: false,
      reply(data, status = 200) {
        this.handled = true;
        resolve({ok: status >= 200 && status < 300, status, json: async () => data});
      },
      fail() { this.handled = true; reject(new Error("private-network-details")); }
    };
    options.signal?.addEventListener("abort", () => request.fail());
    requests.push(request);
  });
}
function createPage() {
  elements = new Map(); events = {};
  context = vm.createContext({
    document: {
      getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
      createElement() { return new Element(); }
    },
    localStorage: storage(local), sessionStorage: storage(session),
    window: {addEventListener(name, callback) { events[name] = callback; }},
    crypto: {randomUUID: require("node:crypto").randomUUID, subtle: {async digest(_type, data) { return createHash("sha256").update(data).digest(); }}},
    TextEncoder, URLSearchParams, AbortController, Uint8Array, atob,
    URL: {createObjectURL(file) { return "blob:" + file.name; }, revokeObjectURL() {}},
    FileReader: class {
      readAsDataURL() { this.result = "data:image/png;base64,aW1hZ2U="; this.onload(); }
    },
    fetch: fetchMock,
    setTimeout(fn, delay) { const id = nextTimer++; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); }
  });
  vm.runInContext(input.script, context);
}
const el = id => elements.get(id);
const evaluate = code => vm.runInContext(code, context);
async function flush() { for (let i = 0; i < 30; i++) await Promise.resolve(); }
function request(path, method = "GET") {
  const found = requests.find(item => !item.handled && item.url.split("?")[0] === path && (item.options.method || "GET") === method);
  assert.ok(found, "missing request: " + method + " " + path);
  return found;
}
function snap(extra = {}) {
  return {run_id: "run_new", status: "running", stage: "executing", message: "Working",
    elapsed_seconds: 4, wall_time_ms: 600000, sequence: 2, ...extra};
}
function idle() { return snap({run_id: undefined, status: "idle", stage: "idle", sequence: 0}); }
async function progress(data) { request("/api/chat/progress").reply(data); await flush(); }
async function tick() {
  const timer = [...timers].find(([, value]) => value.delay === 1500);
  assert.ok(timer, "missing bounded progress timer");
  timers.delete(timer[0]); timer[1].fn(); await flush();
}
async function initial(data = idle()) { await flush(); await progress(data); }
async function send(text = "hello") {
  el("message-input").value = text;
  el("message-input").listeners.input();
  void el("send-btn").listeners.click();
  await flush();
  return request("/api/chat", "POST");
}
function replies() { return el("messages").children.filter(item => item.className === "message assistant").map(item => item.textContent); }
async function main() {
  createPage();
  await flush();
  await eval("(async () => {" + input.scenario + "\n})()");
}
main().catch(error => { process.stderr.write(error.stack + "\n"); process.exitCode = 1; });
"""


def run_page_script(scenario, *, supports_progress=True, markdown=False, receipts=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for deterministic browser-script tests")
    page = _build_chat_page_html(
        auth_enabled=False, model_name="test", provider_name="test",
        supports_images=True, supports_progress=supports_progress,
        supports_receipts=receipts,
    )
    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    if markdown:
        static = Path(__file__).resolve().parents[1] / "xclaw/channels/static"
        script = (
            (static / "markdown-it.min.js").read_text()
            + "\nwindow.markdownit = markdownit;\n"
            + (static / "markdown.js").read_text()
            + "\n" + script
        )
    result = subprocess.run(
        [node, "-e", NODE_HARNESS],
        input=json.dumps({"script": script, "scenario": scenario}),
        text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_page_renders_history_and_direct_replies_with_the_local_markdown_renderer():
    run_page_script(r"""
await initial(snap({status: "completed", stage: "completed", reply: "**历史回复**",
  messages: [{role: "assistant", content: "**历史回复**", run_id: "run_new"}]}));
assert.match(el("messages").children[0].children[0].innerHTML, /<strong>历史回复<\/strong>/);
const post = await send("/reset");
post.reply({reply: "**重置成功**", delivery: "direct"});
await flush();
const items = el("messages").children;
assert.match(items.at(-1).children[0].innerHTML, /<strong>重置成功<\/strong>/);
assert.equal(el("message-input").value, "");
await progress(idle());
assert.equal(el("send-btn").disabled, false);
""", markdown=True)


def test_receipt_ack_is_not_completion_and_exact_receipt_delivers_once():
    run_page_script(r"""
await initial();
const post = await send("question");
const id = JSON.parse(post.options.body).request_id;
assert.match(id, /^[a-f0-9]{32}$/);
request("/api/chat/receipts/" + id).reply({request_id: id, status: "processing"});
await flush();
post.reply({request_id: id, status: "accepted"}, 202);
await flush();
assert.equal(el("send-btn").disabled, true);
request("/api/chat/receipts/" + id).reply({request_id: id, status: "completed", reply: "receipt answer"});
await flush();
assert.deepEqual(replies(), ["receipt answer"]);
assert.equal(el("message-input").value, "");
assert.equal(el("send-btn").disabled, false);
assert.ok([...local.values()].every(value => !value.includes(id)));
""", receipts=True)


def test_missing_receipt_retry_reuses_same_id_and_never_posts_automatically():
    run_page_script(r"""
await initial();
const post = await send("question");
const id = JSON.parse(post.options.body).request_id;
post.fail();
await flush();
request("/api/chat/receipts/" + id).reply({request_id: id, status: "not_found"}, 404);
await flush();
assert.equal(el("send-btn").disabled, false);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
const retry = await send("question");
assert.equal(JSON.parse(retry.options.body).request_id, id);
""", receipts=True)


def test_refresh_recovers_receipt_without_reposting_or_clearing_new_draft():
    run_page_script(r"""
await initial();
const post = await send("original");
const id = JSON.parse(post.options.body).request_id;
post.fail();
await flush();
el("message-input").value = "new unsent draft";
el("message-input").listeners.input();
events.pagehide();
createPage();
await flush();
assert.equal(new URLSearchParams(request("/api/chat/progress").url.split("?")[1]).get("include_history"), "true");
await progress(idle());
await tick();
request("/api/chat/receipts/" + id).reply({request_id: id, status: "completed", reply: "finished"});
await flush();
assert.deepEqual(replies(), ["finished"]);
assert.equal(el("message-input").value, "new unsent draft");
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
""", receipts=True)


def test_unconfirmed_receipt_cannot_silently_change_payload_and_create_new_id():
    run_page_script(r"""
await initial();
const post = await send("original");
const id = JSON.parse(post.options.body).request_id;
post.fail();
await flush();
request("/api/chat/receipts/" + id).reply({request_id: id, status: "interrupted", message: "Needs confirmation"});
await flush();
el("message-input").value = "changed";
el("message-input").listeners.input();
void el("send-btn").listeners.click();
await flush();
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
assert.equal(el("status-pill").textContent, "请先核对原提交");
assert.equal(el("release-receipt-btn").hidden, false);
""", receipts=True)


def test_page_collapses_long_user_input_without_truncating_the_post():
    run_page_script(r"""
await initial();
const text = "长文本 <script>alert(1)</script>\n".repeat(100);
const post = await send(text);
assert.equal(JSON.parse(post.options.body).message, text.trim());
const item = el("messages").children.at(-1);
assert.equal(item.className, "message user");
const details = item.children[0];
assert.match(details.children[0].textContent, /展开原始消息/);
assert.equal(details.children[1].textContent, text.trim());
assert.equal(details.open, undefined);
""", markdown=True)


def test_page_renders_terminal_reply_once_after_progress():
    run_page_script(r"""
await initial();
const post = await send("hello");
await progress(snap());
post.reply({reply: "not authoritative"});
await flush();
await tick();
await progress(snap({status: "completed", stage: "completed", sequence: 3, reply: "## 终答\n\n- 完成"}));
const items = el("messages").children.filter(item => item.className === "message assistant");
assert.equal(items.length, 1);
assert.match(items[0].children[0].innerHTML, /<h2>终答<\/h2>/);
assert.match(items[0].children[0].innerHTML, /<li>完成<\/li>/);
assert.equal(el("send-btn").disabled, false);
""", markdown=True)


def test_page_ignores_previous_terminal_and_deduplicates_final_run():
    run_page_script(r"""
await initial(snap({run_id: "run_old", status: "completed", reply: "old", sequence: 9}));
const post = await send();
assert.equal(JSON.parse(post.options.body).execution_mode, "analysis");
await progress(snap({run_id: "run_old", status: "completed", reply: "old", sequence: 9}));
assert.equal(el("send-btn").disabled, true);
assert.equal(el("cancel-run-btn").disabled, true);
assert.deepEqual(replies(), ["old"]);
await tick();
await progress(snap({run_id: "run_new", status: "running"}));
assert.equal(el("cancel-run-btn").disabled, false);
await tick();
await progress(snap({status: "completed", reply: "final", sequence: 4}));
assert.equal(el("send-btn").disabled, false);
assert.deepEqual(replies(), ["old", "final"]);
el("message-input").value = "new draft";
post.reply({reply: "final"});
await flush();
assert.deepEqual(replies(), ["old", "final"]);
assert.equal(el("message-input").value, "new draft");
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
""")


def test_page_does_not_treat_http_connection_note_as_final_answer():
    run_page_script(r"""
await initial();
const post = await send();
post.reply({reply: "Connection is uncertain; not a final answer."});
await flush();
await progress(snap({stage: "reconnecting"}));
assert.equal(el("send-btn").disabled, true);
assert.deepEqual(replies(), []);
await tick();
await progress(snap({status: "completed", reply: "real final", sequence: 3}));
assert.deepEqual(replies(), ["real final"]);
assert.equal(el("send-btn").disabled, false);
""")


def test_page_accepts_direct_reset_and_ignores_the_old_inflight_poll():
    run_page_script(r"""
await initial(snap({run_id: "run_old", status: "failed", stage: "failed", reply: "old failure"}));
const post = await send("/reset");
const stale = request("/api/chat/progress");
post.reply({reply: "Context reset.", delivery: "direct"});
await flush();
assert.deepEqual(replies(), ["old failure", "Context reset."]);
assert.equal(el("message-input").value, "");
stale.reply(snap({run_id: "run_old", status: "failed", reply: "stale failure"}));
await flush();
await progress(idle());
assert.deepEqual(replies(), ["old failure", "Context reset."]);
assert.equal(el("send-btn").disabled, false);
assert.equal(el("cancel-run-btn").disabled, true);
const next = await send("new conversation");
await progress(snap());
next.reply({reply: "new answer"});
await flush();
await tick();
await progress(snap({status: "completed", reply: "new answer", sequence: 4}));
assert.deepEqual(replies(), ["old failure", "Context reset.", "new answer"]);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 2);
""")


def test_page_reset_poll_can_be_idle_before_direct_http_reply():
    run_page_script(r"""
await initial();
const post = await send("/RESET");
await progress(idle());
post.reply({reply: "Context reset.", delivery: "direct"});
await flush();
await progress(idle());
assert.deepEqual(replies(), ["Context reset."]);
assert.equal(el("send-btn").disabled, false);
assert.equal(el("message-input").value, "");
""")


def test_page_does_not_infer_direct_success_from_reset_text_alone():
    run_page_script(r"""
await initial();
const post = await send("/reset");
post.reply({reply: "Context reset."});
await flush();
await progress(idle());
assert.deepEqual(replies(), []);
assert.equal(el("message-input").value, "/reset");
""")


def test_direct_reply_preserves_a_new_draft_and_failed_reset_is_not_success():
    run_page_script(r"""
await initial();
const post = await send("/reset");
el("message-input").value = "next draft";
post.reply({reply: "Context reset.", delivery: "direct"});
await flush();
await progress(idle());
assert.equal(el("message-input").value, "next draft");
const failed = await send("/reset");
failed.reply({detail: "Reset failed"}, 500);
await flush();
await progress(idle());
assert.deepEqual(replies(), ["Context reset."]);
assert.equal(el("message-input").value, "/reset");
""")


def test_page_waits_for_persisted_reply_after_terminal_and_http():
    run_page_script(r"""
await initial();
const post = await send();
await progress(snap({status: "completed", sequence: 3}));
assert.equal(el("send-btn").disabled, false);
post.reply({reply: "HTTP final"});
await flush();
assert.deepEqual(replies(), []);
await tick();
await progress(snap({status: "completed", reply: "persisted final", sequence: 3}));
assert.deepEqual(replies(), ["persisted final"]);
""")


def test_page_refresh_observes_history_without_posting_and_deduplicates_reply():
    run_page_script(r"""
await initial(snap({status: "running", messages: [{role: "user", content: "earlier question"}]}));
assert.equal(requests.filter(item => item.url === "/api/chat").length, 0);
assert.equal(el("send-btn").disabled, true);
events.pagehide();
createPage();
await flush();
assert.ok(request("/api/chat/progress").url.includes("include_history=true"));
await progress(snap({status: "completed", reply: "final", sequence: 4,
  messages: [{role: "user", content: "earlier question"}, {role: "assistant", content: "final", run_id: "run_new"}]}));
assert.deepEqual(replies(), ["final"]);
assert.equal(el("send-btn").disabled, false);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 0);
""")


def test_page_switch_chat_aborts_old_poll_and_ignores_old_post():
    run_page_script(r"""
await initial();
const post = await send("old draft");
const oldPoll = request("/api/chat/progress");
el("chat-id").value = "other";
void el("chat-id").listeners.change();
await flush();
assert.equal(oldPoll.options.signal.aborted, true);
await progress(idle());
post.reply({reply: "wrong chat reply"});
oldPoll.reply(snap({status: "completed", reply: "wrong poll reply"}));
await flush();
assert.deepEqual(replies(), []);
assert.equal(el("message-input").value, "");
assert.equal(el("send-btn").disabled, false);
el("chat-id").value = "room";
void el("chat-id").listeners.change();
await flush();
assert.equal(el("message-input").value, "old draft");
await progress(snap({status: "completed", reply: "right history reply", sequence: 4}));
assert.deepEqual(replies(), ["right history reply"]);
""")


def test_page_cancelling_waiting_input_keeps_observing_until_terminal():
    run_page_script(r"""
await initial(snap({status: "waiting_input", reply: "question"}));
assert.equal(el("send-btn").disabled, false);
void el("cancel-run-btn").listeners.click();
await flush();
assert.equal(el("send-btn").disabled, true);
const cancel = request("/api/chat/cancel", "POST");
assert.deepEqual(JSON.parse(cancel.options.body), {chat_id: "room", run_id: "run_new"});
cancel.reply(snap({status: "waiting_input", stage: "cancelling", reply: "question"}));
await flush();
assert.equal(el("send-btn").disabled, true);
assert.equal(el("cancel-run-btn").disabled, true);
await progress(snap({status: "waiting_input", stage: "cancelling"}));
await tick();
await progress(snap({status: "cancelled", stage: "cancelled", sequence: 3}));
assert.equal(el("send-btn").disabled, false);
assert.equal(el("status-pill").textContent, "任务已取消");
""")


def test_page_waiting_input_continuation_ignores_previous_question():
    run_page_script(r"""
await initial(snap({status: "waiting_input", reply: "question", sequence: 3}));
const post = await send("answer");
await progress(snap({status: "waiting_input", reply: "question", sequence: 3}));
assert.equal(el("send-btn").disabled, true);
await tick();
await progress(snap({status: "running", sequence: 4}));
await tick();
await progress(snap({status: "completed", reply: "final", sequence: 6}));
post.reply({reply: "final"});
await flush();
assert.deepEqual(replies(), ["question", "final"]);
assert.equal(el("send-btn").disabled, false);
""")


def test_page_transport_failure_retains_attachments_and_only_retries_get():
    run_page_script(r"""
await initial();
el("image-input").files = [{name: "photo.png", type: "image/png", size: 20}];
el("image-input").listeners.change();
const post = await send("draft");
post.fail();
await flush();
for (let i = 0; i < 4; i++) {
  request("/api/chat/progress").fail();
  await flush();
  if (i < 3) await tick();
}
assert.equal(el("resume-progress-btn").hidden, false);
assert.equal(el("send-btn").disabled, true);
assert.equal(el("message-input").value, "draft");
assert.equal(el("attachment-preview").children.length, 1);
assert.ok(!el("messages").textContent.includes("private-network-details"));
void el("resume-progress-btn").listeners.click();
await flush();
await progress(snap({status: "failed", stage: "failed", message: "Execution failed", sequence: 4}));
assert.equal(el("send-btn").disabled, false);
assert.equal(el("message-input").value, "draft");
assert.equal(el("attachment-preview").children.length, 1);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
assert.ok(![...session.values()].some(value => value.includes("aW1hZ2U=")));
""")


def test_lost_post_with_no_new_run_does_not_stay_busy_forever():
    run_page_script(r"""
const old = snap({run_id: "run_old", status: "completed", stage: "completed", reply: "old answer", sequence: 9});
await initial(old);
el("image-input").files = [{name: "photo.png", type: "image/png", size: 20}];
el("image-input").listeners.change();
const post = await send("unreceived question");
post.fail();
await flush();
for (let i = 0; i < 4; i++) {
  await progress(old);
  if (i < 3) await tick();
}
assert.equal(el("send-btn").disabled, false);
assert.equal(el("message-input").value, "unreceived question");
assert.equal(el("attachment-preview").children.length, 1);
assert.match(el("messages").textContent, /未确认本次提交/);
assert.deepEqual(replies(), ["old answer"]);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
assert.equal(el("resume-progress-btn").hidden, false);
""")


def test_lost_post_can_still_discover_a_delayed_accepted_run():
    run_page_script(r"""
await initial();
const post = await send("accepted but response lost");
post.fail();
await flush();
await progress(idle());
await tick();
await progress(snap({run_id: undefined, status: "queued", stage: "reconnecting"}));
await tick();
await progress(snap());
assert.equal(el("send-btn").disabled, true);
await tick();
await progress(snap({status: "completed", stage: "completed", reply: "real answer", sequence: 3}));
assert.deepEqual(replies(), ["real answer"]);
assert.equal(el("message-input").value, "");
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
""")


def test_disconnected_submission_does_not_show_previous_run_as_current():
    run_page_script(r"""
await initial(snap({run_id: "old", status: "completed", stage: "completed", elapsed_seconds: 289, reply: "old answer"}));
const post = await send("new question");
assert.equal(el("run-stage").textContent, "提交中");
assert.ok(!el("run-time").textContent.includes("289"));
post.fail();
await flush();
for (let i = 0; i < 4; i++) {
  request("/api/chat/progress").fail();
  await flush();
  if (i < 3) await tick();
}
assert.equal(el("status-pill").textContent, "进度更新已暂停");
assert.equal(el("send-btn").textContent, "待确认");
assert.equal(el("run-stage").textContent, "状态待确认");
assert.equal(el("send-btn").disabled, true);
""")


def test_page_poll_requests_are_single_in_flight_timeout_bounded_and_capped():
    run_page_script(r"""
await initial();
await send();
const first = request("/api/chat/progress");
assert.equal([...timers.values()].filter(timer => timer.delay === 1500).length, 0);
const timeout = [...timers].find(([, timer]) => timer.delay === 8000);
assert.ok(timeout);
timers.delete(timeout[0]);
timeout[1].fn();
await flush();
assert.equal(first.options.signal.aborted, true);
await tick();
await progress(snap());
evaluate("active.attempts = MAX_POLLS");
await tick();
assert.equal(el("resume-progress-btn").hidden, false);
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
""")


def test_page_non_powercore_keeps_legacy_payload_and_failed_draft():
    run_page_script(r"""
el("image-input").files = [{name: "photo.png", type: "image/png", size: 20}];
el("image-input").listeners.change();
const post = await send("draft");
assert.equal("execution_mode" in JSON.parse(post.options.body), false);
post.reply({detail: "private-api-key"}, 500);
await flush();
assert.equal(el("send-btn").disabled, false);
assert.equal(el("message-input").value, "draft");
assert.equal(el("attachment-preview").children.length, 1);
assert.ok(!el("messages").textContent.includes("private-api-key"));
assert.equal(requests.filter(item => item.url.startsWith("/api/chat/progress")).length, 0);
""", supports_progress=False)


def test_page_standard_mode_and_recovered_budget():
    run_page_script(r"""
await initial();
el("execution-mode").value = "standard";
el("execution-mode").listeners.change();
const post = await send();
assert.equal(JSON.parse(post.options.body).execution_mode, "standard");
await progress(snap({wall_time_ms: 180000, elapsed_seconds: 16.9}));
assert.equal(el("run-time").textContent, "已耗时 16 秒 / 预算 3 分钟");
events.pagehide();
createPage();
await flush();
await progress(snap({wall_time_ms: 180000}));
assert.equal(el("execution-mode").value, "standard");
assert.equal(el("execution-mode").disabled, true);
""")


def test_legacy_terminal_duration_is_displayed_as_unknown():
    client = TestClient(create_web_app(message_handler=AsyncMock(), progress_handler=AsyncMock(
        return_value=snapshot(status="completed", elapsed_seconds=None))))
    response = client.get("/api/chat/progress?chat_id=legacy")
    assert response.status_code == 200
    assert response.json().get("elapsed_seconds") is None
    run_page_script(r"""
await initial(snap({status: "completed", elapsed_seconds: null}));
assert.equal(el("run-time").textContent, "耗时未记录 / 预算 10 分钟");
""")


def test_page_cancel_network_error_only_resumes_get():
    run_page_script(r"""
await initial(snap());
await tick();
const oldPoll = request("/api/chat/progress");
void el("cancel-run-btn").listeners.click();
await flush();
assert.equal(oldPoll.options.signal.aborted, true);
request("/api/chat/cancel", "POST").fail();
await flush();
await progress(snap({status: "cancelled", sequence: 4}));
assert.equal(el("status-pill").textContent, "任务已取消");
assert.equal(el("send-btn").disabled, false);
assert.equal(requests.filter(item => item.url === "/api/chat/cancel").length, 1);
""")


def test_page_old_terminal_after_http_end_does_not_duplicate_previous_reply():
    run_page_script(r"""
await initial(snap({run_id: "run_old", status: "completed", reply: "old", sequence: 9}));
const post = await send();
post.reply({reply: "request did not start a new run"});
await flush();
await progress(snap({run_id: "run_old", status: "completed", reply: "old", sequence: 9}));
assert.equal(el("send-btn").disabled, false);
assert.deepEqual(replies(), ["old"]);
assert.equal(el("message-input").value, "hello");
assert.equal(requests.filter(item => item.url === "/api/chat").length, 1);
""")


def test_page_drafts_survive_refresh_and_are_credential_scoped():
    run_page_script(r"""
await initial();
el("message-input").value = "private draft";
el("message-input").listeners.input();
events.pagehide();
createPage();
await flush();
await progress(idle());
assert.equal(el("message-input").value, "private draft");
el("auth-token").value = "other-token";
void el("auth-token").listeners.change();
await flush();
await progress(idle());
assert.equal(el("message-input").value, "");
assert.ok(![...session.keys()].some(key => key.includes("other-token")));
assert.equal(requests.filter(item => item.url === "/api/chat").length, 0);
""")


def test_page_new_submission_ignores_late_reply_from_previous_post():
    run_page_script(r"""
await initial();
const first = await send("first question");
await progress(snap({run_id: "run_first", status: "completed", reply: "first final", sequence: 4}));
const second = await send("second question");
first.reply({reply: "late first final"});
await flush();
assert.equal(el("send-btn").disabled, true);
await progress(snap({run_id: "run_second", status: "completed", reply: "second final", sequence: 4}));
second.reply({reply: "second final"});
await flush();
assert.deepEqual(replies(), ["first final", "second final"]);
""")
