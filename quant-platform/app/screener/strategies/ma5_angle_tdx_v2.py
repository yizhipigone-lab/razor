"""
MA5 角度突破策略 — 严格匹配通达信公式
ATAN 角度 + PLOYLINE 三角收敛 + 涨停过滤
"""
import pandas as pd
import numpy as np
from datetime import timedelta

# TDX 公式常量
TDX_PI = 3.141593
LIMIT_PCT_MAP = {
    '688': 0.195, '300': 0.195, '301': 0.195,
    '8': 0.29, '4': 0.29,
}


def generate_signals(df: pd.DataFrame,
                     filter_st: bool = True,
                     filter_bj: bool = True,
                     skip_limit_up: bool = True,
                     max_price: float = 0,
                     ) -> pd.DataFrame:
    if df is None or len(df) < 60:
        return pd.DataFrame()

    df = df.copy()

    # ── ST 过滤 ─────────────────────────────
    if filter_st and 'name' in df.columns:
        df = df[~df['name'].str.contains('ST', na=False, case=True)]

    # ── XA: 排除个股（严格匹配 TDX） ──────────
    if filter_bj:
        df = df[df['code'] != '300687']
        df = df[df['code'] != '920001']
        df = df[~df['code'].astype(str).str.startswith('430')]
        df = df[~df['code'].astype(str).str.startswith('873')]

    g = df.groupby('code', group_keys=False)

    # ═══════════ MA5 ═══════════
    df['ma5'] = g['close'].transform(lambda x: x.rolling(5).mean())

    # ═══════════ X1 = ATAN((MA5/REF(MA5,1)-1)*100)*180/3.141159 ═══════════
    df['x1'] = g['ma5'].transform(
        lambda x: np.arctan((x / x.shift(1) - 1) * 100) * 180 / TDX_PI
    )

    # ═══════════ X2 = MA(X1,5) ═══════════
    df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())

    # ═══════════ 金叉 / 死叉 = CROSS ═══════════
    df['x1_prev'] = g['x1'].shift(1)
    df['x2_prev'] = g['x2'].shift(1)
    df['golden_cross'] = (df['x1'] > df['x2']) & (df['x1_prev'].notna()) & (df['x1_prev'] <= df['x2_prev'])
    df['death_cross'] = (df['x1'] < df['x2']) & (df['x1_prev'].notna()) & (df['x1_prev'] >= df['x2_prev'])

    # ═══════════ PLOYLINE ═══════════
    def _plolyline(cross_series, value_series):
        result = pd.Series(np.nan, index=cross_series.index)
        last_val = np.nan
        for i in range(len(cross_series)):
            if cross_series.iloc[i]:
                last_val = value_series.iloc[i]
            result.iloc[i] = last_val
        return result

    x3_list, x4_list = [], []
    for code, grp in df.groupby('code'):
        grp = grp.sort_index()
        x3_list.append(_plolyline(grp['golden_cross'], grp['x2']))
        x4_list.append(_plolyline(grp['death_cross'], grp['x1']))
    df['x3'] = pd.concat(x3_list).sort_index()
    df['x4'] = pd.concat(x4_list).sort_index()

    # ═══════════ 三角收敛: X3 < REF(X3,5) AND X4 > REF(X4,5) ═══════════
    df['x3_decline'] = df['x3'] < df.groupby('code')['x3'].shift(5)
    df['x4_rise'] = df['x4'] >= df.groupby('code')['x4'].shift(5)
    df['convergence'] = df['x3_decline'] & df['x4_rise']

    # ═══════════ XG = CROSS AND convergence（同日） ═══════════
    # COUNT(CROSS, convergence): convergence=1时计数当前K线的CROSS
    df['xg'] = df['golden_cross'] & df['convergence']

    # ═══════════ ZT = XG（XA 已预过滤） ═══════════
    df['zt'] = df['xg']

    # ═══════════ 涨停过滤 ═══════════
    if skip_limit_up:
        df['daily_ret'] = df['close'] / df.groupby('code')['close'].shift(1) - 1
        df['limit_pct'] = 0.095
        for pfx, lp in LIMIT_PCT_MAP.items():
            mask = df['code'].astype(str).str.startswith(pfx)
            df.loc[mask, 'limit_pct'] = lp
        df['limit_up'] = df['daily_ret'] >= df['limit_pct']
    else:
        df['limit_up'] = False

    # ═══════════ ZP = ZT AND NOT_LIMIT ═══════════
    df['zp_raw'] = df['zt'] & (~df['limit_up'])

    # ═══════════ 价格过滤 ═══════════
    if max_price and max_price > 0:
        df['zp_priced'] = df['zp_raw'] & (df['close'] <= max_price)
    else:
        df['zp_priced'] = df['zp_raw']

    # ═══════════ 20天新鲜度（在价格过滤后的最终信号上统计） ═══════════
    df['_dt'] = pd.to_datetime(df['date'] if 'date' in df.columns else df['datetime'])
    df['_dt_date'] = df['_dt'].dt.date
    df['_zp_int'] = df['zp_priced'].astype(int)
    def _count_in_20d(grp):
        dates = grp['_dt_date'].values
        signals = grp['_zp_int'].values
        result = [0] * len(grp)
        for i in range(len(grp)):
            cutoff = dates[i] - timedelta(days=20)
            cnt = 0
            for j in range(max(0,i-40), i+1):
                if dates[j] >= cutoff and signals[j]:
                    cnt += 1
            result[i] = cnt
        return pd.Series(result, index=grp.index)
    c20_list = []
    for code, grp in df.groupby('code'):
        grp = grp.sort_values('_dt')
        c20_list.append(_count_in_20d(grp))
    df['count_20'] = pd.concat(c20_list).sort_index()
    df['zp'] = df['zp_priced'] & (df['count_20'] == 1)

    # ═══════════ 输出 ═══════════
    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.date

    result = df[df['zp'] == True].copy()

    if not result.empty and 'x1' in result.columns:
        angle_norm = (result['x1'] - result['x1'].min()) / (result['x1'].max() - result['x1'].min() + 0.001)
        result['quality'] = angle_norm
        result = result.sort_values(['date', 'quality'], ascending=[True, False])

    return result


from app.screener.strategies.base import BaseStrategy


class MA5AngleTDXv2Strategy(BaseStrategy):
    """MA5 角度突破 — 通达信公式"""
    name = "MA5角度_TDXv2"
    description = "ATAN角度 + PLOYLINE三角收敛 + 涨停过滤"

    def generate_signals(self, bars):
        kwargs = {}
        for k in ('filter_st', 'filter_bj', 'skip_limit_up', 'max_price'):
            if k in self.params:
                kwargs[k] = self.params[k]
        return generate_signals(bars, **kwargs)


PARAMS = {
    "description": "ATAN角度 + PLOYLINE三角收敛 + 涨停过滤（通达信公式）",
    "filter_st": True,
    "filter_bj": True,
    "skip_limit_up": True,
    "max_price": 0,  # 0=不限制，设具体值如26则过滤高价股
}
