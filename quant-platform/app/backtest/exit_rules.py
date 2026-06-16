"""
统一止盈止损规则引擎

所有规则独立、可测试、按优先级执行。同时服务日线和5分钟引擎。

用法：
    ctx = RuleContext(entry_price=10, peak_price=10.5, high=10.3, ...)
    signal = engine.check(ctx)
    if signal:
        # signal.reason, signal.sell_price, signal.sell_ratio
"""
from dataclasses import dataclass, field
from typing import Optional, List, Callable


def _pct(v: float) -> float:
    """自动识别百分比/小数格式：abs>1 时除以100，否则保持"""
    if abs(v) > 1:
        return v / 100.0
    return v


@dataclass
class ExitSignal:
    """一条退出信号"""
    reason: str           # 如 "HS", "TP1", "TR", "FD", "TC", "TF"
    sell_price: float     # 卖出价
    sell_ratio: float = 1.0  # 卖出比例（1.0=全卖，0.3=卖30%）


@dataclass
class RuleContext:
    """规则判断所需的全部上下文"""
    # 持仓数据
    entry_price: float
    peak_price: float
    shares: int = 0
    triggered_tiers: set = field(default_factory=set)  # 已触发的TP档位

    # 当前行情
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    atr: float = 0.0

    # 持仓天数（调用方计算，含首日之计=2，日历差之计=1）
    hold_days: int = 0
    # 首日对应的 hold_days 值：simple_runner/sim_trader=2，5m/日线回退=1
    first_day_hold_value: int = 2

    # 参数
    hard_stop: float = -0.06
    take_profit_tiers: list = field(default_factory=list)
    trail_activate: float = 0.05
    trail_dd: float = 0.02
    time_exit_days: int = 7
    time_exit_profit: float = 0.03
    time_force_days: int = 12
    first_day_exit_min_profit: float = 0.0
    first_day_exit_days: int = 1
    use_atr_trail: bool = False
    atr_trail_multiplier: float = 1.0

    # 保本止损
    breakeven_threshold: float = 0.0   # 盈利达此值激活保本（0=禁用）
    breakeven_stop: float = 0.0        # 激活后的止损位

    # 成交量高潮离场
    vol_climax_enabled: bool = False
    vol_climax_avg: float = 0.0        # 近20日均量
    vol_climax_day_vol: float = 0.0    # 当日累积量
    vol_climax_day_high: float = 0.0   # 当日最高
    vol_climax_day_low: float = 0.0    # 当日最低
    vol_climax_day_close: float = 0.0  # 当日收盘

    # 检测模式
    use_high_for_tp: bool = False  # True=用high检测止盈(5m引擎), False=用close
    # 同bar内TP已触发 → 跳过后续止损检查
    tp_triggered_this_bar: bool = False


# ═══════════════════════════════════════════════════
# 规则函数（每个返回 Optional[ExitSignal]，首个非 None 生效）
# ═══════════════════════════════════════════════════

def rule_hard_stop(ctx: RuleContext) -> Optional[ExitSignal]:
    """硬止损：用 Low 检测"""
    stop_price = ctx.entry_price * (1 + ctx.hard_stop)
    if ctx.low > 0 and ctx.low <= stop_price:
        return ExitSignal("HS", max(stop_price, ctx.open))
    # 兜底：用收盘价检测
    cur = ctx.close / ctx.entry_price - 1
    if cur <= ctx.hard_stop:
        return ExitSignal("HS", ctx.close)
    return None


def rule_first_day_exit(ctx: RuleContext) -> Optional[ExitSignal]:
    """首日弱势离场：前N个有效交易日最高价未达目标则强制卖出"""
    fd = ctx.first_day_exit_min_profit
    fd_days = ctx.first_day_exit_days
    if fd <= 0:
        return None
    ft = ctx.first_day_hold_value  # 首日对应的 hold_days（含首日=2，日历差=1）
    if ctx.hold_days < ft or ctx.hold_days > ft + fd_days - 1:
        return None
    day_high_pct = ctx.high / ctx.entry_price - 1
    if day_high_pct < fd:
        return ExitSignal(
            f"FD({day_high_pct*100:.1f}%)",
            ctx.close,
        )
    return None


def rule_time_force(ctx: RuleContext) -> Optional[ExitSignal]:
    """强制时间退出：持仓超 N 天无条件清仓"""
    if ctx.hold_days > ctx.time_force_days:
        return ExitSignal("TF", ctx.close)
    return None


