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
