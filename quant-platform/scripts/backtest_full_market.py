"""
全市场日线回测 — 5,205 只 A 股
MA5 角度突破策略 + VectorBT
"""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# ── 参数 ─────────────────────────────────────────
INIT_CAP  = 5_000_000
POS_SIZE  = 10000         # 1万/笔 → 最多500个同时仓位
FEES      = 0.0003
SLIPPAGE  = 0.001
HARD_STOP = -0.06
TP_STOP   = 0.12
TRAIL_DD  = 0.025
MIN_BARS  = 500

START_DATE = '2022-01-01'
END_DATE   = '2026-05-12'

print("=" * 70)
print("全市场日线回测 — MA5 角度突破")
print("=" * 70)

# ── 1. 加载全市场数据 ─────────────────────────────
print("\n[1/4] 加载全市场日线数据...")

files = sorted(DAILY_DIR.glob("*.parquet"))
all_codes = [f.stem for f in files if len(f.stem) == 6 and f.stem.isdigit() and f.stem[0] in '603']
print(f"  候选股票: {len(all_codes)} 只")

dfs = []
for i, code in enumerate(all_codes):
    try:
        df = pd.read_parquet(str(DAILY_DIR / f'{code}.parquet'),
                             columns=['date','open','high','low','close','volume'])
        if len(df) < MIN_BARS:
            continue
        df['code'] = code
        df['date'] = pd.to_datetime(df['date'])
        df = df[(df['date'] >= START_DATE) & (df['date'] <= END_DATE)]
        dfs.append(df)
    except:
        pass
    if (i + 1) % 1000 == 0:
        print(f"  加载 {i+1}/{len(all_codes)} ...")

bars = pd.concat(dfs, ignore_index=True)
for c in ['open','high','low','close','volume']:
    bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=['close']).sort_values(['code','date']).reset_index(drop=True)
n_stocks = bars['code'].nunique()
print(f"  有效: {n_stocks} 只 | 记录: {len(bars):,} | {bars['date'].min().date()} ~ {bars['date'].max().date()}")

# ── 2. 生成信号 ───────────────────────────────────
print("\n[2/4] 生成 MA5 角度突破信号...")

from app.screener.strategies.ma5_angle import generate_signals

# 需要 buffer 数据以计算 MA60
buffer_start = pd.Timestamp(START_DATE) - pd.Timedelta(days=180)
bars_buf = bars[bars['date'] >= buffer_start].copy()

sig = generate_signals(bars_buf, version="improved",
    filter_st=True, filter_bj=True,
    vol_threshold=1.5, close_position_threshold=0.8,
    disable_quality_sort=False,
    filter_consecutive_up=False, filter_gap_quality=False)

sig['date'] = pd.to_datetime(sig['date'])
sig = sig[sig['date'] >= START_DATE]
print(f"  入场信号: {len(sig):,} 个")
print(f"  信号股票: {sig['code'].nunique():,} 只")

# 年度统计
for y in range(2022, 2027):
    cnt = len(sig[sig['date'].dt.year == y])
    print(f"    {y}: {cnt:,}")

# ── 3. 转宽表 + VectorBT ──────────────────────────
print("\n[3/4] 转宽表 + VectorBT 回测...")

bars_bt = bars.copy()
bars_bt['date'] = pd.to_datetime(bars_bt['date'])

def to_wide(df, field):
    t = df.pivot_table(index='date', columns='code', values=field, aggfunc='first').sort_index()
    t.index = pd.to_datetime(t.index)
    return t

print("  构建宽表...")
close_w = to_wide(bars_bt, 'close')
open_w  = to_wide(bars_bt, 'open')
high_w  = to_wide(bars_bt, 'high')
low_w   = to_wide(bars_bt, 'low')

print(f"  宽表: {close_w.shape[0]} 行 × {close_w.shape[1]:,} 列")
print(f"  内存: {sum(df.memory_usage(deep=True).sum() for df in [close_w, open_w, high_w, low_w]) / 1024**2:.0f} MB")

# 入场信号宽表
print("  填充入场信号...")
entries_w = pd.DataFrame(False, index=close_w.index, columns=close_w.columns)
sig_map = {}
for _, r in sig.iterrows():
    d = pd.Timestamp(r['date'])
    raw = str(r['code']).replace('.SH','').replace('.SZ','').replace('.BJ','')
    sig_map[(d, raw)] = True

