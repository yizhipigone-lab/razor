import pandas as pd
from app.screener.strategies.base import BaseStrategy

class MACDStrategy(BaseStrategy):
    """
    智能选股：MACD 金叉 (水上模式)
    规则：DIFF > DEA 且 上一交易日 DIFF <= DEA，且 DEA > 0 (水上)。
    """
    def generate_signals(self, df: pd.DataFrame):
        if df.empty: return pd.DataFrame()
        bars = df.copy()
        bars.sort_values(["code", "date"], inplace=True)
        
        # 计算 MACD
        def calc_macd(group):
            ema12 = group["close"].ewm(span=12, adjust=False).mean()
            ema26 = group["close"].ewm(span=26, adjust=False).mean()
            diff = ema12 - ema26
            dea = diff.ewm(span=9, adjust=False).mean()
            macd = (diff - dea) * 2
            return pd.DataFrame({"diff": diff, "dea": dea, "macd": macd}, index=group.index)

        macd_df = bars.groupby("code", group_keys=False).apply(calc_macd)
        bars = pd.concat([bars, macd_df], axis=1)
        
        # 偏移判定
        bars["prev_diff"] = bars.groupby("code")["diff"].shift(1)
        bars["prev_dea"] = bars.groupby("code")["dea"].shift(1)
        
        # 核心逻辑
        gold_cross = (bars["diff"] > bars["dea"]) & (bars["prev_diff"] <= bars["prev_dea"])
        above_water = bars["dea"] > 0
        
        bars["buy_signal"] = gold_cross & above_water
        
        result = bars[bars["buy_signal"] == True][["code", "date", "close"]]
        return result