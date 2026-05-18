"""
MA5 角度突破策略 — 严格复刻通达信新公式
PLOYLINE 三角收敛 + ATAN 角度 + 涨停过滤
"""
import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame,
                     filter_st: bool = True,
                     filter_bj: bool = True,
                     skip_limit_up: bool = True,
                     ) -> pd.DataFrame:
    if df is None or len(df) < 60:
        return pd.DataFrame()

    df = df.copy()

    # ── ST / 北交所 过滤 ─────────────────────────────
    if filter_st and 'name' in df.columns:
        df = df[~df['name'].str.contains('ST', na=False, case=True)]
    if filter_bj and 'code' in df.columns:
        df = df[~df['code'].astype(str).str.startswith(('8', '92'))]

    g = df.groupby('code', group_keys=False)

    # ── 基础均线 ──────────────────────────────────────
    df['ma5'] = g['close'].transform(lambda x: x.rolling(5).mean())

    # ═══════════ X1: ATAN 角度 ═══════════
    # ATAN((MA5/REF(MA5,1)-1)*100)*180/3.141159
    df['x1'] = g['ma5'].transform(
        lambda x: np.degrees(np.arctan((x / x.shift(1) - 1) * 100))
    )

    # ═══════════ X2: MA(X1,5) ═══════════
    df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())

    # ═══════════ 金叉/死叉检测 ═══════════
    df['golden_cross'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
    df['death_cross'] = (df['x1'] < df['x2']) & (df['x1'].shift(1) >= df['x2'].shift(1))

    # ═══════════ PLOYLINE: 上一个金叉/死叉的水平线 ═══════════
    # X3 = PLOYLINE(CROSS(X1,X2), X2): 上一次金叉处的 X2 值
    # X4 = PLOYLINE(CROSS(X2,X1), X1): 上一次死叉处的 X1 值
    def _plolyline(cross_series, value_series):
        """模拟 TDX PLOYLINE: 在交叉点取 value，向前填充"""
        result = pd.Series(np.nan, index=cross_series.index)
        last_val = np.nan
        for i in range(len(cross_series)):
            if cross_series.iloc[i]:
                last_val = value_series.iloc[i]
            result.iloc[i] = last_val
        return result

    # 按股票分组计算
    x3_list = []
    x4_list = []
    for code, grp in df.groupby('code'):
        grp = grp.sort_index()
        x3_list.append(_plolyline(grp['golden_cross'], grp['x2']))
        x4_list.append(_plolyline(grp['death_cross'], grp['x1']))
    df['x3'] = pd.concat(x3_list).sort_index()
    df['x4'] = pd.concat(x4_list).sort_index()

    # ═══════════ 三角收敛条件 ═══════════
    # X3 < REF(X3,5): 金叉水平线下降
    # X4 > REF(X4,5): 死叉水平线上升 → 收敛三角形
    df['x3_decline'] = df['x3'] < df.groupby('code')['x3'].shift(5)
    df['x4_rise'] = df['x4'] >= df.groupby('code')['x4'].shift(5)
    df['convergence'] = df['x3_decline'] & df['x4_rise']

    # ═══════════ XG: 收敛中的金叉 ═══════════
    # COUNT(CROSS(X1,X2), convergence_condition): 收敛条件下的金叉
    df['xg'] = df['golden_cross'] & df['convergence']

    # ═══════════ XA: 排除个股 ═══════════
    df['xa'] = df['code'] != '300687'

    # ═══════════ ZT: 综合信号 ═══════════
    df['zt'] = df['xg'] & df['xa']

    # ═══════════ 涨停过滤 ═══════════
    # 只排除真正封板的，不排除大阳线
    if skip_limit_up:
        df['daily_ret'] = df['close'] / df.groupby('code')['close'].shift(1) - 1
        limit_map = {'688': 0.199, '300': 0.199, '301': 0.199, '8': 0.295, '4': 0.295}
        df['limit_pct'] = 0.099
        for pfx, lp in limit_map.items():
            mask = df['code'].astype(str).str.startswith(pfx)
            df.loc[mask, 'limit_pct'] = lp
        df['limit_up'] = df['daily_ret'] >= df['limit_pct']
    else:
        df['limit_up'] = False

    # ═══════════ 综合信号 ZA ═══════════
    df['za'] = df['zt'] & (~df['limit_up'])
    df['za_int'] = df['za'].astype(int)

    # ═══════════ 20天新鲜度 ═══════════
    df['count_20'] = g['za_int'].transform(
        lambda x: x.rolling(20, min_periods=1).sum()
    )
    df['by'] = df['za'] & (df['count_20'] == 1)

    # ═══════════ 避免连续两天 ═══════════
    df['zp'] = df['by'] & (~g['by'].transform(lambda x: x.shift(1)).fillna(False))

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
    """MA5 角度突破 — TDX 新公式复刻"""
    name = "MA5角度_TDXv2"
    description = "ATAN角度 + PLOYLINE三角收敛 + 涨停过滤"

    def generate_signals(self, bars):
        kwargs = {}
        for k in ('filter_st', 'filter_bj', 'skip_limit_up'):
            if k in self.params:
                kwargs[k] = self.params[k]
        return generate_signals(bars, **kwargs)


PARAMS = {
    "description": "ATAN角度 + PLOYLINE三角收敛 + 涨停过滤",
    "filter_st": True,
    "filter_bj": True,
    "skip_limit_up": True,
}
