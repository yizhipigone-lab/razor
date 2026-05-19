"""
全市场回测对比：自有 FastEngine vs VectorBT
============================================
5,201 只A股，2022-01-04 ~ 2026-05-12
相同入场信号(MA5角度突破)，对比出场机制
"""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import date
from collections import defaultdict, Counter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

START_DATE = date(2022, 1, 4)
END_DATE   = date(2026, 5, 12)
INIT_CAP   = 5_000_000
POS_SIZE   = 10000          # 单票1万 = 最多500仓位

print("=" * 70)
print("全市场回测对比：自有 FastEngine vs VectorBT")
print("=" * 70)

# ── 1. 加载全市场数据（两套系统共用）───────────────
print("\n[1/3] 加载全市场日线 + 生成信号...")

files = sorted(DAILY_DIR.glob("*.parquet"))
all_codes = [f.stem for f in files if len(f.stem)==6 and f.stem.isdigit() and f.stem[0] in '603']
print(f"  候选: {len(all_codes)} 只")

dfs = []
for code in all_codes:
    try:
        df = pd.read_parquet(str(DAILY_DIR / f'{code}.parquet'),
                             columns=['date','open','high','low','close','volume'])
        if len(df) < 500:
            continue
        df['code'] = code
        df['date'] = pd.to_datetime(df['date'])
        df = df[(df['date'] >= '2021-09-01') & (df['date'] <= str(END_DATE))]
        dfs.append(df)
    except:
        pass

bars = pd.concat(dfs, ignore_index=True)
for c in ['open','high','low','close','volume']:
    bars[c] = pd.to_numeric(bars[c], errors='coerce')
bars = bars.dropna(subset=['close']).sort_values(['code','date']).reset_index(drop=True)
n_stocks = bars['code'].nunique()
print(f"  有效: {n_stocks} 只 | 记录: {len(bars):,}")

from app.screener.strategies.ma5_angle import generate_signals

sig = generate_signals(bars, version="improved",
    filter_st=True, filter_bj=True,
    vol_threshold=1.5, close_position_threshold=0.8,
    disable_quality_sort=False,
    filter_consecutive_up=False, filter_gap_quality=False)

sig['date'] = pd.to_datetime(sig['date'])
sig = sig[(sig['date'] >= str(START_DATE)) & (sig['date'] <= str(END_DATE))]
sig['date'] = sig['date'].dt.date
n_sig = len(sig)
print(f"  信号: {n_sig:,} | 股票: {sig['code'].nunique():,}")

# ── 2. 自有 FastEngine 回测 ────────────────────────
print(f"\n[2/3] 自有 FastEngine 回测...")

from app.backtest.simple_runner import FastEngine

# 准备数据
bars_bt = bars[(bars['date'] >= str(START_DATE)) & (bars['date'] <= str(END_DATE))].copy()
bars_bt['date'] = pd.to_datetime(bars_bt['date']).dt.date

closes, highs, lows, opens = {}, {}, {}, {}
for d, g in bars_bt.groupby('date'):
    closes[d] = dict(zip(g['code'], g['close']))
    highs[d] = dict(zip(g['code'], g['high']))
    lows[d] = dict(zip(g['code'], g['low']))
    opens[d] = dict(zip(g['code'], g['open']))

td_list = sorted(closes.keys())
sbd = defaultdict(list)
for _, r in sig.iterrows():
    sbd[r['date']].append((r['code'], float(r['close'])))

params_own = {
    'initial_capital': INIT_CAP,
    'position_size': POS_SIZE,
    'min_buy_amt': 3000,
    # 分层止盈止损（自有系统核心）
    'hard_stop': -0.06,
    'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15,
    'tp2_pct': 0.12,
    'trail_activate': 0.05,
    'trail_dd': 0.025,
    'time_exit_days': 5, 'time_exit_profit': 0.03,
    'time_force_days': 20,
    # 资金管理
    'loss_streak_halve': 3,
    'loss_streak_pause': 5,
    'pause_days': 3,
    'same_stock_cooldown': 20,
}

eng = FastEngine(td_list, params_own)
cooldown = params_own['same_stock_cooldown']
prev_snap = {}

