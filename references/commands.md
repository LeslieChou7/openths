# 命令参考

以下相对路径基于 skill 根目录；在其他工作目录运行时使用 skill 的绝对路径和Python 3.13+。所有输出为一个 UTF-8 JSON 对象：`command` 与 `data`；失败向标准错误输出安全的 `error.category`，不会附原始异常或配置。退出码 0 成功、1 OpenTHS/本地运行失败、2 参数或缺少 `--live`。

本命令行可由终端、脚本或具有本地命令工具的 AI 助手调用，不依赖特定宿主。以下示例的 `python` 指Python 3.13+ 解释器；接入、路径和进程返回值约定见 [宿主接入说明](integration.md)。`--help` 输出帮助文本，不适用查询结果的 JSON 约定。

## 本地命令

```powershell
python scripts/query.py capabilities
python scripts/query.py boards
python scripts/query.py --help
python scripts/query.py ranking --help
```

`capabilities` 为本封装静态目录，`boards` 为 OpenTHS 的本地热榜目录，均非服务端实时发现。

## TCP 查询

`quote`、`ranking`、`market` 必须提供 `--host`、`--port`、`--app-config`、`--app-info`。以下命令使用调用者本地配置，执行会访问真实服务。

```powershell
python scripts/query.py quote --live --code 000001 --market 33 --host mobi2.123ths.com --port 9528 --app-config local/app-config.txt --app-info G037.09.127.1.32
python scripts/query.py ranking --live --sort change_rate --direction asc --pages 2 --host mobi2.123ths.com --port 9528 --app-config local/app-config.txt --app-info G037.09.127.1.32
python scripts/query.py market --live --components breadth limit_counts --host mobi2.123ths.com --port 9528 --app-config local/app-config.txt --app-info G037.09.127.1.32
```

