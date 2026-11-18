// Render fixed evidence through the production host and card renderer.
appendMessage("user", "这组示例指数最近五天怎样变化？");
appendMessage("assistant", "", [], false, false);
const item = document.querySelector("#messages .assistant:last-child");
const query = {symbol: "DEMO"};
const evidence = [{
  name: "stock_history", arguments: query, is_error: false,
  content: [
    "标的: DEMO | 资产类型: index | 周期: daily",
    "数据源: 合成示例",
    "日期 开盘 收盘 最高 最低",
    "2025-01-06 100 100 101 99",
    "2025-01-07 100 103 104 100",
    "2025-01-08 103 102 103 101",
    "2025-01-09 102 105 106 102",
    "2025-01-10 105 104 105 103",
  ].join("\n"),
}];
const blocks = [
  {id: "p0", type: "text", source: "示例指数从 100 点升至 104 点，五个交易日累计上涨 4%。"},
  {id: "chart", type: "openui", source: 'q=Query("stock_history",{symbol:"DEMO"})\nroot=HistoryChart("五日收盘",q)'},
  {id: "p1", type: "text", source: "第四天收盘最高，为 105 点。价格均为合成数据。"},
];
const options = {chatId: "demo", runId: "fixture", getToken: () => "", isCurrent: () => true};
for (const length of [1, 2, 3]) {
  window.XClawOpenUIHost.updateLive(item, {
    version: "xclaw-live-1", round: 0, provisional: true, blocks: blocks.slice(0, length), evidence,
  }, options);
}
window.XClawOpenUIHost.updateLive(item, {
  version: "xclaw-live-1", round: 0, provisional: false, validated: true, blocks, evidence,
}, options);
