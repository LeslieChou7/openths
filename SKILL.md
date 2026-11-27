---
name: openths
license: Apache-2.0
description: 使用 OpenTHS 独立命令查询同花顺只读行情、排行、市场概览、热榜、龙虎榜、板块、异动、涨跌停及集合竞价，保留来源字段、单位和验证范围。
---

# OpenTHS

从本技能目录运行 Python 3.13+ 的 `scripts/query.py`。使用绝对路径可从其他目录执行。先运行 `capabilities` 查看本地目录，按 [命令参考](references/commands.md) 选择参数。运行所需协议代码和资源随技能提供。

| 用户目标 | 命令 | TCP 配置 |
| --- | --- | --- |
| 热榜目录 | `boards`，本地能力目录 | 不需要 |
| 沪深北 A 股单证券九字段显示快照 | `quote --code 六位代码 --market 市场`，沪 `17`、深 `33`、北 `151` | 需要 |
| 涨幅、跌幅、成交额、快速涨幅、大单净量、主力净流入 | `ranking` | 需要 |
| 涨跌家数、涨跌停、成交额比较、资金、大小盘 | `market` | 需要 |
| 大家都在看 1 小时或 24 小时 | `hot` | 不需要 |
| 指定日期龙虎榜列表、候选行事件、单事件详情 | `dragon-list` / `dragon-events` / `dragon-detail` | 不需要 |
| 板块摘要、指定范围板块排行及成分股排行 | `sector-capabilities` / `sector-ranking` / `sector-constituents` | 不需要 |
| 当前/历史异动区间、最新停牌/复牌（M14指定范围） | `stock-anomalies` / `trading-status-events` | 不需要 |
| 指定日期六统计、五池（M15精确检查点） | `limit-capabilities` / `limit-summary` / `limit-pool` | 不需要 |


只有用户要求实际数据时才加 `--live`。默认一页，使用有限请求和用户显式提供的 TCP 配置。统计数字须保留原单位、精度、时区与缺失值。分别说明来源时间和本地接收时间。市场部分结果逐组件检查状态。龙虎榜从候选行枚举事件后再取详情，板块成分从本次排行返回的板块选取。遇鉴权、验证码或限流时停止相关请求，只报告脱敏错误。

各市场的实测证券和字段对照见[来源记录](references/scope.md)。
