# A 股零注册数据源方案

> 本文档基于当前目录下的两份调研文档和已完成的实际验证结果整理，目标是给一个“零注册、零 token”的 A 股分析 / 研究应用提供可直接落地的数据源选型、分层和 failover 参考方案。

---

## 目标

本方案面向第一版可落地产品，原则如下：

- 不依赖注册、积分、token
- 优先选择当前网络环境下已经实测可用的数据源
- 按数据类型拆分链路，不强行用单一数据源覆盖全部能力
- 主链路优先稳定，其次才考虑字段丰富度
- 允许不同能力使用不同主源

---

## 最终选型

### 历史数据

主链路：

1. `BaoStock`
2. `pytdx`

兜底：

3. `TickFlow` 免费层
4. `yfinance`

### 实时行情

主链路：

1. `腾讯直连 HTTP`
2. `新浪直连 HTTP`

### 标的信息 / 基础信息

主链路：

1. `TickFlow` 免费层

补充：

2. `BaoStock`
3. `pytdx`

### 明确不作为第一版核心依赖

- `AKShare`
- `efinance`
- `Tushare Pro`
- 强依赖 Eastmoney API 的链路

原因：

- 当前网络环境下 Eastmoney API 实测不稳定
- 新浪 HTTPS 证书链在当前机器上存在问题
- Tushare Pro 需要注册和 token，不符合第一版目标

---

## 选型理由

| 能力 | 主推荐 | 原因 |
|---|---|---|
| 历史日线 | `BaoStock` | 免费、稳定、无需 token、适合做历史主链路 |
| 历史补充 / 次主源 | `pytdx` | TCP 直连，不依赖网页接口，适合作为强兜底 |
| 实时行情 | `腾讯直连 HTTP` + `新浪直连 HTTP` | 当前环境下最快、最轻、无需注册 |
| 标的信息 | `TickFlow` 免费层 | 结构较清晰，直接支持标的信息查询 |
| 海外兜底 | `yfinance` | 适合最后一层历史兜底，不建议前置 |

---

## 分层设计

建议不要做一个“大一统数据源类”直接覆盖所有接口，而是按能力拆成 3 层：

### 1. 历史行情层

职责：

- 日线 / 周线 / 月线查询
- 批量历史拉取
- 历史回测 / 技术指标输入

推荐实现：

- `HistoricalDataProvider`
- 每个数据源单独实现一个 adapter

建议适配器：

- `BaoStockHistoricalAdapter`
- `PytdxHistoricalAdapter`
- `TickFlowHistoricalAdapter`
- `YFinanceHistoricalAdapter`

### 2. 实时行情层

职责：

- 单只股票实时行情
- 小批量股票实时行情
- 盘中快照

推荐实现：

- `RealtimeQuoteProvider`

建议适配器：

- `TencentQuoteAdapter`
- `SinaQuoteAdapter`

### 3. 标的信息层

职责：

- 股票代码、名称、交易所
- 上市日期
- 基本静态元数据

推荐实现：

- `InstrumentProvider`

建议适配器：

- `TickFlowInstrumentAdapter`
- `BaoStockInstrumentAdapter`
- `PytdxInstrumentAdapter`

---

## 推荐 failover 方案

### 一、历史行情 failover

推荐顺序：

```text
BaoStock -> pytdx -> TickFlow -> yfinance
```

#### 适用原因

- `BaoStock` 最稳定，适合优先承担历史主链路
- `pytdx` 同样稳定，但更适合做高可用备份
- `TickFlow` 免费层可做补充，但不建议单独承担全部历史能力
- `yfinance` 适合最后兜底，避免国内源全部失败时无数据

#### 切换规则

遇到以下情况切换到下一个源：

1. 请求超时
2. 返回空数据
3. 返回错误码或解析失败
4. 返回数据条数明显不够
5. 返回最新交易日落后过多

#### 一致性建议

对历史数据建议做最小一致性校验：

1. 检查是否包含 `date/open/high/low/close/volume`
2. 检查价格字段是否可转数值
3. 检查时间序列是否升序或可排序
4. 检查最近一条记录日期是否合理

---

### 二、实时行情 failover

推荐顺序：

```text
腾讯直连 HTTP -> 新浪直连 HTTP
```

#### 适用原因

- 两者都无需注册
- 接口简单，响应快
- 当前环境下已实测可拿到 A 股实时数据

#### 切换规则

遇到以下情况切换：

1. HTTP 非 200
2. 返回内容为空
3. 返回格式不符合预期
4. 指定股票未出现在返回结果中
5. 响应时间超过阈值

