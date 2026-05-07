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
HARD_STOP      = -0.055       # 硬止损 -5.5%
TP1_PCT        = 0.04         # +4%
TP1_SELL_RATIO = 0.20         # 卖出20%
TP2_PCT        = 0.14         # +14% 清仓剩余
TRAIL_ACTIVATE = 0.08         # 移动止盈激活阈值
TRAIL_DD       = 0.02         # 移动止盈回撤距离
TIME_EXIT_DAYS = 7            # 时间条件退出
TIME_FORCE_DAYS = 10          # 时间强制退出
SAME_STOCK_COOLDOWN = 20      # 同股票冷却天数

# ═══════════ 策略选择 ═══════════
STRATEGY_NAME = "ma5_angle"

# ═══════════ 买入信号 ═══════════
SIGNAL_PARAMS = {
    "version": "improved",
    "filter_st": True,
    "filter_bj": True,
    "sh_red_filter": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
    "disable_quality_sort": True,
    "filter_consecutive_up": False,
    "filter_gap_quality": False,
}

# ═══════════ 模拟盘时间 ═══════════
BUY_TIME  = "14:52"   # 选股买入时间
SELL_TIME = "14:54"   # 止盈止损判断时间

# ═══════════ 回测/模拟区间 ═══════════
SIM_START = date(2022, 1, 4)
SIM_END   = date.today() + date.resolution  # 动态：始终到最新一天
LOAD_START = date(2022, 1, 1)

# ═══════════ 自动执行开关 ═══════════
AUTO_SELL = False   # 是否执行卖出（止盈止损）
AUTO_SCAN = True    # 是否执行选股（生成买入信号）
AUTO_BUY  = False   # 是否执行买入
SELL_MODE = "close"  # 卖出模式："intraday"(盘中Tick) | "close"(尾盘价)
