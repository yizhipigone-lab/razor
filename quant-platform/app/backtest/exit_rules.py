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

from core.logger import get_logger

_log = get_logger("ExitRules")

def _pct(v: float) -> float:
    """[DEPRECATED 2026-07-15] 自动识别百分比/小数格式：abs>1 时除以100,否则保持。

    ⚠️ 已废弃:risk_params.py 集中加载层保证所有风控参数输出为小数(0.03 表示 3%)。
    build_context 不再走本函数,直接拿小数。
    保留仅作向后兼容(老调用方可能传百分比整数,abs>1 时还能救一下)。
    新代码禁止使用本函数 — 传单位不一致的值是 bug 信号。
    """
    import warnings
    warnings.warn(
        "_pct() is deprecated, use risk_params.py for unit conversion",
        DeprecationWarning, stacklevel=2,
    )
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

    # 成交价假设："stop"=纯止损线(对齐VERA,默认); "min"=真实(min(stop,open)跳空低开按开盘); False/"max"=旧乐观(max(stop,open))
    realistic_stop_fill: str = "stop"

    # 阶梯止盈模式：False=首个生效(旧行为,一根K线只触发一档); True=叠加(同bar所有档位全触发,按最高档成交,ratio累加)
    tp_stack_mode: bool = True
    # TP1(idx=0)成交价固定比例：0=用原逻辑(target/close); 0.03=按3%成交(对齐VERA,VERA的TP1成交价=entry*1.03)
    tp1_fill_pct: float = 0.03

    # 优先级模式："stop_first"(止损优先) / "trailing_first"(止盈>移动>止损,对齐VERA,默认)
    # trailing_first 下盘中触及止损线但同日也触及止盈目标时，止盈先触发卖部分，不全仓止损
    priority_mode: str = "trailing_first"


# ═══════════════════════════════════════════════════
# 规则函数（每个返回 Optional[ExitSignal]，首个非 None 生效）
# ═══════════════════════════════════════════════════

def rule_hard_stop(ctx: RuleContext) -> Optional[ExitSignal]:
    """硬止损：用 Low 检测(T+1: 持仓<2日不触发, 隔夜跳空才能卖)"""
    if ctx.hold_days < 2:
        return None
    stop_price = ctx.entry_price * (1 + ctx.hard_stop)
    if ctx.low > 0 and ctx.low <= stop_price:
        # 成交价假设: False/"max"=旧乐观(max(stop,open)); True/"min"=真实(min); "stop"=纯止损线(对齐VERA)
        if ctx.realistic_stop_fill == "stop":
            fill = stop_price
        elif ctx.realistic_stop_fill:
            fill = min(stop_price, ctx.open)
        else:
            fill = max(stop_price, ctx.open)
        return ExitSignal("HS", fill)
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
    """多档阶梯止盈(T+1: 持仓<2日不触发)

    tp_stack_mode=False(旧): 首个非None生效，一根K线只触发一档
    tp_stack_mode=True(叠加,对齐VERA): 同bar内所有未触发档位全触发，按最高档成交价，sell_ratio累加(钳位1.0)
    """
    if ctx.hold_days < 2:
        return None
    tiers = ctx.take_profit_tiers
    if not tiers:
        return None

    if ctx.tp_stack_mode:
        # 叠加模式：收集本bar所有新触发档位
        new_hits = []
        for idx, tier in enumerate(tiers):
            if idx in ctx.triggered_tiers:
                continue
            tp_pct = tier.get("profit_pct", 999)
            target = ctx.entry_price * (1 + tp_pct)
            triggered = (ctx.high >= target) if ctx.use_high_for_tp else (ctx.close >= target)
            if triggered:
                new_hits.append((idx, tier, target))
        if not new_hits:
            return None
        # 按档位降序（最高档在前），取最高档成交价，ratio累加
        new_hits.sort(key=lambda x: x[0], reverse=True)
        highest_idx, _highest_tier, highest_target = new_hits[0]
        # TP1 按 tp1_fill_pct 成交（对齐VERA 3%）
        if highest_idx == 0 and ctx.tp1_fill_pct > 0:
            highest_target = ctx.entry_price * (1 + ctx.tp1_fill_pct)
        total_ratio = min(sum(h[1].get("sell_ratio", 1.0) for h in new_hits), 1.0)
        for idx, _, _ in new_hits:
            ctx.triggered_tiers.add(idx)
        return ExitSignal(f"TP{highest_idx + 1}", highest_target, total_ratio)

    # 旧模式：首个生效
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
            # TP1 按 tp1_fill_pct 成交（对齐VERA 3%）
            if idx == 0 and ctx.tp1_fill_pct > 0:
                sell_px = ctx.entry_price * (1 + ctx.tp1_fill_pct)
            sell_ratio = tier.get("sell_ratio", 1.0)
            label = f"TP{idx + 1}"
            return ExitSignal(label, sell_px, sell_ratio)
    return None


