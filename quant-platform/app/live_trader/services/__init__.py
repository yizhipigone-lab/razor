"""live_trader 交易核心服务(阶段3拆分, 2026-07-19)。

从 main.py 抽出的下单/信号核心服务:
  order_service  - place_order_service(下单核心, WEB/TDX 共用)
  signal_service - process_buy_signals(并发内核+心跳+去重+幂等) + _process_one_signal(单信号)

依赖约定:
  - 顶部 import: _state(独立模块) + 跨 services(signal_service→order_service 单向)
  - 函数内相对 import 用 **.. 双点**(services/ 比 main.py 深一级,审计 C1/A)
  - 绝对路径 import 不变(from app.utils / from core.settings)
  - logger 沿用 live_trader.main(审计 R6,运维监控按此名检索)
"""
