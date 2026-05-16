"""
模拟盘交易 — 配置中心
"""
from datetime import date

# ═══════════ 资金 ═══════════
INITIAL_CAPITAL = 1_000_000
POSITION_SIZE   = 50_000     # 单票买入上限
MIN_BUY_AMT     = 5_000      # 最小买入金额

# ═══════════ 风控 ═══════════
LOSS_STREAK_HALVE = 3        # 连亏N笔 → 仓位减半
LOSS_STREAK_PAUSE = 5        # 连亏N笔 → 暂停买入
PAUSE_DAYS        = 3        # 暂停天数（自然日）

# ═══════════ 退出 ═══════════
HARD_STOP      = -0.06        # 硬止损 -6.0%
TRAIL_ACTIVATE = 0.03         # 移动止盈激活阈值
TRAIL_DD       = 0.01         # 移动止盈回撤距离（固定%）
TIME_EXIT_DAYS = 3            # 时间条件退出天数

# ATR 动态移动止盈: 启用后 TRAIL_DD = max(TRAIL_DD, ATR_TRAIL_MUL * ATR(14) / entry_price)
USE_ATR_TRAIL = True          # 是否用 ATR 动态调整移动止盈回撤
ATR_TRAIL_MULTIPLIER = 1.0    # ATR 倍数（1.0 = ATR本身作为回撤距离）
TIME_EXIT_PROFIT = 0.03       # 时间条件退出盈利阈值
TIME_FORCE_DAYS = 12          # 时间强制退出天数（优化：9→12，赢率+2%）
SAME_STOCK_COOLDOWN = 20      # 同股票冷却天数

# 多档阶梯止盈: 按顺序触发，每档卖出剩余仓位的 sell_ratio%
# 触发过的不再重复，剩余仓位最终由 TR 移动止盈保护
TAKE_PROFIT_TIERS = [
    {"profit_pct": 0.03, "sell_ratio": 0.10},  # TP1: +3% 卖 10%（优化：4→3%提前止盈提赢率）
    {"profit_pct": 0.06, "sell_ratio": 0.20},  # TP2: +6% 卖 20%
]

# ═══════════ 策略选择 ═══════════
STRATEGY_NAME = "ma5_angle"

# ═══════════ 买入信号 ═══════════
SIGNAL_PARAMS = {
    "version": "improved",
    "filter_st": True,
    "filter_bj": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
    "disable_quality_sort": False,
    "filter_consecutive_up": False,
    "filter_gap_quality": False,
}

# ═══════════ 模拟盘时间 ═══════════
SELL_TIME = "14:53"   # 止盈止损卖出时间（先卖，回收现金）
BUY_TIME  = "14:54"   # 选股买入时间（后买，用回收的现金）

# ═══════════ 回测/模拟区间 ═══════════
SIM_START = date(2022, 1, 4)
SIM_END   = date.today() + date.resolution  # 动态：始终到最新一天
LOAD_START = date(2022, 1, 1)

# ═══════════ 自动执行开关 ═══════════
AUTO_SELL = False   # 是否执行卖出（止盈止损）
AUTO_SCAN = True    # 是否执行选股（生成买入信号）
AUTO_BUY  = False   # 是否执行买入

# ═══════════ 盘中监控 ═══════════
MONITOR_ENABLED = True      # 启动时是否自动开启盘中监控
MONITOR_MODE = "close"      # "intraday"(触发即卖) | "close"(仅告警)
BROKER_ENABLED  = False     # 是否通过gateway执行真实券商委托（需 QMT/同花顺 已连接）
