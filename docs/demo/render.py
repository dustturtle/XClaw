"""Render the public chat UI with a synthetic Markdown conversation, offline."""
import asyncio
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


async def story(name):
    from xclaw.db import Database
    from xclaw.tools import ToolContext
    from xclaw.tools.portfolio import PortfolioManageTool
    from xclaw.tools.watchlist import WatchlistManageTool

    db = Database(":memory:")
    await db.connect()
    try:
        context = ToolContext(chat_id=1, channel="web", db=db)
        if name == "watchlist":
            tool = WatchlistManageTool()
            steps = [
                ("记下 DEMO，周五复核成交量。", dict(action="add", symbol="DEMO", notes="周五复核成交量")),
                ("回看自选：我想核对什么？", dict(action="list")),
            ]
        else:
            tool = PortfolioManageTool()
            steps = [
                ("模拟买入 DEMO：20 股，每股 12 元。", dict(action="buy", symbol="DEMO", shares=20, price=12)),
                ("这笔模拟记录的总成本是多少？", dict(action="view")),
            ]
        turns = []
        for question, params in steps:
            result = await tool.execute(params, context)
            if result.is_error:
                raise RuntimeError(result.content)
            turns.append([question, result.content])
        return turns
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", required=True, help="Path to Chrome or Chromium")
    parser.add_argument("--output", type=Path, default=Path("docs/demo/chat.png"))
    parser.add_argument("--story", choices=["watchlist", "portfolio"])
    args = parser.parse_args()
    page = web._build_chat_page_html(auth_enabled=False, model_name="演示样本", provider_name="fixture")
    static = Path(web.__file__).with_name("static")
    page = re.sub(r'"/chat-assets/([^"?]+)(?:\?[^"]*)?"',
                  lambda match: json.dumps((static / match[1]).as_uri()), page)
    fixture = json.dumps(asyncio.run(story(args.story)) if args.story else [[QUESTION, ANSWER]], ensure_ascii=False)
    page = page.replace("</body>", f"""<script>
    for (const [question, answer] of {fixture}) {{
    appendMessage("user", question);
    appendMessage("assistant", answer);
    }}
    </script></body>""")
    with tempfile.TemporaryDirectory() as directory:
        scratch = Path(directory)
        html = scratch / "chat.html"
        html.write_text(page, encoding="utf-8")
        subprocess.run([args.chrome, "--headless", "--disable-background-networking",
                        "--disable-extensions", "--hide-scrollbars",
                        "--user-data-dir=" + str(scratch / "profile"),
                        "--window-size=1120,760", "--virtual-time-budget=1000",
                        "--screenshot=" + str(args.output.resolve()), html.as_uri()], check=True)


if __name__ == "__main__":
    main()
