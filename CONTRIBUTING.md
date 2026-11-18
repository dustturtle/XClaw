# Contributing to XClaw

欢迎用中文或英文反馈。Issues and pull requests are welcome in Chinese or English.

For a bug, include the commit or version, Python version, operating system, provider type, minimal reproduction, expected result and actual result. For a market-data issue, include a public symbol, market, interval, timezone and the source shown in the answer. Replace private conversations with a short synthetic example.

Keep API keys, authentication headers, personal holdings, databases and raw logs out of public issues and screenshots. For security-sensitive findings, use GitHub private vulnerability reporting if the repository offers it.

For a change, create a branch from the current default branch, keep the diff focused, and add or update a regression test for the affected behavior. Run the relevant tests locally. The repository workflow runs the following build and test sequence:

~~~sh
uv sync --extra dev
uv build
uv run python -m pytest tests/ -v
~~~

In the pull request, describe the behavior change, the tests you ran, and any live providers you did or did not exercise. For UI changes, include a screenshot with synthetic data. [Open an issue](https://github.com/dustturtle/XClaw/issues).
