"""
回测系统对比：VectorBT vs 自有FastEngine
========================================
统一：入场信号(MA5角度突破) + 数据源(本地parquet)
对比：自有系统(完整止盈止损) vs VectorBT(原生止盈止损)
"""
import sys, os, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# ── 公共参数 ──────────────────────────────────────────
START_DATE = date(2025, 1, 1)
END_DATE   = date(2026, 5, 9)
INIT_CAP   = 1_000_000
MAX_STOCKS = 100

# 出场参数（两套系统共用）
HARD_STOP    = -0.06    # -6% 硬止损
TP           = 0.12     # +12% 止盈（VectorBT 不区分 TP1/TP2）
TRAIL_ACT    = 0.05     # +5% 激活移动止盈
TRAIL_DD     = 0.025    # 回撤 2.5% 触发
TIME_FORCE   = 20       # 20 天强制离场

print("=" * 70)
print("回测系统对比: VectorBT vs 自有 FastEngine")
print("=" * 70)

# ── 1. 数据加载 ──────────────────────────────────────
print("\n[1/5] 加载数据...")

def load_stocks(max_stocks=MAX_STOCKS):
    """加载日线数据，返回长表"""
    files = sorted(DAILY_DIR.glob("*.parquet"))
    target = set()
    for f in files:
        code = f.stem
        if len(code) == 6 and code.isdigit() and code[0] in '603':
            target.add(code)
            if len(target) >= max_stocks:
                break

    dfs = []
    for f in files:
        code = f.stem
        if code in target:
            df = pd.read_parquet(str(f), columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            if len(df) < 300:  # 排除次新股
                continue
            df['code'] = code
            dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)
    df['date'] = pd.to_datetime(df['date']).dt.date
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close'])
    return df.sort_values(['code', 'date']).reset_index(drop=True)


bars = load_stocks(MAX_STOCKS)
# Filter to backtest period (plus buffer for MA calculation)
bars_bt = bars[(bars['date'] >= START_DATE) & (bars['date'] <= END_DATE)].copy()
codes = sorted(bars_bt['code'].unique())
td_list = sorted(bars_bt['date'].unique())
print(f"  股票: {len(codes)} 只  |  交易日: {len(td_list)} 天")
print(f"  区间: {td_list[0]} ~ {td_list[-1]}")

# ── 2. 入场信号（两套系统共用） ───────────────────────
print("\n[2/5] 计算 MA5 角度突破入场信号...")

from app.screener.strategies.ma5_angle import generate_signals

# 用 buffer 数据计算信号
bars_buf = bars[(bars['date'] >= date(2024, 9, 1)) & (bars['date'] <= END_DATE)].copy()
print(f"  缓冲区数据: {bars_buf['code'].nunique()} 只, {len(bars_buf)} 行")

sig = generate_signals(bars_buf, version="improved",
                       filter_st=True, filter_bj=True,
                       vol_threshold=1.5, close_position_threshold=0.8,
                       disable_quality_sort=False,
                       filter_consecutive_up=False, filter_gap_quality=False)

sig = sig[(sig['date'] >= START_DATE) & (sig['date'] <= END_DATE)].copy()
sig['date'] = pd.to_datetime(sig['date']).dt.date
n_signals = len(sig)
print(f"  入场信号: {n_signals} 个")

if n_signals == 0:
    print("  无信号，退出")
    sys.exit(0)

# ── 3. 自有系统回测（完整止盈止损） ────────────────────
print("\n[3/5] 自有 FastEngine 回测...")

from app.backtest.simple_runner import FastEngine, Position, Trade

# 准备日线快照
closes = {}
highs = {}
lows = {}
opens = {}
for d, g in bars_bt.groupby('date'):
    closes[d] = dict(zip(g['code'], g['close']))
    highs[d] = dict(zip(g['code'], g['high']))
    lows[d] = dict(zip(g['code'], g['low']))
    opens[d] = dict(zip(g['code'], g['open']))

# 信号按日期分组
from collections import defaultdict
sbd = defaultdict(list)
for _, r in sig.iterrows():
    sbd[r['date']].append((r['code'], float(r['close'])))

params = {
    'initial_capital': INIT_CAP,
    'position_size': 50000,
    'min_buy_amt': 5000,
    'hard_stop': HARD_STOP,
    'tp1_pct': 0.04, 'tp1_sell_ratio': 0.15,  # 部分止盈
    'tp2_pct': TP,                                # 全部止盈
    'trail_activate': TRAIL_ACT,
    'trail_dd': TRAIL_DD,
    'time_exit_days': 3, 'time_exit_profit': 0.03,
    'time_force_days': TIME_FORCE,
    'loss_streak_halve': 3,
    'loss_streak_pause': 5,
    'pause_days': 3,
    'same_stock_cooldown': 20,
}

