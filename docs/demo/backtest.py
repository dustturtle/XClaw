"""Render a fixed strategy comparison using main's backtest tool and Web client."""
import argparse
import asyncio
import json
import math
import subprocess
import tempfile
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from xclaw.channels import web
from xclaw.tools import ToolContext
from xclaw.tools.stock_backtest import StockBacktestTool


async def synthetic_closes(symbol, market, start, end, loop):
    dates, closes = [], []
    day = date.fromisoformat(start)
    while day.isoformat() <= end:
        if day.weekday() < 5:
            i = (day - date(2025, 1, 1)).days
            dates.append(day.isoformat())
            closes.append(round(100 + i / 12 + 7 * math.sin(i / 9), 2))
        day += timedelta(days=1)
    return closes, dates


async def report():
    tool = StockBacktestTool()
    params = dict(symbol="DEMO", market="CN", strategy="sma_cross",
                  start_date="2025-03-03", end_date="2025-05-30")
    with patch.object(tool, "_fetch_closes", synthetic_closes):
        result = await tool.execute(params, ToolContext(1, "web"))
    if result.is_error:
        raise RuntimeError(result.content)
    return result.content


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--chrome", required=True)
    chrome = parser.parse_args().chrome
    result = asyncio.run(report())
    print(result)
    question = "合成样本 DEMO：3 月到 5 月，按 10/30 日均线换仓，比一直持有好吗？中途回撤多少？"
    metrics = result.split("── 交易记录")[0].strip()
    ledger = result.split("── 交易记录")[1].split("⚠️")[0].strip()
    turns = [("user", question), ("assistant", metrics),
             ("user", "这些收益对应哪些买卖？"), ("assistant", "交易记录" + ledger)]
    base = web._build_chat_page_html(auth_enabled=False, model_name="合成回放", provider_name="fixture")
    assets = Path(web.__file__).with_name("static").as_uri()
    base = base.replace('"/chat-assets/', '"' + assets + '/')
    output = Path(__file__).parent
    frames = []
    with tempfile.TemporaryDirectory() as directory:
        tmp = Path(directory)
        for count in (1, 2, 4):
            script = f"for (const [role, text] of {json.dumps(turns[:count])}) appendMessage(role, text);"
            html = tmp / "demo.html"
            html.write_text(base.replace("</body>", f"<script>{script}</script></body>"))
            png = tmp / "frame.png"
            subprocess.run([chrome, "--headless", "--disable-background-networking", "--disable-extensions",
                            "--hide-scrollbars", "--window-size=960,900", "--virtual-time-budget=1000",
                            f"--user-data-dir={tmp / 'profile'}", f"--screenshot={png}", html.as_uri()], check=True)
            with Image.open(png) as img:
                frames.append(img.convert("RGB"))
            if count == 2:
                frames[-1].save(output / "backtest.png", optimize=True)
    frames[0].save(output / "backtest.gif", save_all=True, append_images=frames[1:],
                   duration=[2500, 6000, 5000], loop=0, optimize=True)


if __name__ == "__main__":
    main()