- `quote` 每次查询一只 A 股。`--code` 传六位 ASCII 数字，保留前导零。`--market` 传沪市 `17`、深市 `33` 或北交所 `151`。例如沪市用 `--code 601326 --market 17`，北交所用 `--code 920679 --market 151`。九项字段保留页面显示精度。[实测样本](scope.md#沪深北-a-股快照)
- `ranking`：`--sort change_rate` 支持 `--direction desc/asc`，分别涨幅/跌幅；`turnover`、`rapid_change_rate`、`large_order_net_volume`、`main_net_inflow` 仅 `desc`。默认涨幅降序、一页，每页 OpenTHS 固定 24 条。`--pages 1..10` 是本封装的有限请求上限，实际遇无游标提前结束，非服务端总页数限制。输出 `pages` 保留每页元数据，不重排或去重。
- `market`：默认五组件，可用 `--components breadth limit_counts turnover fund_flow size_comparison` 选非空、不重复子集。`--allow-partial` 才返回部分错误组件，否则失败退出。范围固定 `m8_baseline`；完整五组件通常含六个业务子请求，组件不是独立股票列表。

配置文件按字节解码 UTF-8，保留协议要求的 CRLF。不要通过编辑器自动改写本地配置文件的行结束符。将配置放在本地忽略目录。

## HTTP 热榜

```powershell
python scripts/query.py hot --live --board stock_hour
python scripts/query.py hot --live --board stock_24h
```

不需要 TCP 配置。每次单响应，无热榜续页。保留源顺序、重复项、排名和缺失行情；`returned_count` 是本响应条数，`total` 未知时仍为 `null`。

## 龙虎榜

```powershell
python scripts/query.py dragon-list --live --date 2026-11-05 --pages 2
python scripts/query.py dragon-events --live --date 2026-11-05 --list-page 1 --entry-index 0
python scripts/query.py dragon-detail --live --date 2026-11-05 --list-page 1 --entry-index 0 --event-index 0
```

不需要 TCP 配置。日期必须显式指定；不能自动将当天视作最新披露日。`dragon-list` 默认一页，最多显式 10 页，每页 OpenTHS 固定 50 条。每页公开调用包含日期状态和列表两个来源请求。

`--list-page` 从 1 开始，范围 1..10；工具从第一页逐页检查来源日历和分页元数据，直到指定页。`--entry-index`、`--event-index` 从 **0** 开始，必须显式给出；越界报 `selection`。先查看列表后选定行，再查看事件后选定事件；重新请求可能改变页面顺序，应核对输出中的证券、日期、区间和原因。事件/详情输出包含本次实际选中的 `entry`，详情另含 `event`，不能只凭之前的索引认定身份。

事件枚举和详情分别增加一个来源请求；不自动批量展开。引用字段仅用于解释实际结果，JSON 不导出客户端状态，也不接受用户手工构造引用或游标。

## 共用参数与数据语义

实时查询命令支持 `--connect-timeout`（默认 5 秒）、`--request-timeout`（默认 15 秒）和 `--retries 0/1`（默认 0）。超时必须有限且大于零。每个 OpenTHS 公开调用共用其内部总截止时间；分页及事件流程包含多个调用，因此不是整条命令共享一个 15 秒预算。OpenTHS 仅重试允许安全重试的只读网络失败。

输出保留来源文本、单位、精度和派生标记；Decimal 转成十进制字符串，日期及时间转 ISO 格式，未知保留 `null`。列表顺序和重复行保持原样。`next_page_available` 只说明本次 OpenTHS 返回的游标非空，即使热榜为 false，也不能推断服务端全集已取完。私有字段不导出；不存在跨进程游标恢复机制。

安全错误类别：`configuration`、`selection`、`network`、`protocol`、`authentication`、`rate_limit`、`business`、`internal`。参数错误由参数解析器报告。`--allow-partial` 下组件错误留在结果中，进程可以成功退出，使用者仍须检查每个组件的 `status`。
## 板块统计命令

使用本地板块能力目录校验参数。以下示例不由测试自动执行。

```text
python scripts/query.py sector-capabilities
python scripts/query.py sector-ranking --date 2026-11-12 --live
python scripts/query.py sector-ranking --mode multi_day --date 2026-11-02 --end-date 2026-11-13 --category region --sort turnover --direction asc --live
python scripts/query.py sector-constituents --date 2026-11-12 --pages 2 --live
python scripts/query.py sector-ranking --category region --mode intraday --date 2026-11-16 --start-time 11:04 --end-time 15:00 --direction asc --live
python scripts/query.py sector-constituents --category region --mode intraday --date 2026-11-16 --start-time 11:04 --end-time 15:00 --sector-code 882021 --sector-market 48 --sort main_net_inflow --direction asc --live
```

sector-capabilities只读OpenTHS本地摘要。排行/成分命令支持category=all/industry/concept/style/region、sort=change_rate/main_net_inflow/turnover、direction=asc/desc，但这些维度不能任意组合：OpenTHS30项精确白名单决定能否发送，异常历史部分盘内继续拒绝。

区间默认historical_day，必须显式--date；intraday须同时给--start-time/--end-time，不可给--end-date；multi_day须给--end-date，不可给盘内时间。不会猜最新来源日或替用户修改日期。无关或缺少的区间选项在创建客户端前拒绝。

--pages限制1..2，默认1。成分命令额外先用--ranking-pages（1..2，默认1）、--ranking-sort（默认change_rate）、--ranking-direction（默认desc）取相同区间的排行，只从实际返回页选择唯一匹配--sector-code/--sector-market的引用（默认881146/48燃气），然后查询成员。找不到或重复时停止，不造引用、不扫更多页。最多4次OpenTHS调用，默认重试0；API未验收页仍拒绝，不能把出现next_page_available说成任意页可用。

排行结果为pages及请求/返回页数；成分结果为ranking_pages、constituent_pages及请求/返回成分页数。每页保留总数、返回条数、原序、Decimal字符串、空值和时间，私有客户端及游标不序列化。盘内time转ISO文本但不补时区。燃气首页total27/返回20只有显式请求并取得第二页7条后，才可说明本次已覆盖27条。
## M15公共入口检查点命令

运行 limit-capabilities 查看可用范围，limit-summary 和 limit-pool 仅接受已记录的参数组合。历史取证见 [来源记录](scope.md)。

查询必须--live和显式--date，不能猜最新日。limit-pool另须--pool（limit_up/approaching_limit_up/consecutive_limit_up/broken_limit_up/limit_down），可选--sort change_rate/latest、--direction asc/desc、--pages 1..2（默认1）；省略排序采用来源默认。两种查询可选--markets HS GEM2STAR、--include-st、--include-unopened，组合由OpenTHS精确目录限制，不能任意交叉。limit-summary显式--allow-partial才接受不完整六统计。新命令仅--retries 0。

输出保留来源字段、Decimal十进制字符串、null、原顺序及重复项；源总数、返回条数、页号与页数分开。next_page_available仅表示OpenTHS有可用引用，不代表没有引用就是服务端全集结束。游标和客户端私有状态不序列化。未知时间单位、币种和统计口径不由封装补造。

## M14指定范围命令

`stock-anomalies --kind current|history --live`；`trading-status-events --kind suspension|resumption --live`。当前异动仅返回首页。对历史异动和停复牌，显式 `--pages 2` 最多取两页。默认导入、帮助及测试不联网；不提供日期、排序、任意URL或监控参数。

日期和类型raw值、未知状态、None及Decimal字符串保持OpenTHS原义。历史实网覆盖见 [来源记录](scope.md)。

### 集合竞价

- auction-ranking --live --date YYYY-MM-DD --kind one_price|yesterday_limit_up|weak_to_strong 查询上海当日默认首屏。
- auction-sector-changes --live --date YYYY-MM-DD --side bullish|bearish 查询指定侧的可见板块快照。
- watch-auction-sector-changes 使用相同日期与侧别，另须 --interval、--max-duration、--max-requests。秒数与发送上限由 OpenTHS 校验。完成后输出一个 JSON 对象，data 按轮次包含基线与本地观察变化。

三项命令使用 --retries 0（默认）。保留原值和缺失值，两轮之间可能漏掉变化。
