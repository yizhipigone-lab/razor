"""实盘交易模块运行时状态容器。

阶段 0a（2026-07-19）从 main.py 抽离：单独模块避免后续 routers/ 拆分时
main ↔ routers 循环 import（main 顶部要 include_router，routers 回头
import main 会拿到半初始化模块）。

机制：dict 是引用类型，所有 `from ._state import state` 拿到的都是
**同一个 dict 对象**。lifespan 在 main.py 装配组件后 `_state.update({...})`
写入的就是这个 dict，所有 import 它的模块（main / scheduler / 未来的 routers）
同时看到新值。

历史：原 `app/live_trader/main.py:26` 的 `_state: dict = {}`。

运行时填充的 key（供阅读，不在本文件初始化）：
  config / runtime_state / store / qmt / notifier / notif_store / kill_switch /
  clearance_lock / pnl_engine / audit / executor / scheduler / callback /
  connection / risk_gate / exit_monitor / mode_switching / lock_fd / lock_file
"""

state: dict = {}