eng = FastEngine(td_list, params)
cooldown = 20

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
            if eng.cash < min(eng.max_pos(), params['min_buy_amt']):
                break
            if any(t.code == code and (d - t.entry_date).days <= cooldown for t in eng.trades):
                continue
            eng.buy(d, code, px)

    eng.record(d, snap)

# 自有系统统计
trades_own = eng.trades
eq_own = pd.DataFrame(eng.equity)
n_own = len(trades_own)
eq_end_own = eq_own['equity'].iloc[-1] if len(eq_own) > 0 else INIT_CAP
ret_own = (eq_end_own / INIT_CAP - 1) * 100
eq_own['cmax'] = eq_own['equity'].cummax()
eq_own['dd'] = (eq_own['equity'] - eq_own['cmax']) / eq_own['cmax'] * 100
max_dd_own = float(eq_own['dd'].min()) if len(eq_own) > 0 else 0
wins_own = sum(1 for t in trades_own if t.ret > 0)
wr_own = wins_own / n_own * 100 if n_own > 0 else 0
avg_win_own = np.mean([t.ret for t in trades_own if t.ret > 0]) if wins_own > 0 else 0
avg_loss_own = np.mean([t.ret for t in trades_own if t.ret <= 0]) if n_own > wins_own else 0
pf_own = sum(t.profit for t in trades_own if t.profit > 0) / abs(sum(t.profit for t in trades_own if t.profit < 0)) if any(t.profit < 0 for t in trades_own) else 0

print(f"  交易: {n_own} 笔  |  胜率: {wr_own:.1f}%")

# ── 4. VectorBT 回测（等价的止盈止损） ────────────────
print("\n[4/5] VectorBT 回测（等价位止盈止损）...")

import vectorbt as vbt

# 构造宽表（必须用 pd.Timestamp 索引）
def to_wide(df, field):
    tbl = df.pivot_table(index='date', columns='code', values=field, aggfunc='first').sort_index()
    tbl.index = pd.to_datetime(tbl.index)
    return tbl

bars_bt_vbt = bars_bt.copy()
bars_bt_vbt['date'] = pd.to_datetime(bars_bt_vbt['date'])

close_w = to_wide(bars_bt_vbt, 'close')
open_w  = to_wide(bars_bt_vbt, 'open')
high_w  = to_wide(bars_bt_vbt, 'high')
low_w   = to_wide(bars_bt_vbt, 'low')
vol_w   = to_wide(bars_bt_vbt, 'volume')

print(f"  宽表: {close_w.shape[0]} 行 x {close_w.shape[1]} 列, index type: {type(close_w.index[0])}")

# 入场信号（转宽表）
entries_w = pd.DataFrame(False, index=close_w.index, columns=close_w.columns)
for _, r in sig.iterrows():
    d = pd.Timestamp(r['date'])
    # 匹配代码
    raw_code = str(r['code']).replace('.SH', '').replace('.SZ', '').replace('.BJ', '')
    for c in close_w.columns:
        if str(c) == raw_code or str(c) == r['code']:
            if d in entries_w.index:
                entries_w.loc[d, c] = True
            break

print(f"  入场信号(宽表): {entries_w.sum().sum()} 个")

# 出场：VectorBT 用原生 sl_stop / tp_stop / sl_trail
exits_w = pd.DataFrame(False, index=close_w.index, columns=close_w.columns)

# VectorBT 回测
portfolio_vbt = vbt.Portfolio.from_signals(
    close=close_w,
    entries=entries_w,
    price=open_w,
    init_cash=INIT_CAP,
    fees=0.0003,
    slippage=0.001,
    freq='D',
    size_type='value',
    size=50000,         # 单票5万，与自有系统一致
    # 止损止盈
    sl_stop=abs(HARD_STOP),
    tp_stop=TP,
    sl_trail=TRAIL_DD,
    # 使用 high/low 检测日内触发
    high=high_w,
    low=low_w,
    # 只做多
    direction='longonly',
    # 共享资金池
    cash_sharing=True,
)

stats_vbt = portfolio_vbt.stats()
trades_vbt = portfolio_vbt.trades.records_readable
n_vbt = len(trades_vbt)
eq_end_vbt = stats_vbt.get('End Value', INIT_CAP)
ret_vbt = (eq_end_vbt / INIT_CAP - 1) * 100
dd_vbt = stats_vbt.get('Max Drawdown [%]', 0)

# VectorBT 统计
if n_vbt > 0:
    wins_vbt = (trades_vbt['PnL'] > 0).sum()
    wr_vbt = wins_vbt / n_vbt * 100
    avg_win_vbt = trades_vbt[trades_vbt['PnL'] > 0]['Return'].mean() * 100 if wins_vbt > 0 else 0
    avg_loss_vbt = trades_vbt[trades_vbt['PnL'] < 0]['Return'].mean() * 100 if n_vbt > wins_vbt else 0
    total_wins = trades_vbt[trades_vbt['PnL'] > 0]['PnL'].sum()
    total_losses = abs(trades_vbt[trades_vbt['PnL'] < 0]['PnL'].sum())
    pf_vbt = total_wins / total_losses if total_losses > 0 else 0
