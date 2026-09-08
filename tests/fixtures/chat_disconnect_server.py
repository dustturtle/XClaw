"""Browser-only recovery fixture; no model, database or business tools are called."""

from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import socket
import sys
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from xclaw.channels.web import _build_chat_page_html


class Handler(BaseHTTPRequestHandler):
    posts = 0
    fail_polls = 0

    def drop_connection(self):
        self.close_connection = True
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def respond(self, body, media_type="application/json"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self.respond(_build_chat_page_html(
                auth_enabled=False, model_name="disconnect-fixture", provider_name="test",
                supports_progress=True,
            ).encode(), "text/html; charset=utf-8")
        elif path.startswith("/chat-assets/"):
            name = path.removeprefix("/chat-assets/")
            if name not in {"chat.css", "markdown.js", "markdown-it.min.js"}:
                self.send_error(404)
                return
            self.respond((ROOT / "xclaw/channels/static" / name).read_bytes(),
                         "text/css" if name.endswith(".css") else "text/javascript")
        elif path == "/api/chat/progress":
            if Handler.fail_polls:
                Handler.fail_polls -= 1
                self.drop_connection()
                return
            self.respond({
                "run_id": "run_previous", "status": "completed", "stage": "completed",
                "message": "Previous task completed", "elapsed_seconds": 289,
                "wall_time_ms": 600000, "sequence": 9, "reply": "Previous answer",
                "messages": [{"role": "assistant", "content": "Previous answer"}],
            })
        elif path == "/verification":
            self.respond({"posts": Handler.posts, "runs_created": 0})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        Handler.posts += 1
        Handler.fail_polls = 4
        self.drop_connection()


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8082), Handler).serve_forever()
