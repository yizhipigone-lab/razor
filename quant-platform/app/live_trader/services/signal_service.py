"""买入信号处理服务(阶段3, 2026-07-19 从 main.py 抽离)。

含 process_buy_signals(并发内核 + 心跳 + 去重 + 幂等) + _process_one_signal(单信号处理)。

依赖:
  - 顶部: asyncio + date/datetime + _state + logger(沿用 live_trader.main, R6) + place_order_service(跨 services)
  - 函数内相对 import 用 .. 双点(services/ 比 main.py 深一级,审计 C1/A):schemas/buy_volume
  - 绝对路径不变: app.utils.xtquant_compat / core.settings / hashlib

⚠️ 心跳(L199 store.record_heartbeat) + scan_status 三值(ok/no_signal/all_rejected)
   与 scheduler.py 14:55 看门狗配套,搬迁绝不能漏/改(审计 R3/C)。

历史:原 main.py:108-273。
"""
import asyncio
from datetime import date, datetime

from core.logger import get_logger

from .._state import state as _state
from .order_service import place_order_service  # 跨 services:_process_one_signal L270 调

logger = get_logger("live_trader.main")  # 审计 R6:沿用 main 名


async def process_buy_signals(
    signals: list,
    strategy: str = "QUANTQQ",
    source: str = "TDX",
    lock_wait_sec: int = 5,
):
    """买入信号批量处理(共享内核)—— HTTP 端点 + scheduler 自给自足同源。

    保留全部副作用(与原 buy_signal 内核一致):
      - kill_switch 检查(active → 全拒,不抛)
      - 时点 cutoff 检查(now >= buy_signal_cutoff → 全拒,不抛)
      - 信号去重(format_code 统一 key,同 code 只留首条)
      - 并发信号量 3 + _process_one_signal(幂等键 + 尾盘定价,零改动复用)
      - 心跳 store.record_heartbeat("docker_tdx", count, scan_status)  ★必须保留★
        (否则 14:55 看门狗 scheduler.py:_check_signal_heartbeat 误报"无信号心跳")
    删除 HTTP 专属(鉴权 / BuySignalRequest 校验 / buy_enabled 检查 / HTTPException)。
    绝不 raise,失败转 rejected。

    Args:
        signals: List[SignalItem](pydantic 模型,有 .code/.price)
        strategy: 策略名(透传 _process_one_signal)
        source: "TDX" 决定 _process_one_signal 的 terminal/定价
        lock_wait_sec: 清仓锁等待(默认 5s,与原 buy_signal 一致)
    Returns:
        BuySignalResult(accepted/rejected/details)
    """
    from ..schemas import BuySignalResult, SignalResult  # 审计 C1:双点(原 main from .schemas)
    from app.utils.xtquant_compat import format_code

    config = _state.get("config")
    store = _state.get("store")

    # 1. kill_switch(必须保留;HTTP 路径已在端点 raise 403,这里是 scheduler 路径 + 防御)
    kill_switch = _state.get("kill_switch")
    if kill_switch and kill_switch.is_active():
        return BuySignalResult(
            accepted=[], rejected=[s.code for s in signals],
            details=[SignalResult(code=s.code, ok=False, status="forbidden",
                                  reason="kill switch 已激活") for s in signals])

    # 2. 时点 cutoff(必须保留,防尾盘过点乱下单)
    now_str = datetime.now().strftime("%H:%M")
    cutoff = config.buy_signal_cutoff if config else "14:59"
    if now_str >= cutoff:
        reason = f"尾盘已过({now_str} >= {cutoff}),信号丢弃"
        logger.warning(reason)
        return BuySignalResult(
            accepted=[], rejected=[s.code for s in signals],
            details=[SignalResult(code=s.code, ok=False, status="timeout", reason=reason)
                     for s in signals])

    # 3. 信号去重(format_code 统一 key,同 code 只留首条)
    seen_codes = set()
    unique_signals = []
    for s in signals:
        code_key = format_code(s.code) if '.' not in s.code else s.code
        if code_key not in seen_codes:
            seen_codes.add(code_key)
            unique_signals.append(s)
    if len(unique_signals) < len(signals):
        logger.info(f"信号去重: {len(signals)} -> {len(unique_signals)}")

    # 4. 并发处理(信号量3)+ _process_one_signal(零改动复用:幂等键+尾盘定价+下单)
    semaphore = asyncio.Semaphore(3)
    tasks = [_process_one_signal(s, semaphore, lock_wait_sec=lock_wait_sec,
                                 strategy_name=strategy) for s in unique_signals]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # 5. 汇总
    accepted, rejected, details = [], [], []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            code = unique_signals[i].code
            rejected.append(code)
            details.append(SignalResult(code=code, ok=False, status="error", reason=str(r)))
        else:
            code = r.get("code", unique_signals[i].code)
            if r.get("ok"):
                accepted.append(code)
            else:
                rejected.append(code)
            details.append(SignalResult(
                code=code, ok=r.get("ok", False), status=r.get("status", ""),
                reason=r.get("reason", ""), order_id=r.get("order_id"),
            ))

    # 6. 心跳(必须保留,否则 14:55 看门狗误报) — 审计 R3:scan_status 三值不可改
    if store:
        try:
            scan_status = "ok" if len(accepted) > 0 else (
                "no_signal" if len(unique_signals) == 0 else "all_rejected")
            store.record_heartbeat("docker_tdx", len(unique_signals), scan_status)
        except Exception as e:
            logger.warning(f"心跳记录失败: {e}")

    logger.info(f"buy-signal 处理完成: accepted={accepted} rejected={rejected}")
    return BuySignalResult(accepted=accepted, rejected=rejected, details=details)


