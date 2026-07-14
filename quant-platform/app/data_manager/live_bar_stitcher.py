"""LiveBarStitcher 深 module(候选⑥)

设计:小接口大实现 — 把"实时行情缝合到今日未完成 K 线"逻辑抽 deep module。

调用方契约:
- 单 code(API 接口):stitcher.stitch_record(records, code, today_str) — 列表 in-place 改
- 多 code(sim_trader):stitcher.stitch_bars(bars, today) — 返回 (bars, snapshot)

核心共享:
- fetch_quotes():委托 quote_source(QMT→TDX→腾讯→Parquet 逐只降级)
- _build_live_bar():单个 quote dict → 单根 bar dict
- _merge_into_existing():用最新价更新已有今日 bar(high 取 max / low 取 min)

之前散点:
- app/api/system.py:160-212(get_bars 端点):单 code,inline live_bar 构造 + max/min
- app/sim_trader/data_loader.py:34-117(augment_bars_with_realtime):多 code,DataFrame + snapshot

两处都重复"拉 quote → build/merge today bar",本模块收口。

Q6 守约:严禁用现价伪造 last_close,缺价时 pct_chg=0。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional, Tuple

import pandas as pd

from core.logger import get_logger

log = get_logger("data_manager.live_bar_stitcher")


def _safe_float(v, default: float = 0.0) -> float:
    """NaN/None → default;数值 → float"""
    if v is None:
        return default
    try:
        f = float(v)
        # NaN 自反比较判定
        if f != f:
            return default
        return f
    except (ValueError, TypeError):
        return default


def _vol_active(today_vol: float, yest_vol: float) -> bool:
    """今日量 > 昨日量视为量能活跃;yest_vol=0 时一律 False(避免虚假信号)"""
    return bool(yest_vol > 0 and today_vol > yest_vol)


def _pct_chg(last: float, pre_close: float) -> float:
    """(现价 − 昨收)/昨收 × 100, 4 舍 5 入 2 位;pre_close 缺失/0 时返 0(Q6 守约)"""
    if pre_close is None or pre_close <= 0:
        return 0.0
    try:
        return round((last - pre_close) / pre_close * 100, 2)
    except (TypeError, ZeroDivisionError):
        return 0.0


class LiveBarStitcher:
    """实时行情缝合深 module — 单 code 与多 code 共用。

    使用:
        stitcher = LiveBarStitcher()
        # 单 code(API 端点)
        stitcher.stitch_record(records, "600000.SH", today_str="2026-07-13")
        # 多 code(sim_trader)
        bars, snapshot = stitcher.stitch_bars(bars_df, date(2026, 7, 13))
    """

    def fetch_quotes(self, codes: list[str]) -> dict[str, dict]:
        """委托 quote_source 深 module(QMT→TDX→腾讯→Parquet 逐只降级),
        输出 code → {price/open/high/low/volume/amount/last_close} 字典。
        """
        from app.data_manager.quote_source import get_realtime_quotes

        if not codes:
            return {}
        qdf = get_realtime_quotes(codes)
        if qdf is None or qdf.empty:
            return {}
        # 过滤掉 price<=0(缺价 source='missing')
        qdf = qdf[qdf["price"] > 0]
        if qdf.empty:
            return {}
        return {
            str(row["code"]): {
                "price": _safe_float(row.get("price")),
                "open": _safe_float(row.get("open")),
                "high": _safe_float(row.get("high")),
                "low": _safe_float(row.get("low")),
                "volume": _safe_float(row.get("volume")),
                "amount": _safe_float(row.get("amount")),
                "last_close": _safe_float(row.get("last_close")),  # Q6 守约
            }
            for _, row in qdf.iterrows()
        }

    def _build_live_bar(
        self, quote: dict, today_str: str,
        date_col: str = "date", yest_vol: float = 0.0,
    ) -> dict:
        """单个 quote dict 转 live bar dict — 第一根 today bar 用"""
        last_price = quote["price"]
        # Q6 守约:缺 last_close 时 pre_close=0,pct_chg=0(不用现价冒充)
        pre_close = _safe_float(quote.get("last_close"), 0)
        # open/high/low 缺失回退到 price(原 system.py 行为)
        return {
            date_col: today_str,
            "open": quote["open"] if quote["open"] > 0 else last_price,
            "high": quote["high"] if quote["high"] > 0 else last_price,
            "low": quote["low"] if quote["low"] > 0 else last_price,
            "close": last_price,
            "pre_close": pre_close,
            "volume": quote["volume"],
            "amount": quote.get("amount", 0),
            "pct_chg": _pct_chg(last_price, pre_close),
            "vol_active": _vol_active(quote["volume"], yest_vol),
        }

    def _merge_into_existing(self, bar: dict, quote: dict) -> dict:
        """用最新价 update 今日已有 bar — high 取 max / low 取 min"""
        bar["close"] = quote["price"]
        if quote["high"]:
            bar["high"] = max(bar.get("high", 0) or 0, quote["high"])
        if quote["low"]:
            bar["low"] = min(bar.get("low", 999999) or 999999, quote["low"])
        bar["volume"] = quote["volume"]
        # pre_close/pct_chg 用最新 quote 重算(可能 quote 比 bar 更新)
        pre_close = _safe_float(quote.get("last_close"), 0)
        if pre_close > 0:
            bar["pre_close"] = pre_close
            bar["pct_chg"] = _pct_chg(quote["price"], pre_close)
        return bar

    def stitch_record(
        self, records: list[dict], code: str,
        today_str: Optional[str] = None, date_col: str = "date",
    ) -> bool:
        """单 code API 接口 — 列表 in-place 缝合最后一根 bar。

        Returns:
            True=成功拉取 quote 且已缝合;False=无行情或失败(records 不变)。
        """
        if today_str is None:
            today_str = datetime.now().strftime("%Y-%m-%d")
        quotes = self.fetch_quotes([code])
        quote = quotes.get(code)
        if not quote:
            return False

        if not records or records[-1].get(date_col) != today_str:
            # 当前没有今日 bar → 追加
            yest_vol = records[-1].get("volume", 0) if records else 0
            records.append(self._build_live_bar(quote, today_str, date_col,
                                                 yest_vol=yest_vol))
        else:
            # 已有今日 bar → update
            self._merge_into_existing(records[-1], quote)
        return True

    def stitch_bars(
        self, bars: pd.DataFrame, today: date,
    ) -> Tuple[pd.DataFrame, dict]:
        """多 code sim_trader 接口 — DataFrame + 同时返回 snapshot。

        - today 非今日 → 返回 history snapshot,不改 bars
        - quote 缺失 → 返回 history snapshot,不改 bars
        - 正常 → 替换 today 那几行,返回 augmentation 结果 + snapshot
        """
        if bars is None or bars.empty or 'code' not in bars.columns:
            return bars, {}
        if today != date.today():
            return bars, _snapshot_from_history(bars, today)

        codes = bars['code'].unique().tolist()
        quotes = self.fetch_quotes(codes)
        if not quotes:
            return bars, _snapshot_from_history(bars, today)

        snapshot: dict[str, dict] = {}
        new_rows: list[dict] = []
        grouped = bars.groupby("code")
        for code, quote in quotes.items():
            if quote["price"] <= 0:
                continue
            if code not in grouped.groups:
                continue
            last_row = grouped.get_group(code).iloc[-1].to_dict()
            snapshot[code] = {
                'open': quote["open"] if quote["open"] > 0 else quote["price"],
                'high': quote["high"] if quote["high"] > 0 else quote["price"],
                'low': quote["low"] if quote["low"] > 0 else quote["price"],
                'close': quote["price"],
            }
            last_row['date'] = today
            last_row['open'] = snapshot[code]['open']
            last_row['high'] = snapshot[code]['high']
            last_row['low'] = snapshot[code]['low']
            last_row['close'] = snapshot[code]['close']
            last_row['volume'] = quote["volume"]
            new_rows.append(last_row)

        if not new_rows:
            return bars, snapshot
        # 替换今日那几行
        bars = bars[bars['date'] != today]
        ndf = pd.DataFrame(new_rows)
        bars = pd.concat([bars, ndf], ignore_index=True)
        bars = bars.sort_values(['code', 'date'])
        return bars, snapshot


def _snapshot_from_history(bars: pd.DataFrame, today: date) -> dict:
    """当日非今日 或 quote 全失败时,从 history 取 today snapshot(原 data_loader.py:161 实现)"""
    if bars is None or bars.empty:
        return {}
    day_bars = bars[bars["date"] == today]
    return {
        r['code']: {
            'open': float(r['open']), 'high': float(r['high']),
            'low': float(r['low']), 'close': float(r['close']),
        }
        for _, r in day_bars.iterrows()
    }
