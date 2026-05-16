"""
【MA5 角度突破策略】
计算 MA5 的 ATAN 角度，角度上穿其均线时买入。
成交量确认 + 收盘位置确认 + 可选过滤器。
"""
import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame, version: str = "improved",
                     filter_st: bool = True,
                     filter_bj: bool = True,
                     vol_threshold: float = 1.5,
                     close_position_threshold: float = 0.8,
                     disable_quality_sort: bool = False,
                     filter_consecutive_up: bool = False,
                     filter_gap_quality: bool = False,
                     max_price: float = 0,
                     skip_limit_up: bool = False,
                     ) -> pd.DataFrame:
    """
    参数:
      df: 全市场 K 线，含 code/date/open/high/low/close/volume/name
      filter_st: 过滤 ST 股票
      filter_bj: 过滤北交所（8 开头）
      vol_threshold: 量比阈值
      close_position_threshold: 收盘在日K线位置阈值
      disable_quality_sort: 禁用质量排序（用于对比测试）
      filter_consecutive_up: 连续阳线衰竭过滤（信号前已连涨≧5天则跳过）
      filter_gap_quality: 跳空缺口质量过滤（跳空>5%衰竭缺口则跳过）
    """
    if df is None or len(df) < 60:
        return pd.DataFrame()
    df = df.copy()

    # ── ST / 北交所 过滤 ─────────────────────────────
    if filter_st and 'name' in df.columns:
        df = df[~df['name'].str.contains('ST', na=False, case=True)]
    if filter_bj and 'code' in df.columns:
        df = df[~df['code'].astype(str).str.startswith('8')]

    g = df.groupby('code', group_keys=False)

    # ── 基础指标 ──────────────────────────────────────
    df['ma5']   = g['close'].transform(lambda x: x.rolling(5).mean())
    df['ma10']  = g['close'].transform(lambda x: x.rolling(10).mean())
    df['ma20']  = g['close'].transform(lambda x: x.rolling(20).mean())
    df['ma60']  = g['close'].transform(lambda x: x.rolling(60).mean())

    if version == "original":
        # ── ATAN 角度 ──────────────────────────────────
        df['x1'] = g['ma5'].transform(
            lambda x: np.degrees(np.arctan((x / x.shift(1) - 1) * 100))
        )
        df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())

        df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
        df['cond_angle'] = (
            df['cross_up']
            & (df['x2'] < df['x2'].shift(5))
            & (df['x1'] > df['x1'].shift(5))
        )

        price_limit = 26 if max_price == 0 else (99999 if max_price < 0 else max_price)
        df['cond_price'] = (
            (df['close'] < price_limit)
            & (df['close'] / df['close'].shift(1) > 1.02)
            & (df['close'] > df['ma20'])
        )

    else:
        # ── 改进版：5 日实际斜率 ──────────────────────
        df['x1'] = g['ma5'].transform(
            lambda x: (x - x.shift(5)) / x.shift(5) * 100
        )
        df['x2'] = g['x1'].transform(lambda x: x.rolling(5).mean())

        df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
        df['cond_angle'] = df['cross_up'] & (df['x1'] > df['x1'].shift(1))

        # 成交量确认
        df['avg_vol_20'] = g['volume'].transform(lambda x: x.shift(1).rolling(20).mean())
        df['cond_vol'] = df['volume'] > df['avg_vol_20'] * vol_threshold

        # 收盘位置确认
        df['close_pos'] = (df['close'] - df['low']) / (df['high'] - df['low'])
        df['cond_close_strong'] = df['close_pos'] > close_position_threshold

        # 价格在20日区间上半部（结构性确认，替代日涨幅>2%）
        df['range_high_20'] = g['high'].transform(lambda x: x.shift(1).rolling(20).max())
        df['range_low_20']  = g['low'].transform(lambda x: x.shift(1).rolling(20).min())
        df['range_mid_20']  = (df['range_high_20'] + df['range_low_20']) / 2

        # 价格条件（精简：删除冗余的日涨幅>2%，放宽MA60条件）
        df['cond_price'] = (
            (df['close'] > df['ma20'])
            & (df['close'] > df['range_mid_20'])
            & (df['ma60'] >= df['ma60'].shift(10))
        )

    # ── 综合信号 ──────────────────────────────────────
    df['za'] = df['cond_angle'] & df['cond_price']
    if version == "improved":
        df['za'] = df['za'] & df['cond_vol']
        df['za'] = df['za'] & df['cond_close_strong']

    # 连续阳线衰竭：信号日前4天全部收阳 → 动能透支
    if filter_consecutive_up:
        df['up_day'] = df['close'] > df['close'].shift(1)
        df['up_streak_4'] = g['up_day'].transform(
            lambda x: x.shift(1).rolling(4, min_periods=1).sum()
        )
        df['za'] = df['za'] & (df['up_streak_4'] < 4)

    # 跳空缺口质量：衰竭缺口（跳空>5%）过滤
    if filter_gap_quality:
        df['gap_pct'] = (df['open'] / df['close'].shift(1) - 1) * 100
        df['za'] = df['za'] & (df['gap_pct'] < 5.0)  # >5%跳空视为衰竭

    # 20 天信号新鲜度
    df['za_int'] = df['za'].astype(int)
    df['count_20'] = g['za_int'].transform(
        lambda x: x.rolling(20, min_periods=1).sum()
    )
    df['buy'] = df['za'] & (df['count_20'] == 1)

    # 涨停过滤：跳过当日涨停的股票
    if skip_limit_up:
        df['daily_ret'] = df['close'] / df['close'].shift(1) - 1
        prefix = df['code'].astype(str).str[:3]
        # 科创板/创业板 20%, 北交所 30%, 主板 10%
        limit_map = {'688': 0.195, '300': 0.195, '301': 0.195,
                     '8': 0.29, '4': 0.29}
        df['limit_pct'] = 0.095  # 主板默认
        for pfx, lp in limit_map.items():
            mask = df['code'].astype(str).str.startswith(pfx)
            df.loc[mask, 'limit_pct'] = lp
        df['limit_up'] = df['daily_ret'] >= df['limit_pct']
        df['buy_signal'] = df['buy'] & (~df['limit_up']) & (~g['buy'].transform(lambda x: x.shift(1)).fillna(False))
    else:
        df['buy_signal'] = df['buy'] & (~g['buy'].transform(lambda x: x.shift(1)).fillna(False))

    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.date

    result = df[df['buy_signal'] == True].copy()
    result['version'] = version

    # ── 信号质量评分 ──────────────────────────────────
    if not result.empty and 'x1' in result.columns and not disable_quality_sort:
        angle_norm = (result['x1'] - result['x1'].min()) / (result['x1'].max() - result['x1'].min() + 0.001)
        if 'avg_vol_20' in result.columns:
            vol_ratio = result['volume'] / result['avg_vol_20'].replace(0, 1)
            vol_norm = (vol_ratio - vol_ratio.min()) / (vol_ratio.max() - vol_ratio.min() + 0.001)
        else:
            vol_norm = 0.5
        if 'close_pos' in result.columns:
            pos_norm = result['close_pos'].clip(0, 1)
        else:
            pos_norm = 0.5
        result['quality'] = angle_norm * 0.5 + vol_norm * 0.3 + pos_norm * 0.2
        result = result.sort_values(['date', 'quality'], ascending=[True, False])

    return result


def generate_signals_original(df, **kwargs):
    return generate_signals(df, version="original")


def generate_signals_improved(df, **kwargs):
    return generate_signals(df, version="improved")


from app.screener.strategies.base import BaseStrategy


class MA5AngleImprovedStrategy(BaseStrategy):
    """MA5 角度突破策略（改进版）"""
    name = "MA5角度_改进版"
    description = "MA5斜率 + 放量确认 + 收盘位确认 + 涨停过滤"

    def generate_signals(self, bars):
        filter_keys = ('filter_st', 'filter_bj', 'version',
                       'vol_threshold', 'close_position_threshold',
                       'disable_quality_sort', 'filter_consecutive_up',
                       'filter_gap_quality', 'max_price', 'skip_limit_up')
        kwargs = {k: v for k, v in self.params.items() if k in filter_keys}
        if 'version' not in kwargs:
            kwargs['version'] = 'improved'
        return generate_signals(bars, **kwargs)


PARAMS = {
    "description": "MA5斜率 + 放量确认 + 收盘位确认 + 涨停过滤",
    "version": "improved",
    "skip_limit_up": True,
    "vol_threshold": 1.5,
    "close_position_threshold": 0.8,
}