async def _process_one_signal(signal, semaphore, lock_wait_sec: int = 5, strategy_name: str = "QUANTQQ") -> dict:
    """处理单个买入信号(在信号量控制下并发)"""
    from ..schemas import OrderIntent  # 审计 C1:双点(原 main from .schemas)
    from ..buy_volume import _calc_buy_volume  # 审计 C1:双点(原 main from .buy_volume)
    from app.utils.xtquant_compat import format_code

    async with semaphore:
        config = _state.get("config")

        code = signal.code
        code_fmt = format_code(code) if '.' not in code else code

        # 幂等键:不带时间戳,同天同股唯一(漏洞9修复)
        import hashlib
        client_order_id = hashlib.md5(
            f"buy_signal|{code_fmt}|{date.today()}".encode()
        ).hexdigest()[:16]

        # 计算买入量:本金×比例,卡在全局 [min_buy_amount, max_buy_amount] 之间
        # buy_position_ratio 走 runtime_state 可变 holder(热更新);min/max 走 settings trading 段(全局,与模拟盘同源)
        rs = _state.get("runtime_state")
        ratio = rs.buy_position_ratio if rs else (config.buy_position_ratio if config else 0.05)
        capital = config.live_capital if config else 0
        from core.settings import settings as _settings
        min_amt = float(_settings.get("trading", "min_buy_amount", default=5000))
        max_amt = float(_settings.get("trading", "max_buy_amount", default=60000))
        position_size = max(min_amt, min(ratio * capital, max_amt))
        price = signal.price  # service 内 TDX source 会用 QMT 实时价覆盖

        # 先用传入价格估算 volume(如果是0则用默认估算价)
        if price <= 0:
            price = 10.0  # 兜底估算价

        # 计算买入量
        volume = _calc_buy_volume(code_fmt, position_size, price)
        if volume <= 0:
            return {
                "code": code_fmt, "ok": False,
                "status": "error", "reason": f"计算买入量为0(price={price}, size={position_size})",
            }

        # 尾盘全时段统一对手方最优价(2026-07-19): 取代 14:55/14:57 时段分档,
        # 避免限价单挂在最新价不追价、尾盘拉升时买不到
        from app.utils.xtquant_compat import PRICE_TYPE_PEER_FIRST

        intent = OrderIntent(
            code=code_fmt,
            direction="buy",
            volume=volume,
            price=0,  # 对手最优为市价单, 由交易所按卖一价撮合, 无需指定价格
            price_type=PRICE_TYPE_PEER_FIRST,
            strategy_name=strategy_name,
            terminal="TDX",
            client_order_id=client_order_id,
            reason=f"TDX选股买入信号",
        )

        # 调用 service(TDX source, lock_wait=5s) — place_order_service 顶部 import 自 order_service
        # 用 asyncio.to_thread 避免阻塞事件循环(time.sleep in ClearanceLock)
        result = await asyncio.to_thread(
            place_order_service, intent, "TDX", lock_wait_sec
        )
        result["code"] = code_fmt
        return result