def rule_trailing_stop(ctx: RuleContext) -> Optional[ExitSignal]:
    """移动止盈：峰值盈利超激活线后，从峰值回撤超阈值触发(T+1)"""
    if ctx.hold_days < 2:
        return None
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
            # 成交价假设: False/"max"=旧乐观; True/"min"=真实; "stop"=纯回撤线(对齐VERA)
            if ctx.realistic_stop_fill == "stop":
                fill = trail_price
            elif ctx.realistic_stop_fill:
                fill = min(trail_price, ctx.open)
            else:
                fill = max(trail_price, ctx.open)
            return ExitSignal("TR", fill)

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
    """保本止损：最高盈利曾达阈值后，回落到保本线就卖(T+1)"""
    if ctx.hold_days < 2:
        return None
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

# trailing_first 模式：阶梯止盈 > 移动止盈 > 硬止损（对齐VERA priority=trailing_first）
# 盘中触及止损线但同日也触及止盈目标时，止盈先触发卖部分，不全仓止损出局
ALL_RULES_TRAILING: List[tuple] = [
    (110, rule_take_profit,     "多档阶梯止盈",     False),
    (105, rule_trailing_stop,   "移动止盈",         False),
    (100, rule_hard_stop,       "硬止损",          False),
    (95,  rule_breakeven_stop,  "保本止损",         False),
    (90,  rule_first_day_exit,  "首日弱势离场",     True),
    (80,  rule_time_force,      "强制时间退出",     False),
    (20,  rule_time_condition,  "时间条件退出",     False),
    (10,  rule_vol_climax_exit, "成交量高潮离场",   False),
]