for c in close_w.columns:
    for d in entries_w.index:
        raw_c = str(c)
        if (d, raw_c) in sig_map:
            entries_w.loc[d, c] = True

n_entry = entries_w.sum().sum()
print(f"  入场信号(宽表): {n_entry:,}")

# 出场
ma5_w  = close_w.rolling(5).mean()
ma20_w = close_w.rolling(20).mean()
x1_w = (ma5_w - ma5_w.shift(5)) / ma5_w.shift(5) * 100
x2_w = x1_w.rolling(5).mean()
cross_down = (x1_w < x2_w) & (x1_w.shift(1) >= x2_w.shift(1))
exits_w = cross_down | (close_w < ma20_w)
exits_w = exits_w.shift(1).fillna(False).astype(bool)
exits_w = exits_w & ~entries_w

import vectorbt as vbt

print("  执行 VectorBT 回测...")
portfolio = vbt.Portfolio.from_signals(
    close=close_w, entries=entries_w, exits=exits_w, price=open_w,
    init_cash=INIT_CAP, fees=FEES, slippage=SLIPPAGE, freq='D',
    size_type='value', size=POS_SIZE,
    sl_stop=abs(HARD_STOP), tp_stop=TP_STOP, sl_trail=TRAIL_DD,
    high=high_w, low=low_w, direction='longonly', cash_sharing=True,
)

# ── 4. 结果 ────────────────────────────────────────
print("\n[4/4] 回测结果")
print("=" * 70)

stats = portfolio.stats()
trades = portfolio.trades.records_readable
n_trades = len(trades)

eq_end = stats.get('End Value', INIT_CAP)
total_ret = (eq_end / INIT_CAP - 1) * 100
dd = stats.get('Max Drawdown [%]', 0)
sharpe = stats.get('Sharpe Ratio', 0)

if n_trades > 0:
    wins = (trades['PnL'] > 0).sum()
    wr = wins / n_trades * 100
    avg_win  = trades[trades['PnL'] > 0]['Return'].mean() * 100 if wins > 0 else 0
    avg_loss = trades[trades['PnL'] < 0]['Return'].mean() * 100 if n_trades > wins else 0
    total_wins = trades[trades['PnL'] > 0]['PnL'].sum()
    total_losses = abs(trades[trades['PnL'] < 0]['PnL'].sum())
    pf = total_wins / total_losses if total_losses > 0 else 0
else:
    wins = 0; wr = 0; avg_win = 0; avg_loss = 0; pf = 0

trading_days = len(close_w)
ann_ret = ((1 + total_ret/100) ** (365 / max(trading_days, 1)) - 1) * 100

print(f"  股票池:        {n_stocks:,} 只")
print(f"  回测区间:      {close_w.index[0].date()} ~ {close_w.index[-1].date()}")
print(f"  交易日:        {trading_days} 天")
print(f"  入场信号:      {n_entry:,} 个")
print(f"  完成交易:      {n_trades:,} 笔")
print(f"  初始资金:      {INIT_CAP:,.0f}")
print(f"  最终资金:      {eq_end:,.0f}")
print("-" * 70)
print(f"  总收益率:      {total_ret:+.2f}%")
print(f"  年化收益:      {ann_ret:+.1f}%")
print(f"  最大回撤:      {dd:.2f}%")
print(f"  夏普比率:      {sharpe:.2f}")
print(f"  胜率:          {wr:.1f}% ({wins}/{n_trades})")
print(f"  平均盈利:      {avg_win:+.2f}%")
print(f"  平均亏损:      {avg_loss:+.2f}%")
print(f"  盈亏比:        {pf:.2f}")

# 年度收益
print(f"\n  年度收益率:")
for y in range(2022, 2027):
    y_start = f'{y}-01-01'
    y_end = f'{y}-12-31'
    if y_start in close_w.index or y_end in close_w.index:
        eq_col = 'equity'
        if hasattr(portfolio, 'value'):
            val = portfolio.value()
            yr_start = val.loc[y_start].iloc[0] if y_start in val.index else None
            yr_end = val.loc[y_end].iloc[0] if y_end in val.index else None
            if yr_start and yr_end:
                yr_ret = (yr_end / yr_start - 1) * 100
                print(f"    {y}: {yr_ret:+.2f}%")

print("=" * 70)
print(f"脚本: scripts/backtest_full_market.py")
print("=" * 70)