def rule_take_profit(ctx: RuleContext) -> Optional[ExitSignal]:
    """多档阶梯止盈：按顺序触发，每档只触发一次"""
    tiers = ctx.take_profit_tiers
    if not tiers:
        return None

    for idx, tier in enumerate(tiers):
        if idx in ctx.triggered_tiers:
            continue

        tp_pct = tier.get("profit_pct", 999)
        target = ctx.entry_price * (1 + tp_pct)

        if ctx.use_high_for_tp:
            triggered = ctx.high >= target
            sell_px = target
        else:
            triggered = ctx.close >= target
            sell_px = ctx.close

        if triggered:
            sell_ratio = tier.get("sell_ratio", 1.0)
            label = f"TP{idx + 1}"
            return ExitSignal(label, sell_px, sell_ratio)

    return None


def rule_trailing_stop(ctx: RuleContext) -> Optional[ExitSignal]:
    """移动止盈：峰值盈利超激活线后，从峰值回撤超阈值触发"""
    peak_pct = ctx.peak_price / ctx.entry_price - 1

    if peak_pct < ctx.trail_activate:
        return None

    # ATR 动态回撤
    eff_dd = ctx.trail_dd
    if ctx.use_atr_trail and ctx.atr > 0:
        atr_pct = ctx.atr_trail_multiplier * ctx.atr / ctx.entry_price
        eff_dd = max(ctx.trail_dd, atr_pct)

    # 用 Low 检测回撤触发
    if ctx.low > 0:
        dd_from_peak = ctx.low / ctx.peak_price - 1
        if dd_from_peak <= -eff_dd:
            trail_price = ctx.peak_price * (1 - eff_dd)
            return ExitSignal("TR", max(trail_price, ctx.open))

    # 兜底：用 close 检测
    dd_close = ctx.close / ctx.peak_price - 1
    if dd_close <= -eff_dd:
        return ExitSignal("TR", ctx.close)

    return None


def rule_time_condition(ctx: RuleContext) -> Optional[ExitSignal]:
    """时间条件退出：持仓超 N 天且盈利达标"""
    if ctx.hold_days > ctx.time_exit_days:
        cur = ctx.close / ctx.entry_price - 1
        if cur > ctx.time_exit_profit:
            return ExitSignal("TC", ctx.close)
    return None


def rule_breakeven_stop(ctx: RuleContext) -> Optional[ExitSignal]:
    """保本止损：最高盈利曾达阈值后，回落到保本线就卖"""
    if ctx.breakeven_threshold <= 0:
        return None
    peak_pct = ctx.peak_price / ctx.entry_price - 1
    if peak_pct < ctx.breakeven_threshold:
        return None
    # 用 Low 检测（盘中触发），兜底 close
    if ctx.low > 0 and ctx.low <= ctx.entry_price * (1 + ctx.breakeven_stop):
        return ExitSignal("BE", max(ctx.entry_price * (1 + ctx.breakeven_stop), ctx.open))
    if ctx.close <= ctx.entry_price * (1 + ctx.breakeven_stop):
        return ExitSignal("BE", ctx.close)
    return None


def rule_vol_climax_exit(ctx: RuleContext) -> Optional[ExitSignal]:
    """成交量高潮离场：天量+弱势收盘 → 出货日"""
    if not ctx.vol_climax_enabled or ctx.vol_climax_avg <= 0:
        return None
    if ctx.vol_climax_day_vol <= 0:
        return None
    # 量 > 3倍均量 且 收盘在日线低位 < 40%
    if ctx.vol_climax_day_vol > ctx.vol_climax_avg * 3.0:
        rng = ctx.vol_climax_day_high - ctx.vol_climax_day_low
        if rng > 0:
            close_pos = (ctx.vol_climax_day_close - ctx.vol_climax_day_low) / rng
            if close_pos < 0.4:
                return ExitSignal(
                    f"CLIMAX({ctx.vol_climax_day_vol/ctx.vol_climax_avg:.1f}x)",
                    ctx.vol_climax_day_close,
                )
    return None


# ═══════════════════════════════════════════════════
# 规则引擎
# ═══════════════════════════════════════════════════

# 全局规则注册表（按优先级从高到低）
ALL_RULES: List[tuple] = [
    (100, rule_hard_stop,       "硬止损",          False),
    (95,  rule_breakeven_stop,  "保本止损",         False),
    (90,  rule_first_day_exit,  "首日弱势离场",     True),   # 仅14:52生效
    (80,  rule_time_force,      "强制时间退出",     False),
    (60,  rule_take_profit,     "多档阶梯止盈",     False),
    (40,  rule_trailing_stop,   "移动止盈",         False),
    (20,  rule_time_condition,  "时间条件退出",     False),
    (10,  rule_vol_climax_exit, "成交量高潮离场",   False),
]