class ExitRuleEngine:
    """统一止盈止损规则引擎"""

    def __init__(self, rules: list = None):
        self._rules = rules or ALL_RULES
        # 按优先级降序排列
        self._rules.sort(key=lambda r: r[0], reverse=True)
        self._rules_trailing = sorted(ALL_RULES_TRAILING, key=lambda r: r[0], reverse=True)

    def check(self, ctx: RuleContext, skip_eod_only: bool = False) -> Optional[ExitSignal]:
        """按优先级依次检查。skip_eod_only=True 时跳过"仅尾盘"规则（盘前盘中不触发FD等）
        根据 ctx.priority_mode 选择规则集：trailing_first 止盈优先于止损"""
        rules = self._rules_trailing if getattr(ctx, 'priority_mode', 'stop_first') == 'trailing_first' else self._rules
        for priority, rule_fn, _name, eod_only in rules:
            if skip_eod_only and eod_only:
                continue
            try:
                signal = rule_fn(ctx)
                if signal is not None:
                    return signal
            except Exception:
                # 不静默吞：记录规则名与关键上下文，避免"该止损没止损"被掩盖
                _log.error(
                    f"退出规则 {_name} 执行异常 "
                    f"(entry={getattr(ctx, 'entry_price', '?')}, "
                    f"close={getattr(ctx, 'close', '?')}, "
                    f"hold_days={getattr(ctx, 'hold_days', '?')})",
                    exc_info=True,
                )
                continue
        return None

    def check_all(self, ctx: RuleContext, skip_eod_only: bool = False) -> list:
        """trailing_first 顺序执行（对齐VERA engine.py:186）：ladder部分卖后继续检查trailing/cost_stop，
        用剩余仓位。返回 ExitSignal 列表（可能多个，如 ladder部分卖 + trailing全卖剩余）。
        非trailing_first 退化为首个生效（返回0或1个元素）。"""
        if getattr(ctx, 'priority_mode', 'stop_first') != 'trailing_first':
            sig = self.check(ctx, skip_eod_only)
            return [sig] if sig else []
        signals = []
        remaining = 1.0  # 剩余仓位比例
        for priority, rule_fn, _name, eod_only in self._rules_trailing:
            if skip_eod_only and eod_only:
                continue
            try:
                signal = rule_fn(ctx)
                if signal is None:
                    continue
                # 全卖类（trailing/cost_stop/保本/时间等 ratio>=1.0）：卖剩余仓位，结束
                if signal.sell_ratio >= 1.0:
                    signal.sell_ratio = remaining if remaining < 1.0 else 1.0
                    signals.append(signal)
                    break
                # 部分卖类（ladder ratio<1.0）：记录，继续检查后续规则
                if signal.sell_ratio > remaining:
                    signal.sell_ratio = remaining
                signals.append(signal)
                remaining -= signal.sell_ratio
                if remaining <= 0.001:
                    break
            except Exception:
                _log.error(
                    f"退出规则 {_name} 执行异常 "
                    f"(entry={getattr(ctx, 'entry_price', '?')}, "
                    f"close={getattr(ctx, 'close', '?')}, "
                    f"hold_days={getattr(ctx, 'hold_days', '?')})",
                    exc_info=True,
                )
                continue
        return signals

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
            breakeven_threshold=params.get("breakeven_threshold_pct", 0.0),
            breakeven_stop=params.get("breakeven_stop_pnl_pct", 0.0),
            vol_climax_enabled=params.get("use_vol_climax_exit", False),
            use_high_for_tp=use_high_for_tp,
            realistic_stop_fill=params.get("realistic_stop_fill", "stop"),
            tp_stack_mode=params.get("tp_stack_mode", True),
            tp1_fill_pct=params.get("tp1_fill_pct", 0.03),
            priority_mode=params.get("priority_mode", "trailing_first"),
        )


def adjust_for_gap(code: str, entry_price: float, peak_price: float,
                   close: float, prev_close: float) -> tuple:
    """除权跳空保护：修正成本价和峰值价。返回 (new_entry, new_peak)"""
    if prev_close <= 0 or close <= 0:
        return entry_price, peak_price
    gap = close / prev_close - 1
    prefix = code[:3] if len(code) >= 3 else code
    # H8(2026-07-15 全项目审计): 阈值改为【超过跌停幅度】才判除权, 避免正常跌停被误判。
    # 旧版主板 -0.10 / 创业科创 -0.12 / 北证 -0.30, 其中 -0.10/-0.12 恰好等于/小于
    # 跌停幅度, 导致正常跌停日被永久下调 entry/peak → P&L 失真。
    # 新阈值取跌停幅度再过 1%: 主板 -0.11, 创业科创 -0.21, 北证 -0.31。
    # M1: 北证 4xx 原走错分支(prefix[0]=='4' 落 else -0.10), 现并入 ('8','4') → -0.31。
    if prefix in ('300', '301', '688'):
        limit = -0.21   # 创业板/科创板跌停-20%, 超过才判除权
    elif prefix[0] in ('8', '4'):
        limit = -0.31   # 北证(8xx/4xx)跌停-30%, 超过才判除权
    else:
        limit = -0.11   # 主板跌停-10%, 超过才判除权
    if gap <= limit:
        ratio = close / prev_close
        return entry_price * ratio, peak_price * ratio
    return entry_price, peak_price


# 全局单例
exit_rule_engine = ExitRuleEngine()
