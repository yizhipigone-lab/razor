import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy


class SharpMACDKDJStrategy(BaseStrategy):
    """
    连涨两日 + MACD金叉 + KDJ金叉 + 上影线过滤

    信号逻辑（通达信原公式转写）：
      DIF = (EMA(CLOSE,5) - EMA(CLOSE,10)) * 100
      DEA = EMA(DIF, 5)
      CROSS(DIF, DEA)  — MACD 金叉
      CROSS(KDJ.K, KDJ.D) — KDJ 金叉
      连涨 2 日、收阳、上影线不过长、涨幅 < 7%
      股价 < 40，非 ST/科创板/三板，首日信号
    """

    name = "快MACD+KDJ双金叉"
    description = "双金叉共振：快线MACD(5,10,5)金叉 + KDJ(9,3,3)金叉 + 连涨2日 + 上影线过滤"

    def generate_signals(self, df: pd.DataFrame):
        if df.empty:
            return pd.DataFrame()

        bars = df.copy()
        bars.sort_values(["code", "date"], inplace=True)

        # ── 过滤条件：非科创板/三板 ──
        bars["_code_prefix"] = bars["code"].str[:3]
        bars["_bad_code"] = bars["_code_prefix"].isin(["688", "430", "873", "920"])
        bars = bars[~bars["_bad_code"]].copy()

        if bars.empty:
            return pd.DataFrame()

        g = bars.groupby("code", group_keys=False)

        # ═══ 1. DIF / DEA（快线MACD） ═══
        def calc_fast_macd(group):
            ema5 = group["close"].ewm(span=5, adjust=False).mean()
            ema10 = group["close"].ewm(span=10, adjust=False).mean()
            dif = (ema5 - ema10) * 100
            dea = dif.ewm(span=5, adjust=False).mean()
            return pd.DataFrame({"dif": dif, "dea": dea}, index=group.index)

        macd_df = bars.groupby("code", group_keys=False)[["close"]].apply(
            calc_fast_macd, include_groups=False
        )
        bars = pd.concat([bars, macd_df], axis=1)

        # ═══ 2. KDJ(9,3,3) ═══
        def calc_kdj(group):
            low9 = group["low"].rolling(9).min()
            high9 = group["high"].rolling(9).max()
            rsv = (group["close"] - low9) / (high9 - low9 + 1e-9) * 100
            k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
            d = k.ewm(alpha=1 / 3, adjust=False).mean()
            return pd.DataFrame({"k": k, "d": d}, index=group.index)

        kdj_df = bars.groupby("code", group_keys=False)[["low", "high", "close"]].apply(
            calc_kdj, include_groups=False
        )
        bars = pd.concat([bars, kdj_df], axis=1)

        # ═══ 3. K线形态特征 ═══
        g = bars.groupby("code", group_keys=False)
        bars["prev_close"] = g["close"].shift(1)
        bars["prev_dif"] = g["dif"].shift(1)
        bars["prev_dea"] = g["dea"].shift(1)
        bars["prev_k"] = g["k"].shift(1)
        bars["prev_d"] = g["d"].shift(1)

        # 上影线
        bars["upper_shadow"] = bars["high"] - bars[["close", "open"]].max(axis=1)
        bars["body"] = (bars["close"] - bars["open"]).abs()
        bars["shadow_ok"] = ~(bars["upper_shadow"] > bars["body"] * 0.8)

        # 连涨 2 日（UPNDAY）
        bars["upnday"] = (bars["close"] > bars["prev_close"]) & (
            bars["prev_close"] > g["close"].shift(2)
        )

        # ═══ 4. 综合信号 ═══
        cond_price = bars["close"] < 40
        cond_rise = bars["close"] / bars["prev_close"] < 1.07
        cond_positive = bars["close"] > bars["open"]
        cond_macd_cross = (bars["dif"] > bars["dea"]) & (bars["prev_dif"] <= bars["prev_dea"])
        cond_kdj_cross = (bars["k"] > bars["d"]) & (bars["prev_k"] <= bars["prev_d"])

        zt = (
            cond_rise
            & bars["upnday"]
            & bars["shadow_ok"]
            & cond_price
            & cond_positive
            & cond_macd_cross
            & cond_kdj_cross
        )

        # 首日信号：10 日内首次出现
        bars["zt_flag"] = zt.astype(int)
        bars["zt_count_10"] = g["zt_flag"].transform(lambda x: x.rolling(10).sum())
        bars["buy_signal"] = zt & (bars["zt_count_10"] == 1)

        result = bars[bars["buy_signal"] == True][["code", "date", "close"]].copy()
        return result
