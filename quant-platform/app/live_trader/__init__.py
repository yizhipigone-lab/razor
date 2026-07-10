"""实盘交易模块 live_trader(v5.4)

在 p9 既有 sim_trader + backtest + screener 体系上新增真实下单能力。
与 sim_trader 并行,独立 DB,复用 exit_rules/execution/intraday_monitor。

详见 docs/实盘交易模块实施开发书_v5.0.md
"""
