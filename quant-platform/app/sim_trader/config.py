"""
模拟盘交易 — 配置中心
"""
from datetime import date
from core.settings import settings

# ═══════════ 资金 ═══════════
INITIAL_CAPITAL = 1_000_000
POSITION_SIZE   = 50_000     # 单票买入上限
MIN_BUY_AMT     = 5_000      # 最小买入金额

# ═══════════ 风控 ═══════════
LOSS_STREAK_HALVE = 3        # 连亏N笔 → 仓位减半
LOSS_STREAK_PAUSE = 5        # 连亏N笔 → 暂停买入
PAUSE_DAYS        = 3        # 暂停天数（自然日）

# ═══════════ 退出 ═══════════
# 从配置文件加载，消除硬编码（解决风控参数漂移问题）
HARD_STOP      = settings.get("backtest", "hard_stop", default=-0.06)        # 硬止损
TRAIL_ACTIVATE = settings.get("backtest", "trail_activate", default=0.05)    # 移动止盈激活阈值（5轮回测最优）
TRAIL_DD       = settings.get("backtest", "trail_dd", default=0.02)          # 移动止盈回撤距离（5轮回测最优）
TIME_EXIT_DAYS = 7            # 时间条件退出天数（盘整突破优化: 3→5→7）

# ATR 动态移动止盈: 启用后 TRAIL_DD = max(TRAIL_DD, ATR_TRAIL_MUL * ATR(14) / entry_price)
USE_ATR_TRAIL = True          # 是否用 ATR 动态调整移动止盈回撤
ATR_TRAIL_MULTIPLIER = 1.0    # ATR 倍数（1.0 = ATR本身作为回撤距离）
TIME_EXIT_PROFIT = 0.03       # 时间条件退出盈利阈值
TIME_FORCE_DAYS = 12          # 时间强制退出天数（优化：9→12，赢率+2%）
SAME_STOCK_COOLDOWN = 20      # 同股票冷却天数
FIRST_DAY_EXIT_MIN_PROFIT = 0.03  # 首日弱势离场：目标涨幅阈值（0=禁用）
FIRST_DAY_EXIT_DAYS = 1          # 首日弱势离场：买入后前N个交易日检查（默认仅第1天）

# 多档阶梯止盈: 按顺序触发，每档卖出剩余仓位的 sell_ratio%
# 触发过的不再重复，剩余仓位最终由 TR 移动止盈保护
TAKE_PROFIT_TIERS = [
    {"profit_pct": 0.03, "sell_ratio": 0.30},  # TP1: +3% 卖30%，剩余走移动止盈（部分止盈+Trail最优）
]

# ═══════════ 策略选择 ═══════════
STRATEGY_NAME = "盘整突破"

# ═══════════ 买入信号 ═══════════
SIGNAL_PARAMS = {
    "N": 5,
    "ZF": 8.0,
    "filter_st": True,
    "filter_bj": True,
    "skip_limit_up": True,
}

# ═══════════ 模拟盘时间 ═══════════
SELL_TIME = "14:53"   # 止盈止损卖出时间（先卖，回收现金）
BUY_TIME  = "14:54"   # 选股买入时间（后买，用回收的现金）

# ═══════════ 回测/模拟区间 ═══════════
SIM_START = date(2015, 1, 5)
SIM_END   = date.today() + date.resolution  # 动态：始终到最新一天
LOAD_START = date(2015, 1, 1)

# ═══════════ 自动执行开关 ═══════════
AUTO_SELL = True    # 是否执行卖出（止盈止损）
AUTO_SCAN = True    # 是否执行选股（生成买入信号）
AUTO_BUY  = True    # 是否执行买入

# ═══════════ 实盘信号转发(v1.2.2 §3) ═══════════
# off=不转发 / sim_only=仅模拟盘(等同off) / sim_and_live=模拟盘+实盘真买
LIVE_SIGNAL_MODE = settings.get("sim_trader", "live_signal_mode", default="off")

# ═══════════ 盘中监控 ═══════════
MONITOR_ENABLED = True      # 启动时是否自动开启盘中监控
MONITOR_MODE = "close"      # "intraday"(触发即卖) | "close"(仅告警)
BROKER_ENABLED  = False     # 是否通过gateway执行真实券商委托（需 QMT/同花顺 已连接）