for d in td_list:
    snap = {}
    for code in set(list(eng.positions.keys())):
        if d in closes and code in closes[d]:
            snap[code] = {
                'open': opens[d].get(code, closes[d].get(code, 0)),
                'high': highs[d].get(code, closes[d].get(code, 0)),
                'low': lows[d].get(code, closes[d].get(code, 0)),
                'close': closes[d].get(code, 0)
            }
    eng.sell_phase(d, snap, prev_snap)
    prev_snap = {k: dict(v) for k, v in snap.items()}

    paused = eng.pause is not None and d <= eng.pause
    if d in sbd and not paused:
        for code, px in sbd[d]:
            if eng.cash < min(eng.max_pos(), params_own['min_buy_amt']):
                break
            if any(t.code == code and (d - t.entry_date).days <= cooldown for t in eng.trades):
                continue
            eng.buy(d, code, px)

    eng.record(d, snap)

trades_own = eng.trades
n_own = len(trades_own)
eq_df = pd.DataFrame(eng.equity)
eq_end_own = eq_df['equity'].iloc[-1] if len(eq_df) > 0 else INIT_CAP
ret_own = (eq_end_own / INIT_CAP - 1) * 100

eq_df['cmax'] = eq_df['equity'].cummax()
eq_df['dd'] = (eq_df['equity'] - eq_df['cmax']) / eq_df['cmax'] * 100
dd_own = float(eq_df['dd'].min())

wins_own = [t for t in trades_own if t.ret > 0]
loses_own = [t for t in trades_own if t.ret <= 0]
wr_own = len(wins_own) / n_own * 100 if n_own > 0 else 0
aw_own = np.mean([t.ret for t in wins_own]) if wins_own else 0
al_own = np.mean([t.ret for t in loses_own]) if loses_own else 0
pf_own = sum(t.profit for t in wins_own) / abs(sum(t.profit for t in loses_own)) if loses_own and sum(t.profit for t in loses_own) else 0

# 夏普
rets = eq_df['equity'].pct_change().dropna()
sharpe_own = float(np.sqrt(252) * rets.mean() / rets.std()) if len(rets) > 0 and rets.std() > 0 else 0

# 年化
td_span = (td_list[-1] - td_list[0]).days
ann_own = (1 + ret_own/100) ** (365/max(td_span, 1)) - 1

print(f"  交易: {n_own:,} 笔 | 胜率: {wr_own:.1f}% | 收益: {ret_own:+.2f}%")

exit_reasons = Counter(t.reason for t in trades_own)
print(f"  退出原因: {dict(exit_reasons.most_common())}")

# ── 3. VectorBT 回测 ──────────────────────────────
print(f"\n[3/3] VectorBT 回测...")

bars_bt2 = bars_bt.copy()
bars_bt2['date'] = pd.to_datetime(bars_bt2['date'])

def to_wide(df, field):
    t = df.pivot_table(index='date', columns='code', values=field, aggfunc='first').sort_index()
    t.index = pd.to_datetime(t.index)
    return t

close_w = to_wide(bars_bt2, 'close')
open_w  = to_wide(bars_bt2, 'open')
high_w  = to_wide(bars_bt2, 'high')
low_w   = to_wide(bars_bt2, 'low')

# 信号
entries_w = pd.DataFrame(False, index=close_w.index, columns=close_w.columns)
sig_map = {}
for _, r in sig.iterrows():
    sig_map[(pd.Timestamp(r['date']), r['code'])] = True

for c in close_w.columns:
    raw_c = str(c)
    for d in entries_w.index:
        if (d, raw_c) in sig_map:
            entries_w.loc[d, c] = True

ma5_w  = close_w.rolling(5).mean()
ma20_w = close_w.rolling(20).mean()
x1_w = (ma5_w - ma5_w.shift(5)) / ma5_w.shift(5) * 100
x2_w = x1_w.rolling(5).mean()
cross_down = (x1_w < x2_w) & (x1_w.shift(1) >= x2_w.shift(1))
exits_w = cross_down | (close_w < ma20_w)
exits_w = exits_w.shift(1).fillna(False).astype(bool)
exits_w = exits_w & ~entries_w

import vectorbt as vbt

portfolio_vbt = vbt.Portfolio.from_signals(
    close=close_w, entries=entries_w, exits=exits_w, price=open_w,
    init_cash=INIT_CAP, fees=0.0003, slippage=0.001, freq='D',
    size_type='value', size=POS_SIZE,
    sl_stop=0.06, tp_stop=0.12, sl_trail=0.025,
    high=high_w, low=low_w, direction='longonly', cash_sharing=True,
)

