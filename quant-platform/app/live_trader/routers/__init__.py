"""live_trader 路由子模块(阶段1拆分, 2026-07-19)。

从 main.py 抽出的 40 个路由按业务域分组:
  system     - 通知/同步/health/shutdown(阶段1第1步, 已完成)
  market     - (阶段1后续) 行情/持仓/订单/成交/净值查询
  trade      - (阶段1后续) 下单/撤单/信号/kill-switch/对账
  config_api - (阶段1后续) 配置热加载一组

依赖约定(避免 main↔routers 循环 import):
  - 顶部 import: _state(独立模块)、auth(独立模块)、stdlib、fastapi、logger
  - 函数内 import: main 内的工具函数(_takeover_positions / _cleanup_zombies /
    _spawned_processes 等), 阶段2搬 lifecycle.py 后改 import 源。
"""
