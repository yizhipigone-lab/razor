import pandas as pd
from app.screener.strategies.base import BaseStrategy


class ConsecutiveUpMacdUnderwaterStrategy(BaseStrategy):
    """
    连涨四天 + 水下MACD金叉

    条件：
    1. 连涨 4 天：今日收盘 > 昨日收盘，连续 4 日
    2. 水下 MACD 金叉：DIFF 上穿 DEA，且 DIFF < 0、DEA < 0（水下）
    """

    name = "连涨四天+水下MACD金叉"
    description = "连续上涨 4 天，MACD 在水下（零轴下方）形成金叉，倾向超跌反弹启动"

    def generate_signals(self, df: pd.DataFrame):
        if df.empty:
            return pd.DataFrame()

        bars = df.copy()
        bars.sort_values(["code", "date"], inplace=True)

        g = bars.groupby("code", group_keys=False)

        # ── 1. 连涨四天：今日收盘 > 昨日收盘，连涨 4 日 ──
        bars["prev_close_1"] = g["close"].shift(1)
        bars["prev_close_2"] = g["close"].shift(2)
        bars["prev_close_3"] = g["close"].shift(3)
        bars["prev_close_4"] = g["close"].shift(4)

        bars["up1"] = bars["close"] > bars["prev_close_1"]
        bars["up2"] = bars["prev_close_1"] > bars["prev_close_2"]
        bars["up3"] = bars["prev_close_2"] > bars["prev_close_3"]
        bars["up4"] = bars["prev_close_3"] > bars["prev_close_4"]

        bars["consecutive_up_4"] = bars["up1"] & bars["up2"] & bars["up3"] & bars["up4"]

        # ── 2. MACD 计算 ──
        def calc_macd(group):
            ema12 = group["close"].ewm(span=12, adjust=False).mean()
            ema26 = group["close"].ewm(span=26, adjust=False).mean()
            diff = ema12 - ema26
            dea = diff.ewm(span=9, adjust=False).mean()
            macd = (diff - dea) * 2
            return pd.DataFrame({"diff": diff, "dea": dea, "macd": macd}, index=group.index)

        macd_df = bars.groupby("code", group_keys=False)[["close"]].apply(calc_macd, include_groups=False)
        bars = pd.concat([bars, macd_df], axis=1)

        # ── 3. 金叉判定（重新 groupby，确保 diff/dea 列可见） ──
        g = bars.groupby("code", group_keys=False)
        bars["prev_diff"] = g["diff"].shift(1)
        bars["prev_dea"] = g["dea"].shift(1)

        # 金叉：DIFF 上穿 DEA（今日 diff > dea 且昨日 diff <= dea）
        gold_cross = (bars["diff"] > bars["dea"]) & (bars["prev_diff"] <= bars["prev_dea"])

        # 水下：DEA < 0 且 DIFF < 0
        underwater = (bars["dea"] < 0) & (bars["diff"] < 0)

        # 综合信号
        bars["buy_signal"] = bars["consecutive_up_4"] & gold_cross & underwater

        result = bars[bars["buy_signal"] == True][["code", "date", "close"]].copy()
        return result