#### 实现建议

- 单只查询优先用主源
- 小批量查询支持拆分请求
- 给主源设置较短超时，例如 `2-3 秒`
- 主源失败立刻切到次源，不重试过多次

---

### 三、标的信息 failover

推荐顺序：

```text
TickFlow -> BaoStock -> pytdx
```

#### 适用原因

- `TickFlow` 免费层的标的信息结构更适合直接消费
- `BaoStock` 可补基础静态信息
- `pytdx` 可在最后兜底股票列表或证券基础信息

#### 切换规则

遇到以下情况切换：

1. 查询结果为空
2. 代码格式不匹配
3. 缺失关键字段，如代码、名称、交易所

---

## 推荐的职责边界

为了减少后续维护成本，建议每类能力只暴露“统一标准模型”，不要把底层返回结构直接暴露给业务层。

### 历史行情标准字段

```python
[
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "source",
]
```

### 实时行情标准字段

```python
[
    "symbol",
    "name",
    "price",
    "open",
    "high",
    "low",
    "pre_close",
    "volume",
    "amount",
    "quote_time",
    "source",
]
```

### 标的信息标准字段

```python
[
    "symbol",
    "code",
    "exchange",
    "name",
    "market",
    "list_date",
    "instrument_type",
    "source",
]
```

---

## 股票代码标准化建议

内部统一建议使用：

```text
600519.SH
000001.SZ
430001.BJ
```

原因：

- 比纯六位代码更清晰
- 比 `sh.600519` 更通用
- `TickFlow`、`yfinance` 映射也更容易做

### 各源格式映射

| 内部格式 | 数据源格式 |
|---|---|
| `600519.SH` | BaoStock: `sh.600519` |
| `600519.SH` | pytdx: `market=1, code=600519` |
| `600519.SH` | 腾讯/新浪: `sh600519` |
| `600519.SH` | TickFlow: `600519.SH` |
| `600519.SH` | yfinance: `600519.SS` |

---

## 建议的调用策略

### 历史数据

- 先查本地缓存
- 未命中时优先请求 `BaoStock`
- 若失败则切到 `pytdx`
- 若 `pytdx` 也失败，则尝试 `TickFlow`
- 最后才用 `yfinance`
- 成功后统一标准化并写入缓存

### 实时行情

- 不建议强缓存过久
- 可做短缓存，例如 `3-10 秒`
- 主源腾讯失败时立即切新浪
- 对同一股票短时间内避免重复请求

### 标的信息

- 适合长缓存
- 每日或每周做一次刷新即可
- 首次查询未命中时走 `TickFlow`
- 若 `TickFlow` 失败，再查 `BaoStock` / `pytdx`

---

## 建议的缓存策略

| 数据类型 | 建议 TTL |
|---|---|
| 标的信息 | 1 天 - 7 天 |
| 历史日线 | 交易日内可视为稳定，收盘后刷新 |
| 实时行情 | 3 秒 - 10 秒 |
| 股票列表 | 1 天 |

---

## 建议的容错策略

### 超时

- 实时接口：短超时
- 历史接口：中等超时
- TCP 数据源：允许比 HTTP 更长的超时

参考值：

```text
实时 HTTP: 2-3 秒
历史 HTTP: 5-8 秒
pytdx TCP: 5-10 秒
```

### 重试

- 实时行情：最多重试 1 次
- 历史数据：最多重试 2 次
- 切源优先于无意义重试

### 降级

建议按“能力降级”而不是“服务整体失败”处理：

1. 实时失败时，页面可降级显示上一笔缓存
2. 历史失败时，提示当前数据源不可用并尝试次源
3. 标的信息失败时，允许仅展示代码、不阻塞主流程

---

## 推荐的模块结构

可参考下面的目录设计：

```text
datasources/
  base/
    models.py
    exceptions.py
    normalizers.py
  historical/
    baostock_adapter.py
    pytdx_adapter.py
    tickflow_adapter.py
    yfinance_adapter.py
    manager.py
  realtime/
    tencent_adapter.py
    sina_adapter.py
    manager.py
  instruments/
    tickflow_adapter.py
    baostock_adapter.py
    pytdx_adapter.py
    manager.py
```

---

## 推荐的 manager 思路

### HistoricalDataManager

职责：

- 统一入口
- 顺序尝试数据源
- 标准化输出
- 记录命中源
- 写缓存

伪代码：

```python
for adapter in [baostock, pytdx, tickflow, yfinance]:
    try:
        data = adapter.get_history(symbol, start, end, interval="1d")
        if validate_history(data):
            return normalize_history(data, source=adapter.name)
    except Exception as e:
        log_warning(adapter.name, e)
```

