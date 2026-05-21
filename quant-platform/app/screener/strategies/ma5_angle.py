"""
MA5 角度突破策略（原版）
ATAN 角度金叉 + 收敛确认 + 价格过滤
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
        df = df[~df['code'].astype(str).str.startswith('8')]

    g = df.groupby('code', group_keys=False)

    # ── 基础均线 ──────────────────────────────────────
    df['ma5']  = g['close'].transform(lambda x: x.rolling(5).mean())
    df['ma20'] = g['close'].transform(lambda x: x.rolling(20).mean())

    # ═══════════ ATAN 角度 ═══════════
    df['x1'] = g['ma5'].transform(
        lambda x: np.degrees(np.arctan((x / x.shift(1) - 1) * 100))
    )
    df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())

    # ═══════════ 金叉 + 三角收敛 ═══════════
    px1, px2 = g['x1'].shift(1), g['x2'].shift(1)
    df['cond_angle'] = (
        (df['x1'] > df['x2']) & (px1.notna()) & (px1 <= px2)  # CROSS(X1, X2)
        & (df['x2'] < df['x2'].shift(5))                       # X2 下降
        & (df['x1'] > df['x1'].shift(5))                       # X1 上升
    )

    # ═══════════ 价格条件 ═══════════
    df['cond_price'] = (
        (df['close'] < 26)                                     # 低价股
        & (df['close'] / df['close'].shift(1) > 1.02)          # 日涨幅 > 2%
        & (df['close'] > df['ma20'])                            # 站上 MA20
    )

    # ═══════════ 综合信号 ═══════════
    df['za'] = df['cond_angle'] & df['cond_price']

    # 20 天信号新鲜度
    df['za_int'] = df['za'].astype(int)
    df['count_20'] = g['za_int'].transform(
        lambda x: x.rolling(20, min_periods=1).sum()
    )
    df['buy'] = df['za'] & (df['count_20'] == 1)

    # ═══════════ 涨停过滤 ═══════════
    if skip_limit_up:
        df['daily_ret'] = df['close'] / df['close'].shift(1) - 1
        limit_map = {'688': 0.195, '300': 0.195, '301': 0.195,
                     '8': 0.29, '4': 0.29}
        df['limit_pct'] = 0.095
        for pfx, lp in limit_map.items():
            df.loc[df['code'].astype(str).str.startswith(pfx), 'limit_pct'] = lp
        df['limit_up'] = df['daily_ret'] >= df['limit_pct']
    else:
        df['limit_up'] = False

    df['buy_signal'] = df['buy'] & (~df['limit_up'])

    # ═══════════ 输出 ═══════════
    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.date

    result = df[df['buy_signal'] == True].copy()

    if not result.empty and 'x1' in result.columns:
        result['quality'] = (result['x1'] - result['x1'].min()) / (result['x1'].max() - result['x1'].min() + 0.001)
        result = result.sort_values(['date', 'quality'], ascending=[True, False])

    return result


from app.screener.strategies.base import BaseStrategy


class MA5AngleStrategy(BaseStrategy):
    """MA5 角度突破策略"""
    name = "MA5角度_原版"
    description = "ATAN角度金叉 + 三角收敛 + 价格<26 + 日涨幅>2% + 站上MA20"

    def generate_signals(self, bars):
        kwargs = {}
        for k in ('filter_st', 'filter_bj', 'skip_limit_up'):
            if k in self.params:
                kwargs[k] = self.params[k]
        return generate_signals(bars, **kwargs)


PARAMS = {
    "description": "ATAN角度金叉 + 三角收敛 + 价格<26 + 日涨幅>2% + 站上MA20",
    "filter_st": True,
    "filter_bj": True,
    "skip_limit_up": True,
}