else:
    wins_vbt = 0; wr_vbt = 0; avg_win_vbt = 0; avg_loss_vbt = 0; pf_vbt = 0

print(f"  交易: {n_vbt} 笔  |  胜率: {wr_vbt:.1f}%")

# ── 5. 对比报告 ──────────────────────────────────────
print("\n[5/5] 对比报告")
print("=" * 70)

# 计算日收益率用于夏普
def calc_sharpe(eq_df):
    if len(eq_df) < 2:
        return 0
    rets = eq_df['equity'].pct_change().dropna()
    if rets.std() == 0:
        return 0
    return float(np.sqrt(252) * rets.mean() / rets.std())

sharpe_own = calc_sharpe(eq_own) if len(eq_own) > 0 else 0

# 按退出原因统计自有系统
from collections import Counter
rc = Counter(t.reason for t in trades_own) if trades_own else {}

metrics = [
    ("", "自有 FastEngine", "VectorBT"),
    ("初始资金", f"{INIT_CAP:,.0f}", f"{INIT_CAP:,.0f}"),
    ("最终资金", f"{eq_end_own:,.0f}", f"{eq_end_vbt:,.0f}"),
    ("总收益率", f"{ret_own:+.2f}%", f"{ret_vbt:+.2f}%"),
    ("最大回撤", f"{max_dd_own:.2f}%", f"{dd_vbt:.2f}%" if isinstance(dd_vbt, (int, float)) and not np.isnan(dd_vbt) else "N/A"),
    ("夏普比率", f"{sharpe_own:.2f}", f"{stats_vbt.get('Sharpe Ratio', 0):.2f}" if not np.isnan(stats_vbt.get('Sharpe Ratio', 0)) else "N/A"),
    ("总交易", f"{n_own}", f"{n_vbt}"),
    ("胜率", f"{wr_own:.1f}%", f"{wr_vbt:.1f}%"),
    ("平均盈利", f"{avg_win_own:+.2f}%", f"{avg_win_vbt:+.2f}%"),
    ("平均亏损", f"{avg_loss_own:+.2f}%", f"{avg_loss_vbt:+.2f}%"),
    ("盈亏比", f"{pf_own:.2f}", f"{pf_vbt:.2f}"),
    ("退出原因", str(dict(rc.most_common(5))) if rc else "N/A", "原生 sl/tp/trail"),
]

for label, own, vbt_val in metrics:
    print(f"  {label:<15s}: {own:>25s}  |  {vbt_val:>25s}")

# 展示自有系统的净值曲线 vs VectorBT
print(f"\n  自有系统")
print(f"    入场参数: MA5角度突破改进版 + ST/北交所过滤")
print(f"    出场逻辑: HS({HARD_STOP:.0%}) / TP1(4%卖15%) / TP2({TP:.0%}) / TR({TRAIL_ACT:.0%}激活,{TRAIL_DD:.1%}) / TF({TIME_FORCE}天)")
print(f"    资金管理: 单票5万, 连败(3次减半,5次暂停3天), 同股冷却20天")
print(f"    除权保护: 自动检测, 调整入场价")

print(f"\n  VectorBT")
print(f"    入场参数: 同上")
print(f"    出场逻辑: sl_stop({abs(HARD_STOP):.0%}) / tp_stop({TP:.0%}) / sl_trail({TRAIL_DD:.1%})")
print(f"    日内检测: high/low 触发, close 执行")
print(f"    不足: 无部分止盈, 无连败保护, 无除权保护, 无冷却期")

print(f"\n  关键差异分析:")
# 为什么交易数不同
if n_own != n_vbt:
    print(f"    交易数差异 ({n_own} vs {n_vbt}):")
    print(f"      - 自有系统: 冷却期/连败暂停减少交易; 部分止盈产生多次卖出")
    print(f"      - VectorBT: 无冷却期; 每次止盈止损是完整退出")

# VectorBT stats details
print(f"\n  VectorBT 完整统计:")
for k, v in stats_vbt.items():
    print(f"    {k}: {v}")

print("=" * 70)
print(f"结论提示:")
print(f"  - 自有系统的止盈止损更精细(分层止盈+连败保护+除权处理)")
print(f"  - VectorBT 的原生止损止盈更快但无法模拟部分平仓等高级逻辑")
print(f"  - 建议: 入场用 MA5 角度突破信号, 出场用自有系统的分层止盈止损")
print(f"  - 脚本: scripts/compare_backtest_systems.py")
print("=" * 70)