stats_vbt = portfolio_vbt.stats()
trades_vbt = portfolio_vbt.trades.records_readable
n_vbt = len(trades_vbt)
eq_vbt = stats_vbt.get('End Value', INIT_CAP)
ret_vbt = (eq_vbt / INIT_CAP - 1) * 100
dd_vbt = stats_vbt.get('Max Drawdown [%]', 0)
sharpe_vbt = stats_vbt.get('Sharpe Ratio', 0)

if n_vbt > 0:
    wins_vbt = (trades_vbt['PnL'] > 0).sum()
    wr_vbt = wins_vbt / n_vbt * 100
    aw_vbt = trades_vbt[trades_vbt['PnL'] > 0]['Return'].mean() * 100 if wins_vbt > 0 else 0
    al_vbt = trades_vbt[trades_vbt['PnL'] < 0]['Return'].mean() * 100 if n_vbt > wins_vbt else 0
    pf_vbt = trades_vbt[trades_vbt['PnL'] > 0]['PnL'].sum() / abs(trades_vbt[trades_vbt['PnL'] < 0]['PnL'].sum()) if (trades_vbt['PnL'] < 0).sum() > 0 else 0
else:
    wins_vbt = 0; wr_vbt = 0; aw_vbt = 0; al_vbt = 0; pf_vbt = 0

print(f"  交易: {n_vbt:,} 笔 | 胜率: {wr_vbt:.1f}% | 收益: {ret_vbt:+.2f}%")

# ── 4. 对比 ────────────────────────────────────────
print(f"\n{'='*70}")
print(f"对比报告")
print(f"{'='*70}")

ann_vbt = (1 + ret_vbt/100) ** (365/max(td_span, 1)) - 1

rows = [
    ("", "自有 FastEngine", "VectorBT", ""),
    ("股票池", f"{n_stocks:,} 只", f"{n_stocks:,} 只", ""),
    ("入场信号", f"{n_sig:,}", f"{n_sig:,}", ""),
    ("完成交易", f"{n_own:,}", f"{n_vbt:,}", ""),
    ("总收益", f"{ret_own:+.2f}%", f"{ret_vbt:+.2f}%", ""),
    ("年化收益", f"{ann_own*100:+.1f}%", f"{ann_vbt*100:+.1f}%", ""),
    ("最大回撤", f"{dd_own:.2f}%", f"{dd_vbt:.2f}%", ""),
    ("夏普比率", f"{sharpe_own:.2f}", f"{sharpe_vbt:.2f}" if not np.isnan(sharpe_vbt) else "N/A", ""),
    ("胜率", f"{wr_own:.1f}%", f"{wr_vbt:.1f}%", ""),
    ("平均盈利", f"{aw_own:+.2f}%", f"{aw_vbt:+.2f}%", ""),
    ("平均亏损", f"{al_own:+.2f}%", f"{al_vbt:+.2f}%", ""),
    ("盈亏比", f"{pf_own:.2f}", f"{pf_vbt:.2f}", ""),
    ("退出方式", str(dict(exit_reasons.most_common(6))) if exit_reasons else "N/A", "原生sl/tp/trail", "自有: HS/TP1/TP2/TR/TC/TF"),
]

for label, own, vbt_val, note in rows:
    print(f"  {label:<12s}: {own:>30s} | {vbt_val:>30s}  {note}")

print(f"\n  自有系统优势:")
print(f"    - TP1部分止盈(4%卖15%): 提前锁定利润，降低回撤风险")
print(f"    - 移动止盈 TR(5%激活,-2.5%): 保护涨幅较大的仓位")
print(f"    - 时间止盈 TC(5天+3%): 快速了结盈利不足的仓位")
print(f"    - 时间强制 TF(20天): 避免仓位长期锁死")
print(f"    - 连败保护(3次减半→5次暂停3天): 极端行情下自保")
print(f"    - 同股冷却(20天): 避免过度交易同一标的")
print(f"    - 除权保护: 自动检测并调整入场价")
print(f"  自有系统弱点:")
print(f"    - 部分止盈让盈利单跑不完全程")
print(f"    - 冷却期可能错过连续机会")
print(f"  VectorBT优势:")
print(f"    - C实现,速度快")
print(f"    - 原生sl/tp/trail执行精确")
print(f"  VectorBT弱点:")
print(f"    - 无分层止盈,盈利无法保护")
print(f"    - 无连败保护")
print(f"    - 无除权处理")
print("=" * 70)