### RealtimeQuoteManager

伪代码：

```python
for adapter in [tencent, sina]:
    try:
        data = adapter.get_quote(symbol)
        if validate_quote(data):
            return normalize_quote(data, source=adapter.name)
    except Exception as e:
        log_warning(adapter.name, e)
```

### InstrumentManager

伪代码：

```python
for adapter in [tickflow, baostock, pytdx]:
    try:
        data = adapter.get_instrument(symbol)
        if validate_instrument(data):
            return normalize_instrument(data, source=adapter.name)
    except Exception as e:
        log_warning(adapter.name, e)
```

---

## 不建议第一版做的事情

1. 不要把实时、历史、标的信息混在一个 failover 链里
2. 不要依赖单一的“大而全”免费数据源
3. 不要在第一版就把 Eastmoney 体系当核心
4. 不要把 legacy 接口能力误当成长期稳定承诺
5. 不要把底层源的原始字段直接返回给业务层

---

## 第一版推荐落地方案

如果目标是“先稳定跑起来”，建议按下面方案实现：

### 历史

```text
主源: BaoStock
备源: pytdx
补充: TickFlow
最终兜底: yfinance
```

### 实时

```text
主源: 腾讯直连 HTTP
备源: 新浪直连 HTTP
```

### 标的信息

```text
主源: TickFlow
备源: BaoStock
兜底: pytdx
```

---

## 总结

对于一个零注册的 A 股研究应用，当前最现实、最稳的方案不是选择单一“万能数据源”，而是：

- 用 `BaoStock + pytdx` 解决历史数据稳定性
- 用 `腾讯/新浪直连 HTTP` 解决实时行情
- 用 `TickFlow` 免费层解决标的信息和补充日线
- 用 `yfinance` 做最后一层海外兜底

这个组合的核心优势是：

- 不需要注册
- 不需要 token
- 不强依赖当前环境下不稳定的 Eastmoney 链路
- 可以按能力拆分，后续逐步替换和增强

如果后续要做第二版，再考虑把 `Tushare Pro` 或更专业的数据服务接入进来会更合理。

-----------------------------------------------------------------------

附录（验证报告）：
# A 股数据源验证报告

验证时间：2026-03-26（Asia/Shanghai）

## 验证范围

- 仅做“当前网络环境下”的基础验证
- 明确跳过“必须注册 / 必须 token”前提的主流程验证
- 重点看：是否能直接拉到 A 股实际数据，而不是只看官网能否打开

## 验证方法

- 新建临时 Python 环境并安装相关库
- 对每个候选源执行 1-2 个最小可用调用
- 优先验证：
  - 实时行情
  - 历史日线
  - 标的信息 / 股票列表
- 补充验证了底层网络特征：
  - Eastmoney API
  - 新浪 / 腾讯直连 HTTP
  - TickFlow 免费层限制

## 结论总览

### 直接可用

| 数据源 | 实测可用能力 | 结论 |
|---|---|---|
| 新浪直连 HTTP | 实时行情 | 可直接用 |
| 腾讯直连 HTTP | 实时行情 | 可直接用 |
| BaoStock | 历史日线 | 可直接用 |
| pytdx | 历史日线 / TCP 行情直连 | 可直接用 |
| yfinance | A 股历史日线 | 可直接用 |
| TickFlow（免费层） | 标的信息、历史日线 | 可直接用 |

### 部分可用

| 数据源 | 实测情况 | 结论 |
|---|---|---|
| efinance | 单票实时行情可用；历史日线失败；全市场实时批量失败 | 部分可用 |
| Tushare（旧接口） | `get_realtime_quotes` 可用 | 部分可用，且应视为 legacy 路线 |
| AKShare | 股票列表可用；东财历史 / 实时失败；新浪历史 SSL 失败；新浪实时超时 | 部分可用，但当前环境下不适合作主数据源 |

### 本次未纳入匿名可用性结论

| 数据源 | 原因 |
|---|---|
| Tushare Pro | 主流程依赖注册和 token，本次按要求跳过 |

## 详细结果

### 1. 新浪直连 HTTP

- 测试接口：`http://hq.sinajs.cn/list=sh600519`
- 结果：成功返回 `var hq_str_sh600519=...`
- 判断：当前环境下可直接作为轻量实时行情源

### 2. 腾讯直连 HTTP

- 测试接口：`http://qt.gtimg.cn/q=sh600519`
- 结果：成功返回 `v_sh600519=...`
- 判断：当前环境下可直接作为轻量实时行情源

