"""
盘整突破策略
逻辑：阴转阳后N天内横盘不破低点 + 放量突破前高 + 5连阳背景
"""
import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame,
                     N: int = 5,
                     ZF: float = 8.0,
                     filter_st: bool = True,
                     filter_bj: bool = True,
                     skip_limit_up: bool = True,
                     ) -> pd.DataFrame:
    """
    盘整突破选股

    参数:
      df: 全市场K线
      N: 横盘最大天数（默认5）
      ZF: 突破涨幅阈值%（默认8%）
      filter_st: 过滤ST
      filter_bj: 过滤北交所
      skip_limit_up: 涨停过滤
    """
    if df is None or len(df) < 60:
        return pd.DataFrame()

    df = df.copy()

    # ── 基础过滤 ─────────────────────────────
    if filter_st and 'name' in df.columns:
        df = df[~df['name'].str.contains('ST', na=False, case=True)]
    if filter_bj and 'code' in df.columns:
        df = df[~df['code'].astype(str).str.startswith(('8', '92'))]

    sig_list = []

    for code, grp in df.groupby('code'):
        grp = grp.sort_values('date').reset_index(drop=True)
        if len(grp) < 60:
            continue

        c = grp['close'].values
        o = grp['open'].values
        h = grp['high'].values
        l = grp['low'].values

        n = len(grp)

        # ══════════ Y1: 阳包阴（今天阳线 + 昨天阴线）══════════
        y1 = np.zeros(n, dtype=bool)
        for i in range(1, n):
            y1[i] = c[i] > o[i] and c[i - 1] < o[i - 1]

        # ══════════ BARSLAST(Y1): 距上一次Y1的天数 ══════════
        bars_since = np.full(n, -1)
        last_y1 = -1
        for i in range(n):
            if y1[i]:
                last_y1 = i
            bars_since[i] = i - last_y1 if last_y1 >= 0 else -1

        # ══════════ Y1_LOW / Y1_HIGH: Y1当天的最低价和最高价 ══════════
        y1_low = np.full(n, np.nan)
        y1_high = np.full(n, np.nan)
        for i in range(n):
            if bars_since[i] >= 0:
                yi = i - bars_since[i]
                y1_low[i] = l[yi]
                y1_high[i] = h[yi]

        # ══════════ LOW_PRICE: 从Y1至今的最低价 ══════════
        low_price = np.full(n, np.nan)
        for i in range(n):
            if bars_since[i] >= 0:
                low_price[i] = np.min(l[i - int(bars_since[i]): i + 1])

        # ══════════ HENGPAN: 横盘条件 ══════════
        # Y1后N天内，最低价从未跌破Y1当天最低价
        hengpan = np.zeros(n, dtype=bool)
        for i in range(n):
            if bars_since[i] > 0 and bars_since[i] <= N:
                hengpan[i] = (low_price[i] >= y1_low[i]
                              and not np.isnan(y1_low[i]))

        # ══════════ XG4: 最近15天内有≥1次5连阳 ══════════
        bullish = c > o
        five_consecutive_up = np.zeros(n, dtype=bool)
        for i in range(4, n):
            five_consecutive_up[i] = all(bullish[i - 4: i + 1])

        xg4 = np.zeros(n, dtype=bool)
        for i in range(14, n):
            xg4[i] = any(five_consecutive_up[i - 14: i + 1])

        # ══════════ BREAK1: 突破信号 ══════════
        for i in range(1, n):
            ret = (c[i] / c[i - 1] - 1) * 100
            if (c[i] > o[i]                          # 今天阳线
                and ret >= ZF                         # 涨幅达标
                and not np.isnan(y1_high[i])
                and c[i] > y1_high[i]                 # 突破Y1最高价
                and hengpan[i]                        # 横盘不破
                and xg4[i]):                          # 5连阳背景
                sig_list.append({
                    'code': code,
                    'date': grp['date'].iloc[i],
                    'close': c[i],
                    'ret': ret,
                    'bars_since_y1': bars_since[i],
                })

    if not sig_list:
        return pd.DataFrame()

    result = pd.DataFrame(sig_list)

    # ══════════ 涨停过滤 ══════════
    if skip_limit_up and 'ret' in result.columns:
        # 主板 10%, 双创 20%, 北交所 30%
        limits = {'688': 0.195, '300': 0.195, '301': 0.195, '8': 0.29, '4': 0.29}
        result['limit_pct'] = 0.095
        for prefix, lp in limits.items():
            mask = result['code'].astype(str).str.startswith(prefix)
            result.loc[mask, 'limit_pct'] = lp
        result = result[result['ret'] / 100 < result['limit_pct']]

    # ══════════ FILTER: 5天内去重 ══════════
    result = result.sort_values(['code', 'date'])
    keep_mask = np.ones(len(result), dtype=bool)
    last_sig = {}
    for i, (_, row) in enumerate(result.iterrows()):
        code = row['code']
        d = row['date']
        if code in last_sig:
            days_gap = (d - last_sig[code]).days
            if days_gap <= 5:
                keep_mask[i] = False
                continue
        last_sig[code] = d

    result = result.loc[keep_mask]
    date_col = 'date'
    result[date_col] = pd.to_datetime(result[date_col]).dt.date

    # ══════════ 质量评分（涨幅越大质量越高）══════════
    if 'ret' in result.columns and len(result) > 0:
        result['quality'] = result['ret'] / result['ret'].max()
        result = result.sort_values(['date', 'quality'], ascending=[True, False])

    return result.reset_index(drop=True)


from app.screener.strategies.base import BaseStrategy


class PanzhengTupoStrategy(BaseStrategy):
    """盘整突破策略"""
    name = "盘整突破"
    description = "阴转阳后横盘不破 + 放量突破前高 + 5连阳背景"

    def generate_signals(self, bars):
        kwargs = {}
        for k in ('N', 'ZF', 'filter_st', 'filter_bj', 'skip_limit_up'):
            if k in self.params:
                kwargs[k] = self.params[k]
        return generate_signals(bars, **kwargs)


PARAMS = {
    "description": "阴转阳后横盘不破 + 放量突破前高 + 5连阳背景",
    "N": 5,
    "ZF": 8.0,
    "filter_st": True,
    "filter_bj": True,
    "skip_limit_up": True,
}
