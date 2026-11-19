# Reproduce the chat screenshot

![Synthetic three-day price comparison in the XClaw web chat](chat.png)

This fixture loads the project's own chat page, stylesheet and Markdown renderer. It supplies one fixed question and answer directly to the message renderer. It exercises presentation only. There is no server, database, live quote, tool execution or model call.

After installing the project in a virtual environment, run from the repository root with a local Chrome or Chromium executable:

~~~sh
python docs/demo/render.py --chrome /path/to/chromium --output docs/demo/chat.png
~~~

Chrome uses a temporary browser profile and renders a 1120 × 760 viewport. Fonts can differ between operating systems. The prices 100.00, 103.00 and 101.97 are synthetic. The daily percentage changes are +3% and −1%.

## Replay the homepage stories

Install the checkout using the steps above. Set CHROME to the path of your Chromium executable. Each command creates a fresh browser profile and SQLite database. The prompts select fixed tool calls and the Web client renders their returned text. This exercises storage, retrieval and presentation without model inference or a running server.

~~~sh
python docs/demo/render.py --chrome "$CHROME" --story watchlist --output docs/demo/watchlist.png
python docs/demo/render.py --chrome "$CHROME" --story portfolio --output docs/demo/portfolio.png
~~~

## Replay the moving-average comparison

Install the project and Pillow in your virtual environment. From the checkout run:

~~~sh
python docs/demo/backtest.py --chrome "$CHROME"
~~~

The script injects deterministic weekday closes (including pre-period warmup) at the historical data boundary, runs the actual 10/30 SMA backtest, prints its report and captures three stages in a temporary Chrome profile. The follow-up shows the ledger from the same tool result. The dates follow weekdays rather than an exchange holiday calendar. No live quotes, language model, messaging account or server is involved.

**如何读交易记录。** 策略在预热期已开始交易。2 月 28 日以 105.96 买入的模拟持仓延续至 3 月 3 日，因此区间内第一条记录是 4 月 2 日以 103.15 卖出。引擎用当天收盘价计算均线信号，并假设以同一收盘价全仓成交，未计手续费和滑点。策略收益比较 3 月 3 日与 5 月 30 日收盘时的账户净值；买入持有基准比较这两天的合成收盘价。回撤也只在这个区间内计算。

**Reading the ledger.** The strategy trades during warmup. Its February 28 purchase at 105.96 was still open on March 3, so the first entry in the reporting window is the April 2 sale at 103.15. The engine computes each signal at the close and assumes a full-position fill at that same close, with zero fees and slippage. Strategy return compares closing account equity on March 3 and May 30. Buy-and-hold compares the synthetic closing prices on those same dates. Drawdown is measured within that window.
