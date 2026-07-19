"""实盘自给自足选股器(2026-07-14:替代被动 /live/buy-signal)

封装 TdxBridge 选股 + QMT 实时价配价,产出 List[SignalItem]。
阻塞型(TdxBridge.execute_screen 是 subprocess,最长 600s),**必须在线程池调用**
(scheduler 用 asyncio.to_thread 包,绝不在事件循环线程同步调)。

进程内独立运行:TdxBridge 用 subprocess 对话通达信本地目录,不依赖主进程;
QmtWrapper 走进程内 xtdata 取 lastPrice 配价。

蓝本:app/scheduler/cron_jobs.py:441-464(模拟盘同款选股流程)。
"""
from typing import List, Optional, Tuple

from core.logger import get_logger

from .schemas import SignalItem

logger = get_logger("live_trader.signal_picker")


class SignalScreenError(Exception):
    """选股失败(TDX 返回非 ok / 异常),向上抛由 scheduler 决定告警等级"""


class SignalPicker:
    """实盘自给自足选股器(阻塞型,必须在线程池调用)。"""

    def __init__(self, qmt, config=None):
        self.qmt = qmt       # QmtWrapper(进程内,取 lastPrice 配价)
        self.config = config

    def screen_and_price(
        self,
        end_time: str,
        lookback_days: int = 500,
        formula_name: Optional[str] = None,
    ) -> Tuple[List[SignalItem], str, dict]:
        """选股 + 配价(阻塞)。返回 (signals, formula_name, meta)。

        Args:
            end_time: YYYYMMDD(调度方传 today)
            lookback_days: 选股回看天数(与模拟盘 cron_jobs.py:452 对齐,默认 500)
            formula_name: 公式名(None → 读 settings tqsdk.formula_name,默认 QUANTQQ)
        Returns:
            signals: List[SignalItem](code=QMT格式 + price=lastPrice),已剔除无价的
            formula_name: 实际用的公式名(透传,供日志/告警/strategy)
            meta: {matched_count, priced_count, skipped:[(code, reason)]}
        Raises:
            SignalScreenError: TDX 返回 status != ok

        注:此处 price(lastPrice)仅用于 _process_one_signal 的买入量估算;
            最终下单价由 OrderExecutor 对 TDX source 再次取 QMT 实时价覆盖
            (order_executor.py:99-109),双重保证价格新鲜度。
        """
        from app.tqsdk.bridge import TdxBridge, _get_formula_name
        from app.utils.xtquant_compat import format_code

        # 1. 选股(每次新建 TdxBridge,与模拟盘一致;构造时自动部署 worker)
        bridge = TdxBridge()
        fname = formula_name or _get_formula_name()
        sig_result = bridge.execute_screen(
            end_time=end_time,
            lookback_days=lookback_days,
            formula_name=fname,
        )
        if sig_result.get("status") != "ok":
            raise SignalScreenError(
                f"TDX 选股返回非 ok: {sig_result.get('message', sig_result)}"
            )

        matched = sig_result.get("matched", [])
        meta: dict = {
            "matched_count": len(matched),
            "priced_count": 0,
            "skipped": [],
        }

        if not matched:
            logger.info(f"SignalPicker 选股 0 命中(formula={fname})")
            return [], fname, meta

        # 2. 配价:进程内 QMT 取 lastPrice(注意是 lastPrice 非 close)
        #    matched 可能带后缀或裸码;统一去后缀后用 format_code 重加(禁止信任传入后缀,
        #    防 TDX 返回 000001.SH(上证指数)被当成平安银行(.SZ) 或反之)
        codes_fmt_unique: List[str] = []
        for code in matched:
            bare = code.split(".")[0] if "." in code else code
            fmt = format_code(bare)
            if fmt not in codes_fmt_unique:
                codes_fmt_unique.append(fmt)

        quotes = {}
        if self.qmt:
            try:
                quotes = self.qmt.get_realtime_quotes(codes_fmt_unique) or {}
            except Exception as e:
                logger.warning(f"SignalPicker 取实时价失败(无价的信号将被剔除): {e}")

        # 3. 逐只配价:price>0 才入 signals(参考 cron_jobs.py:462-464 的 px>0 过滤)
        signals: List[SignalItem] = []
        for code in matched:
            bare = code.split(".")[0] if "." in code else code
            code_fmt = format_code(bare)
            q = quotes.get(code_fmt) or {}
            try:
                price = float(q.get("lastPrice", 0) or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                signals.append(SignalItem(code=code_fmt, price=price))
                meta["priced_count"] += 1
            else:
                meta["skipped"].append((code_fmt, "无实时价"))

        logger.info(
            f"SignalPicker 选股完成: formula={fname} matched={meta['matched_count']} "
            f"priced={meta['priced_count']} skipped={len(meta['skipped'])}"
        )
        return signals, fname, meta
