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
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}
function fetchMock(url, options = {}) {
  if (String(url) === "/api/chat/models") return Promise.resolve({ok:true, json:async()=>({model_options:["codex/fixture-default","codex/fixture-b","codex/fixture-a"],default_model:"codex/fixture-default"})});
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
      createElement() { return new Element(); },
      createTextNode(text) { const element = new Element(); element.textContent = text; return element; }
    },
    localStorage: storage(local), sessionStorage: storage(session),
    window: {location: {search: ""}, addEventListener(name, callback) { events[name] = callback; }},
    crypto: {randomUUID: require("node:crypto").randomUUID, subtle: {async digest(_type, data) { return createHash("sha256").update(data).digest(); }}},
    ResizeObserver: class { observe() {} }, MutationObserver: class { observe() {} },
    requestAnimationFrame: fn => setImmediate(fn), performance,
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


def run_page_script(scenario, *, supports_progress=True, markdown=False, receipts=False, openui=False, prepare=False):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for deterministic browser-script tests")
    page = _build_chat_page_html(
        auth_enabled=False, model_name="test", provider_name="test",
        supports_images=True, supports_progress=supports_progress,
        supports_receipts=receipts, supports_openui=openui, supports_prepare=prepare,
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


def test_failed_receipt_does_not_generate_ui_and_keeps_draft():
    run_page_script(r"""
await initial();
evaluate("window.uiCalls=0; window.XClawOpenUIHost={attach(){window.uiCalls++},clear(){}};");
const post = await send("请分析，不要重放工具");
const payload=JSON.parse(post.options.body);
post.reply({request_id:payload.request_id,status:"accepted"},202);
await flush();
const outcome={request_id:payload.request_id,status:"failed",reply:"模型暂时不可用"};
evaluate("applyReceipt(active,"+JSON.stringify(outcome)+")");
assert.equal(evaluate("window.uiCalls"),0);
assert.equal(el("message-input").value,"请分析，不要重放工具");
assert.equal(replies().at(-1),"模型暂时不可用");
assert.equal(el("send-btn").disabled,false);
assert.equal(evaluate("active.pending.receiptFinished"),true);
assert.equal(requests.filter(r=>r.options.method==="POST").length,1);
""",receipts=True,openui=True)


def test_failed_history_skips_ui_but_normal_negative_business_reply_does_not():
    run_page_script(r"""
evaluate("window.uiCalls=0; window.XClawOpenUIHost={attach(){window.uiCalls++},clear(){}};");
await initial(snap({status:"completed",stage:"completed",messages:[
 {role:"user",content:"问题"}, {role:"assistant",content:"模型暂时不可用",status:"failed"},
 {role:"assistant",content:"没有符合条件的股票"}]}));
assert.equal(evaluate("window.uiCalls"),1);
assert.deepEqual(replies(),["模型暂时不可用","没有符合条件的股票"]);
""",openui=True)


def test_write_recovery_panel_requires_explicit_review_and_never_replays():
    run_page_script(r"""
await initial();
request("/api/chat/write-recovery").reply({records: [{id:"operation", tool_name:"portfolio_manage", arguments_json:'{"symbol":"600519"}', state:"unknown", created_at:"2026-11-17 08:00:00"}]});
await flush();
assert.equal(el("write-recovery").hidden, false);
const section = el("write-recovery-records").children[0];
const first = section.children[1].children[0], second = section.children[2].children[0];
const note = section.children[3], button = section.children[4];
assert.equal(button.disabled, true);
first.checked = true; first.listeners.change();
second.checked = true; second.listeners.change();
assert.equal(button.disabled, true);
note.value = "核对到已生效的一条记录，执行已停止"; note.listeners.input();
assert.equal(button.disabled, false);
void button.listeners.click(); await flush();
const ack = request("/api/chat/write-recovery/acknowledge", "POST");
assert.deepEqual(JSON.parse(ack.options.body), {chat_id:"room", execution_id:"operation", confirmed_reviewed:true, execution_stopped:true, note:note.value});
ack.reply({status:"acknowledged",message:"已记录核对；未重试，不表示原操作成功。"}); await flush();
request("/api/chat/write-recovery").reply({records:[]}); await flush();
assert.equal(el("write-recovery").hidden, true);
assert.equal(requests.filter(r => r.options.method === "POST").length, 1);
assert.match(el("messages").textContent, /不表示原操作成功/);
""")


@pytest.mark.parametrize("last_failed", [True, False])
def test_native_history_restores_exact_latest_failure_status(last_failed):
    run_page_script(r"""
await flush();
const records=[{role:"assistant",content:"同一段普通否定文字",status:"failed"}];
if (!LAST_FAILED) records.push({role:"assistant",content:"同一段普通否定文字"});
request("/api/chat/history").reply({messages:records}); await flush();
assert.equal(el("status-pill").textContent, LAST_FAILED ? "上次任务未完成" : "准备就绪");
assert.equal(el("send-btn").disabled,false);
assert.equal(requests.filter(r=>r.options.method==="POST").length,0);
const post=await send("新问题");
assert.equal(el("status-pill").textContent,"正在提交");
assert.equal(evaluate("active.historyFailed"),false);
""".replace("LAST_FAILED", "true" if last_failed else "false"), supports_progress=False, receipts=True)


def test_idle_progress_preserves_latest_history_failure_without_blocking_send():
    run_page_script(r"""
await initial({...idle(), messages:[{role:"assistant",content:"失败正文",status:"failed"}]});
assert.equal(el("status-pill").textContent,"上次任务未完成");
assert.equal(el("send-btn").disabled,false);
assert.match(el("status-pill").style,/9f331c/);
""")


@pytest.mark.parametrize("strategy", [True, False])
def test_recovery_explains_strategy_local_write_without_generalizing_to_other_tools(strategy):
    run_page_script(r"""
await initial();
request("/api/chat/write-recovery").reply({records:[{id:"untouched", tool_name:STRATEGY ? "strategy_scan" : "submit_order", arguments_json:'{"symbol":"600519","note":"<script>bad</script>"}', state:"unknown", created_at:"2026-11-17 08:00:00"}]});
await flush();
const info=el("write-recovery-records").children[0].children[0];
assert.match(info.children[0].textContent,/600519.*待核对/);
assert.doesNotMatch(info.children[0].textContent,/unknown/);
assert.equal(info.children[3].open,undefined);
assert.equal(info.children[3].children[1].textContent,'{"symbol":"600519","note":"<script>bad</script>"}');
if (STRATEGY) {
 assert.match(info.textContent,/策略扫描（本地分析记录）/);
 assert.match(info.textContent,/不是买入、卖出或下单/);
 assert.match(info.textContent,/是否已经保存尚未确认/);
} else {
 assert.match(info.textContent,/submit_order/);
 assert.doesNotMatch(info.textContent,/不是买入|本地分析记录/);
}
assert.equal(requests.filter(r=>r.options.method==="POST").length,0);
""".replace("STRATEGY", "true" if strategy else "false"))



def test_native_prepare_selector_visible_and_defaults_standard():
    from bs4 import BeautifulSoup
    page = BeautifulSoup(_build_chat_page_html(auth_enabled=False,model_name='test',provider_name='test',
        supports_progress=False,supports_prepare=True), 'html.parser')
    selector = page.select_one('#execution-mode')
    assert not selector.find_parent(class_='field').has_attr('hidden')
    assert selector.select_one('option[selected]')['value'] == 'standard'
    assert selector.select_one('option[value="prepare"]') is not None
    assert page.select_one('#run-controls').has_attr('hidden')
    run_page_script(r"""
await flush();assert.equal(el("execution-mode").value,"standard");
el("execution-mode").value="prepare";
const post=await send("仅准备操作");
assert.equal(JSON.parse(post.options.body).execution_mode,"prepare");
post.reply({reply:"准备完成"});await flush();
""",supports_progress=False,prepare=True)


def test_model_selection_is_frozen_per_request_and_scoped_to_chat():
    run_page_script(r'''
await initial(idle());
assert.equal(el("model-select").value,"codex/fixture-default");
el("model-select").value="codex/fixture-a";el("model-select").listeners.change();
const post=await send("read only");
assert.equal(JSON.parse(post.options.body).model,"codex/fixture-a");
assert.equal(el("model-select").disabled,true);
el("model-select").value="codex/fixture-b";
assert.equal(JSON.parse(post.options.body).model,"codex/fixture-a");
el("chat-id").value="other";el("chat-id").listeners.change();await flush();
assert.equal(el("model-select").value,"codex/fixture-default");
await progress(idle());
post.reply({reply:"stale"});await flush();
assert.equal(el("model-select").value,"codex/fixture-default");
''')


def test_model_rejection_is_explicit_without_leaking_backend_details():
    run_page_script(r'''
await flush();
el("model-select").value="codex/fixture-a";el("model-select").listeners.change();
const post=await send("read only");
post.reply({detail:{code:"model_unavailable",message:"private-api-key"}},400);await flush();
assert.match(el("messages").textContent,/所选模型不可用，未自动切换模型/);
assert.ok(!el("messages").textContent.includes("private-api-key"));
assert.equal(el("model-select").value,"codex/fixture-a");
''',supports_progress=False)


def test_legacy_receipt_retry_preserves_original_fingerprint_without_model():
    run_page_script(r'''
await initial(idle());
const original={chat_id:"room",message:"legacy question",attachments:[],execution_mode:"analysis"};
const fingerprint=createHash("sha256").update(JSON.stringify(original)).digest("hex");
const oldId="a".repeat(32);
evaluate("active.pending="+JSON.stringify({requestId:oldId,fingerprint,mode:"analysis",draftText:"legacy question",files:[],settled:true})+"; controls(active)");
assert.equal(el("model-select").disabled,true);
// Tampering with the draft still cannot reuse or replace the original request ID.
el("message-input").value="changed question";
void el("send-btn").listeners.click();await flush();
assert.equal(requests.filter(r=>r.url==="/api/chat").length,0);
assert.equal(evaluate("active.pending.requestId"),oldId);
assert.match(el("progressMessageEl")?.textContent || el("run-message").textContent,/保持原文字/);
const retry=await send("legacy question");
const body=JSON.parse(retry.options.body);
assert.equal(body.request_id,oldId);
assert.equal("model" in body,false);
delete body.request_id;assert.deepEqual(body,original);
''',receipts=True)


def test_model_badges_distinguish_requested_from_verified_response_and_history():
    run_page_script(r'''
await initial(idle());
evaluate('appendMessage("assistant","rejected",[],false,false,{model:"codex/fixture-a"})');
assert.equal(el("messages").children.at(-1)["data-model"],undefined);
evaluate('appendMessage("assistant","local command",[],false,true,{model:"codex/fixture-a"})');
assert.equal(el("messages").children.at(-1)["data-model"],undefined);
evaluate('appendMessage("assistant","verified",[],false,true,{model:"requested",response_model:"codex/fixture-b"})');
assert.equal(el("messages").children.at(-1)["data-model"],"codex/fixture-b");
evaluate('appendMessage("assistant","old history",[],true,true,{})');
assert.equal(el("messages").children.at(-1)["data-model"],undefined);
evaluate('appendMessage("assistant","verified history",[],true,true,{model:"codex/fixture-a"})');
assert.equal(el("messages").children.at(-1)["data-model"],"codex/fixture-a");
// Inline final badge likewise requires the verified response_model parameter.
evaluate('active.liveBubble=document.createElement("article");active.inlinePresentation={};active.liveRequestId="one";');
evaluate('finishInline(active,"answer","completed","codex/fixture-b")');
assert.equal(evaluate('active.liveBubble["data-model"]'),"codex/fixture-b");
''')
