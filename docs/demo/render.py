"""Render the public chat UI with a synthetic Markdown conversation, offline."""
import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

from xclaw.channels import web

QUESTION = "比较这组三天的示例收盘价。哪一天涨幅最大？"
ANSWER = """### 三日收盘对比

| 交易日 | 收盘（点） | 较前日 |
| --- | ---: | ---: |
| 第一天 | 100.00 | — |
| 第二天 | 103.00 | +3.00% |
| 第三天 | 101.97 | −1.00% |

第二天涨幅最大。第三天虽回落，收盘仍比第一天高 **1.97%**。

以上数值为演示用合成数据。"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, help="Path to Chrome or Chromium")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--openui", action="store_true", help="Render the synthetic history card fixture")
    args = parser.parse_args()
    page = web._build_chat_page_html(auth_enabled=False, model_name="演示样本", provider_name="fixture",
                                    supports_openui=args.openui)
    page = page.replace("<head>", """<head><script>
    window.fetch = async url => {
      if (url === "/api/chat/models") return new Response(JSON.stringify({
        model_options: ["演示样本"], default_model: "演示样本"
      }));
      throw new Error("Offline fixture: no network requests");
    };
    </script>""")
    static = Path(web.__file__).with_name("static")
    page = re.sub(r'"/chat-assets/([^"?]+)(?:\?[^"]*)?"',
                  lambda match: json.dumps((static / match[1]).as_uri()), page)
    fixture = json.dumps([QUESTION, ANSWER], ensure_ascii=False)
    script = f"""
    const [question, answer] = {fixture};
    appendMessage("user", question);
    appendMessage("assistant", answer);
    """
    if args.openui:
        script = Path(__file__).with_name("openui-fixture.js").read_text()
    page = page.replace("</body>", f"<script>{script}</script></body>")
    output = args.output or Path("docs/demo/openui.png" if args.openui else "docs/demo/chat.png")
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        html = scratch / "chat.html"
        html.write_text(page, encoding="utf-8")
        subprocess.run([args.chrome, "--headless", "--disable-background-networking",
                        "--disable-extensions", "--hide-scrollbars",
                        "--user-data-dir=" + str(scratch / "profile"),
                        "--window-size=1120,1200" if args.openui else "--window-size=1120,760", "--virtual-time-budget=1000",
                        "--screenshot=" + str(output.resolve()), html.as_uri()], check=True)


if __name__ == "__main__":
    main()
