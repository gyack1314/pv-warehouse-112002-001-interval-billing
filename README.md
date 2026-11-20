# 分段电价下的电费出账与差额入仓

这是本地演练用的合成来源资料。CSV 使用 UTF-8、有首行字段名，空字段表示未提供。除非字段语义明确为零，空值没有数值含义。

## 资料契约

结算窗口是交割点月度纳入市场的营业段，pilot 的营业段跨夏令时回拨且只有两个短窗口。时间含偏移，区间左闭右开。表底、分量均为 kWh，价为元/MWh。版本号在同一业务键内递增。投递按 wave 逐次到达，同一 wave 内无先后要求。财务回执引用财务端留存的送单别名，A、B、C 分别表示实际首张、下一张和再下一张送单。账期内没有营业义务的格子不属于缺格。

以下文件位于 `data/`。

- `market_calendar.csv`（2 条）：`point`、`period`、`start`、`end`、`minutes`
- `meter_registers.csv`（11 条）：`wave`、`meter`、`direction`、`at`、`kwh`、`rev`
- `interval_proof.csv`（2 条）：`wave`、`meter`、`start`、`end`、`kwh`、`rev`
- `meter_authority.csv`（3 条）：`wave`、`point`、`direction`、`start`、`end`、`meter`、`kind`、`preceding_meter`
- `prices.csv`（9 条）：`wave`、`point`、`start`、`direction`、`rev`、`status`、`yuan_per_mwh`
- `ledger_receipts.csv`（4 条）：`wave`、`receipt`、`dispatch_alias`、`decision`

`manifest.json` 记录完整资料包的列名、行数、校验值和跨表引用。完整资料包可含业务冲突或同一来源的多次投递，校验器保留这些原始记录。

## 本地查看

适用 Python 3.11，无需安装第三方库或构建。

```sh
python3 inspect_inputs.py
python3 -m unittest -v test_input_pack
```

查看命令打印各原始表的记录数。单元测试校验资料结构、引用与文件完整性，可离线执行。

## 结算明细仓（settlement/）

`settlement/` 把上述来源资料跑成可签收的结算明细仓，纯标准库、Python 3.11：

```sh
python3 run_settlement.py --data data --db settlement.db
python3 -m unittest -v test_settlement
```

处理约定：同一波次内先落全部资料修订，再处理财务回执，最后尝试出账；波次内行序不影响结果。

- `foundation.py`：金额十进制、逐格按分取整且半分远离零；区间身份按瞬时确定，夏令时回拨后相同钟表时分不合并；`RevisionBook` 对表底/分量/价格按业务键多版本留存，同版本同键冲突显式隔离，不按到达顺序选值。
- `quantities.py`：表底是累积值，只有同一只表内相邻表底差才是增量，绝不跨表相减；换表/归零只认交接记录，备用表只在授权期间替代指定方向主表；跨界增量只有与格对齐、铺满且合计相等的可核验分量才能拆，整刻钟等不合口径的量只进对账。
- `engine.py`：分区在两方向量价齐备（正式价）时才发出；临时价只做预估；缺格、冲突、已确认零交易状态可区分。已发待签期间收到修订保留在途单；确认后按同一格子对比已确认基线产生差额批次；未签在途单只有明确拒收才终止并重出完整分区；修订使已签格子失去证据时挂待决，资料补齐前不得冲零。回执按财务别名定位实际送单，确认重送幂等、过期回执不串版，同一个确认只改变一次基线。
- `store.py`：把事件、原始行、隔离记录、格子口径、送单、回执、基线落 SQLite，旧版、差额、当前待开可并列查询。

`Warehouse.partition_view` 并列三种口径，`reconciliation` 给出购电/售电独立的量与应收应付，`trace` 从任一张凭单行回查哪对表底、哪份价格、哪版归属。
