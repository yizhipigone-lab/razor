"""
MA5金叉策略 — Python 复刻通达信 QUANTQQ 公式
双均线斜率连续向上 + MA20金叉MA20A + 阳线放量
"""
import pandas as pd
import numpy as np


def generate_signals(df: pd.DataFrame,
                     ) -> pd.DataFrame:
    if df is None or len(df) < 80:
        return pd.DataFrame()
    df = df.copy()

    # 候选⑤:ST/退市/北交所过滤已移到 base.preprocess
    g = df.groupby('code', group_keys=False)

    # ── 均线 ──────────────────────────────────────
    df['ma5']   = g['close'].transform(lambda x: x.rolling(5).mean())
    df['ma10']  = g['close'].transform(lambda x: x.rolling(10).mean())
    df['ma20']  = g['close'].transform(lambda x: x.rolling(20).mean())
    df['ma60']  = g['close'].transform(lambda x: x.rolling(60).mean())
    df['ma20a'] = g['ma20'].transform(lambda x: x.rolling(10).mean())

    # ── 斜率 D5/D10 ───────────────────────────────
    df['d5']  = df['ma5']  - g['ma5'].shift(1)
    df['d10'] = df['ma10'] - g['ma10'].shift(1)

    # ── TODAY: 两根均线同时向上，MA5斜率>MA10 ──────
    df['today'] = (df['d5'] > 0) & (df['d10'] > 0) & (df['d5'] > df['d10'])
    # ── YESTERDAY: 昨日也满足 ──────────────────────
    s_d5  = g['d5'].shift(1)
    s_d10 = g['d10'].shift(1)
    df['yesterday'] = (s_d5 > 0) & (s_d10 > 0) & (s_d5 > s_d10)

    # ── XG: 连续两天满足 + UPNDAY(C,2) ─────────────
    s_close_1 = g['close'].shift(1)
    s_close_2 = g['close'].shift(2)
    df['xg'] = (df['today'] & df['yesterday']
                & (df['close'] > s_close_1) & (s_close_1 > s_close_2))

    # ── AA: CROSS(MA20, MA20A) AND MA10>MA20 ─────
    s_ma20  = g['ma20'].shift(1)
    s_ma20a = g['ma20a'].shift(1)
    df['aa'] = ((df['ma20'] > df['ma20a'])
                & (s_ma20 <= s_ma20a)
                & (df['ma10'] > df['ma20']))

    # ── ZP 综合信号 ────────────────────────────────
    s_close = g['close'].shift(1)
    s_aa    = g['aa'].shift(1)
    s_ma20a2 = g['ma20a'].shift(1)

    df['buy_signal'] = (
        df['xg']
        & (s_aa == True)                        # REF(AA,1)=1
        & (df['ma20a'] > s_ma20a2)              # MA20A继续上升
        & (df['close'] > df['open'])             # C>O 阳线
        & (df['close'] / s_close > 1.01)         # 涨幅>1%
        & (df['high'] / s_close < 1.05)          # 最高<+5%
    )

    # ── 输出 ──────────────────────────────────────
    signals = df[df['buy_signal']].copy()
    if signals.empty:
        return pd.DataFrame(columns=['code', 'date', 'close', 'buy_signal'])
    signals = signals[['code', 'date', 'close', 'buy_signal']].copy()
    signals['date'] = pd.to_datetime(signals['date']).dt.date
    return signals


# 候选⑤:保留原行为 — 无涨停过滤 + 北交所只 '8' 起头(不是 base 默认的 '^[84]\\d{5}')
PARAMS = {
    "skip_limit_up": False,
    "filter_bj_pattern": r"^8",
}
