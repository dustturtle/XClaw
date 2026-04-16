# XClaw 金融能力改进清单

基于对 `finance-skills` 文章与开源仓库的调研，结合 XClaw 当前的金融能力、渠道形态和已上线工作流，整理如下改进优先级。

## 立即做（第一批）

- [x] `stock_correlation`
  - 目标：补齐“多标的联动性/相关矩阵”分析能力。
  - 当前状态：已完成。
  - 说明：支持 2-8 个股票/指数的收益率相关性分析，覆盖 A 股 / 美股 / 港股。

- [x] `earnings_analysis`
  - 目标：补齐“财报前瞻 / 财报回顾 / 盈利预期”分析能力。
  - 当前状态：已完成第一版。
  - 说明：当前主要支持美股 / 港股，统一收敛在一个工具里，`mode` 支持 `preview` / `recap` / `estimate`。

- [x] Agent 路由与注册补齐
  - 目标：让模型能稳定命中新工具，而不是继续兜圈子。
  - 当前状态：已完成。
  - 说明：已补充 system prompt 路由规则、investment skill 注册，以及 sub_agent 白名单。

## 中期规划

- [x] 财报事件分析拆细
  - 当前状态：已完成第一轮增强。
  - 已补充：财报回顾中的历史 Beat 率、价格反应等关键信息。
  - 后续可继续追加：管理层指引摘要、目标价变化轨迹。

- [x] 相关性可视化
  - 当前状态：已完成第一轮增强。
  - 已补充：`stock_correlation` 支持 `visualize=true` 时导出热力图 PNG。

- [x] ETF 溢价 / 折价分析
  - 当前状态：已完成。
  - 已补充：`etf_premium_analysis`，支持基于现价与 NAV 做溢价/折价判断。

- [x] 流动性分析
  - 当前状态：已完成第一版。
  - 已补充：`stock_liquidity`，输出日均成交额、平均振幅和 Amihud 冲击成本代理指标。

- [x] 策略扫描能力增强
  - 当前状态：已完成第一轮增强。
  - 已补充：`strategy_scan` 新增 `decision_card` 输出模式，适合直接回答“决策卡/买卖点总结”类问题。

## 暂不做

- [ ] 国外社交抓取
  - 本轮明确不做，包括 Twitter / Discord / Telegram / LinkedIn 等。

- [ ] 外部付费数据平台深度接入
  - 如 Funda AI、Adanos 这类重 API 依赖能力，先不作为近期重点。

- [ ] 期权收益曲线交互式可视化
  - 有价值，但当前 XClaw 的主战场仍是股票 + 报告 + 多渠道投递，暂不抢优先级。

- [ ] 创业公司分析 / 地缘专题监控
  - 与当前“投资助手 + 任务助理”的主线不够贴合，先不纳入近期路线。

## 本轮已完成的验证

- [x] 新增测试：`stock_correlation`（双标的 + 多标的矩阵）
- [x] 新增测试：`earnings_analysis`（preview + estimate）
- [x] 新增测试：system prompt 路由规则
- [x] 已运行：
  - `python3 -m pytest -q tests/test_stock_tools.py::test_stock_correlation_cn_pair_analysis tests/test_stock_tools.py::test_stock_correlation_matrix_analysis tests/test_stock_tools.py::test_earnings_analysis_preview tests/test_stock_tools.py::test_earnings_analysis_estimate tests/test_agent_engine.py::test_build_system_prompt_mentions_correlation_and_earnings_tools`
- [x] 已运行：
  - `python3 -m pytest -q tests/test_stock_tools.py::test_stock_correlation_visualization_exports_heatmap tests/test_stock_tools.py::test_earnings_analysis_recap_includes_beat_rate_and_price_reaction tests/test_stock_tools.py::test_etf_premium_analysis_uses_nav_gap tests/test_stock_tools.py::test_stock_liquidity_analyzes_average_value_and_amihud tests/test_strategy_scan.py::test_strategy_scan_supports_decision_card_output`
- [x] 已运行：
  - `python3 -m pytest -q`
  - 结果：`411 passed, 1 warning`
- [ ] 端到端真实行情验证（建议下一轮手工补）
