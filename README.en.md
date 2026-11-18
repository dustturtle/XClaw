# XClaw

[中文手册](README.md) · [Reproduce the demo](docs/demo/README.md) · [Contributing](CONTRIBUTING.md)

XClaw is a self-hosted Python agent for chat, task automation and market research. Ask questions in the web client or connect a messaging adapter. The investment tools cover quotes and history for supported mainland Chinese, Hong Kong and US securities, plus selected domestic futures. Availability varies by instrument and provider.

![The XClaw chat client showing a Chinese Markdown table comparing three synthetic daily closes](docs/demo/chat.png)

The screenshot uses a fixed question and answer. The real chat renderer formats the table; the fixture contacts neither a model nor a market-data provider.

## Run locally

Use Python 3.12 or newer. The Dockerfile on this branch uses Python 3.11, below the package requirement.

~~~sh
git clone https://github.com/dustturtle/XClaw.git
cd XClaw
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
xclaw setup
xclaw start --config xclaw.config.yaml
~~~

Choose a provider supported by your environment, its model ID and the matching API endpoint. Hosted providers may require a key; Ollama requires a running local model. Open **http://127.0.0.1:8080/** when using the default Web port. Start with a simple text question, then ask for a price history with an explicit symbol and date range.

## How a request runs

1. A channel adapter passes the message and chat identity to the runtime.
2. The configured model requests tools as needed. For mainland Chinese historical prices, the source chain tries pytdx, baostock, yfinance and AkShare. A quote, a historical close and an intraday observation may have different timestamps.
3. The agent receives tool results and returns an answer through the originating channel. SQLite stores local chat and business records. Optional skills, MCP servers and scheduled tasks extend this path.

The Chinese manual covers model configuration, channels, tools, backtests, storage and scheduling. Keep the Web listener on loopback for a local trial. Configure authentication before sharing the service. A hosted model can receive prompts, conversation context and tool results; data providers receive the instruments queried. Review those boundaries before entering holdings or attaching files.

## Build and test

~~~sh
uv sync --extra dev
uv build
uv run python -m pytest tests/ -v
~~~

[GitHub Actions](https://github.com/dustturtle/XClaw/actions/workflows/uv-ci.yml) reports build and test results per run. Provider credentials, market hours and upstream availability affect live behavior. [MIT license](LICENSE).
