"""Reading-first layout and the actual vendored Markdown trust boundary."""

import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import AsyncMock

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
import pytest

from xclaw.channels.web import _build_chat_page_html, create_web_app


STATIC = Path(__file__).resolve().parents[1] / "xclaw/channels/static"


def render_markdown(text):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required to test the actual browser Markdown bundle")
    script = """
const fs = require("node:fs"), vm = require("node:vm");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
const ctx = vm.createContext({atob});
vm.runInContext(fs.readFileSync(input.bundle, "utf8"), ctx);
vm.runInContext(fs.readFileSync(input.renderer, "utf8"), ctx);
process.stdout.write(ctx.renderChatMarkdown(input.text));
"""
    result = subprocess.run(
        [node, "-e", script],
        input=json.dumps({
            "bundle": str(STATIC / "markdown-it.min.js"),
            "renderer": str(STATIC / "markdown.js"), "text": text,
        }), text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return BeautifulSoup(result.stdout, "html.parser")


def test_markdown_renders_headings_lists_tables_quotes_and_code():
    doc = render_markdown(
        "# 结论\n\n**重点**与*说明*\n\n- 第一项\n- 第二项\n\n"
        "1. 第一步\n2. 第二步\n\n> 引用\n\n"
        "| 资产 | 比例 |\n|---|---:|\n| 现金 | 25% |\n\n"
        "\x60\x60\x60python\nprint('<not-html>')\n\x60\x60\x60\n\n行内 \x60x < 3\x60\n\n---"
    )
    assert doc.h1.text == "结论"
    assert doc.strong.text == "重点"
    assert doc.em.text == "说明"
    assert len(doc.select("ul li")) == len(doc.select("ol li")) == 2
    assert doc.blockquote.text.strip() == "引用"
    table = doc.select_one(".table-scroll")
    assert table["tabindex"] == "0"
    assert table["aria-label"] == "表格"
    assert table.select("td")[1].text == "25%"
    assert table.select("td")[1]["style"] == "text-align:right"
    assert doc.pre.code.text == "print('<not-html>')\n"
    assert "language-python" in doc.pre.code["class"]
    assert doc.find("not-html") is None
    assert doc.hr is not None


@pytest.mark.parametrize("payload", [
    '<script>alert(1)</script><img src=x onerror=alert(1)>',
    '<svg onload=alert(1)><a href="javascript:alert(1)">x</a></svg>',
    "[x](javascript:alert%281%29)",
    "[x](JaVaScRiPt:alert%281%29)",
    "[x](jav&#x61;script:alert%281%29)",
    "[x](vbscript:msgbox%281%29)",
    "[x](file:///etc/passwd)",
    "[x](data:text/html;base64,PHNjcmlwdD4=)",
    "![tracking](https://tracker.invalid/pixel.png)",
    "![svg](data:image/svg+xml;base64,PHN2Zz4=)",
    "\x60\x60\x60\"><img src=x onerror=alert(1)>\n<svg onload=alert(1)>\n\x60\x60\x60",
])
def test_untrusted_markdown_cannot_execute_or_load_remote_images(payload):
    doc = render_markdown(payload)
    assert not doc.select("script, iframe, img, svg, style, object, embed, form")
    for tag in doc.find_all(True):
        assert not any(attr.lower().startswith("on") for attr in tag.attrs)
        assert not tag.get("href", "").lower().startswith(("javascript:", "vbscript:", "data:", "file:"))


def test_links_are_readable_but_cannot_control_the_opener():
    doc = render_markdown("[来源](https://example.com/a?q=1&b=2)")
    assert doc.a.text == "来源"
    assert doc.a["href"] == "https://example.com/a?q=1&b=2"
    assert doc.a["target"] == "_blank"
    assert set(doc.a["rel"]) == {"noopener", "noreferrer"}


def test_controls_are_discoverable_without_consuming_the_reading_area():
    doc = BeautifulSoup(_build_chat_page_html(
        auth_enabled=False, model_name="<test-model>", provider_name="test",
        supports_progress=True, supports_images=True,
    ), "html.parser")
    settings = doc.select_one("details#chat-settings")
    assert not settings.has_attr("open")
    for name in ["chat-id", "auth-token", "execution-mode", "clear-btn"]:
        assert settings.select_one("#" + name)
    composer = doc.select_one(".composer")
    assert composer.select_one("#message-input")["rows"] == "2"
    assert not composer.select("#chat-id, #auth-token, #execution-mode")
    assert composer.select_one("#cancel-run-btn")
    assert composer.select_one("#resume-progress-btn")
    assert composer.select_one("#image-input")
    assert not doc.select(".sidebar")
    assert not doc.find("test-model")
    assert "<test-model>" in doc.select_one(".model-name").text
    assert all(s["src"].startswith("/chat-assets/") for s in doc.select("script[src]"))


def test_local_assets_are_served_and_only_allowlisted_files_are_public():
    client = TestClient(create_web_app(message_handler=AsyncMock(return_value="ok")))
    for name, content_type in [
        ("chat.css", "text/css"), ("markdown-it.min.js", "javascript"), ("markdown.js", "javascript")
    ]:
        response = client.get("/chat-assets/" + name)
        assert response.status_code == 200
        assert content_type in response.headers["content-type"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.content == (STATIC / name).read_bytes()
    for name in ["web.py", "xclaw.config.yaml", "THIRD_PARTY.txt", "..%2Fweb.py"]:
        assert client.get("/chat-assets/" + name).status_code == 404
