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
# H6(2026-07-15 全项目审计): 风控参数单源 — 从 risk_params(读 app_setting.json 的 risk 段)派生,
# 与实盘(live exit_monitor)/sim_trader check_stops 共用同一真相源, 杜绝回测与实盘参数漂移。
# 旧版 11 个常量各自硬编码/读 backtest 段, 与 risk 段漂移(USE_ATR_TRAIL True vs False、
# ATR 倍数 1.0 vs 2.5、TAKE_PROFIT_TIERS 单档 vs 双档)→ 回测结果与实盘不可比。
from app.config.risk_params import load_risk_params as _load_risk_params
_RP = _load_risk_params()
HARD_STOP      = _RP.hard_stop                # 硬止损(小数, 如 -0.046)
TRAIL_ACTIVATE = _RP.trail_activate            # 移动止盈激活阈值
TRAIL_DD       = _RP.trail_dd                  # 移动止盈回撤距离
TIME_EXIT_DAYS = _RP.time_exit_days            # 时间条件退出天数

# ATR 动态移动止盈: 启用后 TRAIL_DD = max(TRAIL_DD, ATR_TRAIL_MUL * ATR(14) / entry_price)
USE_ATR_TRAIL = _RP.use_atr_trail              # 是否用 ATR 动态调整移动止盈回撤(对齐实盘)
ATR_TRAIL_MULTIPLIER = _RP.atr_trail_multiplier  # ATR 倍数(对齐实盘)
TIME_EXIT_PROFIT = _RP.time_exit_profit        # 时间条件退出盈利阈值
TIME_FORCE_DAYS = _RP.time_force_days          # 时间强制退出天数
SAME_STOCK_COOLDOWN = 20      # 同股票冷却天数(不在 RiskParams, 保留硬编码; 回测/sim 共用)
FIRST_DAY_EXIT_MIN_PROFIT = _RP.first_day_exit_min_profit  # 首日弱势离场阈值(0=禁用)
FIRST_DAY_EXIT_DAYS = _RP.first_day_exit_days  # 首日弱势离场检查天数

# 多档阶梯止盈: 按顺序触发，每档卖出剩余仓位的 sell_ratio%
# 触发过的不再重复，剩余仓位最终由 TR 移动止盈保护
TAKE_PROFIT_TIERS = _RP.take_profit_tiers      # 对齐实盘 risk 段(双档)

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

# ═══════════ 盘中监控 ═══════════
MONITOR_ENABLED = True      # 启动时是否自动开启盘中监控
MONITOR_MODE = "close"      # "intraday"(触发即卖) | "close"(仅告警)
# BROKER_ENABLED 已删除(2026-07-14):sim_trader 永远不真下单,真单唯一入口是 live_trader(qmt_wrapper)
# 见 docs/审计报告/项目质量审计_2026-07-13_全项目.md 架构决定
