"""
MA5 角度突破策略 — 严格匹配通达信公式
ATAN 角度 + PLOYLINE 三角收敛 + 涨停过滤
"""
import pandas as pd
import numpy as np
from datetime import timedelta

# TDX 公式常量
# 注: TDX_PI=3.141593 与通达信内部精度一致(差 math.pi 约 3e-7，对角度信号无影响)，保持不变以匹配 TDX 输出
TDX_PI = 3.141593

def generate_signals(df: pd.DataFrame,
                     max_price: float = 0,
                     ) -> pd.DataFrame:
    if df is None or len(df) < 60:
        return pd.DataFrame()

    df = df.copy()

    # 候选⑤:ST/北交所/涨停 过滤已移到 base.preprocess
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

    # ═══════════ PLOYLINE (向量化: where+ffill，替代逐行循环) ═══════════
    # PLOYLINE(COND, VALUE): COND为True时取VALUE，否则延续上一次的VALUE
    # 按 code 分组 ffill，避免跨股票串值
    df['_x3_raw'] = df['x2'].where(df['golden_cross'])
    df['_x4_raw'] = df['x1'].where(df['death_cross'])
    df['x3'] = df.groupby('code', group_keys=False)['_x3_raw'].ffill()
    df['x4'] = df.groupby('code', group_keys=False)['_x4_raw'].ffill()

    # ═══════════ 三角收敛: X3 < REF(X3,5) AND X4 > REF(X4,5) ═══════════
    df['x3_decline'] = df['x3'] < df.groupby('code')['x3'].shift(5)
    df['x4_rise'] = df['x4'] >= df.groupby('code')['x4'].shift(5)
    df['convergence'] = df['x3_decline'] & df['x4_rise']

    # ═══════════ XG = CROSS AND convergence（同日） ═══════════
    # COUNT(CROSS, convergence): convergence=1时计数当前K线的CROSS
    df['xg'] = df['golden_cross'] & df['convergence']

    # ═══════════ ZT = XG（XA 已预过滤） ═══════════
    df['zt'] = df['xg']

    # 候选⑤:涨停过滤已移到 base.preprocess
    # ═══════════ ZP = ZT ═══════════
    df['zp_raw'] = df['zt']

    # ═══════════ 价格过滤 ═══════════
    if max_price and max_price > 0:
        df['zp_priced'] = df['zp_raw'] & (df['close'] <= max_price)
    else:
        df['zp_priced'] = df['zp_raw']

    # ═══════════ 20天新鲜度（向量化: rolling(20) 交易日窗口，替代自然日循环） ═══════════
    # M-02 修复: 用 rolling(20) 统计最近20根K线（交易日），与TDX COUNT(X,20)一致
    df['_zp_int'] = df['zp_priced'].astype(int)
    df = df.sort_values(['code', 'date'] if 'date' in df.columns else ['code', 'datetime'])
    df['count_20'] = df.groupby('code', group_keys=False)['_zp_int'].transform(
        lambda x: x.rolling(20, min_periods=1).sum()
    )
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
    # 候选⑤:保留旧 0.195 阈值 + 8/4 用 0.29(原 inline 表)
    LIMIT_TABLE = {"688": 0.195, "300": 0.195, "301": 0.195, "8": 0.29, "4": 0.29}
    LIMIT_MAIN_PCT = 0.095

    def default_params(self) -> dict:
        return dict(PARAMS)

    def generate_signals(self, bars):
        kwargs = {}
        for k in ('max_price',):
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