class ExitRuleEngine:
    """统一止盈止损规则引擎"""

    def __init__(self, rules: list = None):
        self._rules = rules or ALL_RULES
        # 按优先级降序排列
        self._rules.sort(key=lambda r: r[0], reverse=True)

    def check(self, ctx: RuleContext, skip_eod_only: bool = False) -> Optional[ExitSignal]:
        """按优先级依次检查。skip_eod_only=True 时跳过"仅尾盘"规则（盘前盘中不触发FD等）"""
        for priority, rule_fn, _name, eod_only in self._rules:
            if skip_eod_only and eod_only:
                continue
            try:
                signal = rule_fn(ctx)
                if signal is not None:
                    return signal
            except Exception:
                continue
        return None

    @staticmethod
    def build_context(pos, bar: dict, hold_days: int,
                      params: dict, use_high_for_tp: bool = False,
                      first_day_hold_value: int = 2) -> RuleContext:
        """从持仓对象 + bar数据 + 参数 构建 RuleContext

        first_day_hold_value: 首日对应的 hold_days 值
          = 2: simple_runner / sim_trader（含首日计）
          = 1: 5m引擎 / 日线回退（日历差/排他计）
        """
        tp_tiers = params.get("take_profit_tiers", [])
        triggered = getattr(pos, "tp_triggered", None)
        if triggered is None:
            triggered = set()
            if getattr(pos, "tp1_triggered", False):
                triggered.add(0)
            if getattr(pos, "tp2_triggered", False):
                triggered.add(1)

        return RuleContext(
            entry_price=pos.entry_price,
            peak_price=pos.peak_price if hasattr(pos, 'peak_price') else pos.entry_price,
            shares=getattr(pos, 'shares', getattr(pos, 'remaining_shares', 0)),
            triggered_tiers=triggered if isinstance(triggered, set) else set(triggered),
            open=float(bar.get("open", bar.get("close", 0))),
            high=float(bar.get("high", bar.get("close", 0))),
            low=float(bar.get("low", bar.get("close", 0))),
            close=float(bar.get("close", 0)),
            atr=float(bar.get("atr", 0)),
            hold_days=hold_days,
            first_day_hold_value=first_day_hold_value,
            hard_stop=params.get("hard_stop", -0.06),
            take_profit_tiers=tp_tiers,
            trail_activate=params.get("trail_activate", 0.05),
            trail_dd=params.get("trail_dd", 0.02),
            time_exit_days=params.get("time_exit_days", 7),
            time_exit_profit=params.get("time_exit_profit", 0.03),
            time_force_days=params.get("time_force_days", 12),
            first_day_exit_min_profit=params.get("first_day_exit_min_profit", 0.0),
            first_day_exit_days=params.get("first_day_exit_days", 1),
            use_atr_trail=params.get("use_atr_trail", True),
            atr_trail_multiplier=params.get("atr_trail_multiplier", 1.0),
            breakeven_threshold=_pct(params.get("breakeven_threshold_pct", 0.0)),
            breakeven_stop=_pct(params.get("breakeven_stop_pnl_pct", 0.0)),
            vol_climax_enabled=params.get("use_vol_climax_exit", False),
            use_high_for_tp=use_high_for_tp,
        )


def adjust_for_gap(code: str, entry_price: float, peak_price: float,
                   close: float, prev_close: float) -> tuple:
    """除权跳空保护：修正成本价和峰值价。返回 (new_entry, new_peak)"""
    if prev_close <= 0 or close <= 0:
        return entry_price, peak_price
    gap = close / prev_close - 1
    prefix = code[:3] if len(code) >= 3 else code
    if prefix in ('300', '301', '688'):
        limit = -0.12  # 创业板/科创板除权更频繁，阈值收紧
    elif prefix[0] == '8':
        limit = -0.30
    else:
        limit = -0.10
    if gap <= limit:
        ratio = close / prev_close
        return entry_price * ratio, peak_price * ratio
    return entry_price, peak_price


import os
from pathlib import Path as _Path


def atomic_write_parquet(df, path: str):
    """原子写入 parquet：先写 .tmp 再 rename，避免半截文件"""
    target = _Path(path)
    tmp = target.with_suffix('.parquet.tmp')
    df.to_parquet(str(tmp))
    os.replace(str(tmp), str(target))  # 原子 rename


def cleanup_tmp_parquet(dir_path: str):
    """清理残留的 .parquet.tmp 文件"""
    for f in _Path(dir_path).rglob('*.parquet.tmp'):
        try:
            f.unlink()
        except Exception:
            pass


# 全局单例
exit_rule_engine = ExitRuleEngine()