### 3. AKShare

- 成功：
  - `stock_info_a_code_name()` 可返回约 5493 条 A 股代码
- 失败：
  - `stock_zh_a_hist(...)` 走 Eastmoney，失败
  - `stock_zh_a_spot_em()` 走 Eastmoney，失败
  - `stock_zh_a_daily(...)` 走新浪 HTTPS，证书校验失败
  - `stock_zh_a_spot()` 在本环境中 25 秒超时
- 判断：
  - 不是“完全不可用”
  - 但当前环境下历史 / 实时核心链路都不稳定，不建议作为主源

### 4. efinance

- 成功：
  - `get_latest_quote('600519')` 可用
- 失败：
  - `get_quote_history('600519', ...)` 走 Eastmoney 历史接口，失败
  - `get_realtime_quotes()` 全市场批量接口返回 JSON 解析失败
- 判断：
  - 可做单票实时补充
  - 不适合当前环境下承担历史数据或全市场批量主链路

### 5. BaoStock

- 成功：
  - `login()`
  - `query_history_k_data_plus(...)`
- 返回：成功取到 2024-01-02 至 2024-01-10 的 7 条日线
- 判断：当前环境下是可靠的免费历史数据源

### 6. pytdx

- 成功：
  - TCP 直连通达信服务器
  - 从 `218.75.126.9:7709` 成功取到 `600519` 最近 10 条日线
- 特征：
  - 前几个预置服务器不一定成功，实际落到 failover 后的服务器
  - 单次验证耗时约 20.63 秒
- 判断：可用，适合历史行情 / K 线兜底

### 7. yfinance

- 成功：
  - `yf.download('600519.SS', start='2024-01-02', end='2024-01-10')`
- 返回：6 条历史记录
- 判断：可作为 A 股历史数据的海外兜底源，但不建议做主源

### 8. Tushare（旧接口）

- 成功：
  - `tushare.get_realtime_quotes('600519')`
- 说明：
  - 这是无需 token 的旧接口，不等同于 Tushare Pro
- 判断：
  - 现在能用，但更适合当兼容补充，不建议把产品主架构压在 legacy 接口上

### 9. TickFlow

- 成功：
  - 免费层可查询标的信息：`600519.SH`
  - 免费层可查询历史日线：最近 5 条
- 失败：
  - 免费层实时行情会明确报 `PermissionError`
- 判断：
  - 免费层适合“标的信息 + 日线”
  - 不适合实时行情

## 网络环境观察

### 1. Eastmoney 域名不是完全不通，但 API 实际不可稳定使用

- `curl -I https://push2his.eastmoney.com` 可返回 `404`
- 但请求实际 JSON API 时：
  - `curl` 返回 `Empty reply from server`
  - Python 库侧表现为 `ProxyError` / 连接被远端关闭

这说明当前环境下，Eastmoney 的 API 根链路存在实际可用性问题。

### 2. 新浪 HTTPS 证书链存在问题

- `curl -I https://stock.finance.sina.com.cn` 返回：
  - `SSL certificate problem: unable to get local issuer certificate`
- 这与 `AKShare stock_zh_a_daily(...)` 的失败现象一致

### 3. PyPI 默认证书校验也有问题

- 直接 `pip install` 首次失败
- 追加 `--trusted-host` 后安装成功

这说明当前机器存在一定的证书信任链 / 网络代理环境问题，后续凡是依赖 HTTPS 校验严格的网站，都可能再踩一次。

## 对产品架构的建议

如果你现在就开始做一个“零注册 / 零 token 可落地”的 A 股研究应用，建议当前环境下先按下面的组合来：

1. 历史日线主链路：`BaoStock + pytdx`
2. 实时行情主链路：`腾讯直连 HTTP + 新浪直连 HTTP`
3. 标的信息 / 免费补充：`TickFlow 免费层`
4. 海外兜底：`yfinance`
5. 谨慎使用：`AKShare`、`efinance`

不建议当前环境直接把下面两类放到主链路：

1. 强依赖 Eastmoney API 的链路
2. 强依赖新浪 HTTPS 证书链正常的 AKShare 子接口

## 推荐结论

如果目标是“先快速做出能跑的 A 股研究应用”，当前环境下最现实的可用数据组合是：

- 历史：BaoStock、pytdx、yfinance
- 实时：腾讯直连 HTTP、新浪直连 HTTP
- 标的信息 / 免费日线补充：TickFlow 免费层

AKShare 和 efinance 在这台机器当前网络环境里都不够稳，不建议作为第一版核心依赖。

