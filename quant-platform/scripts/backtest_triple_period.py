"""
MA5 角度突破 — 三周期回测 (日线 / 5分钟 / 1分钟)
==================================================
日线:   2022-01 ~ 2026-05  本地 parquet
5分钟:  2024-06 ~ 2026-04  通达信 fzline/*.lc5
1分钟:  最近100天            通达信 minline/*.lc1
引擎:   VectorBT + 统一止盈止损
"""
import sys, os, warnings, glob, struct
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent

# ── 参数 ──────────────────────────────────────────
INIT_CAP  = 5_000_000   # 500万，避免分钟线资金瓶颈
FEES      = 0.0003
SLIPPAGE  = 0.001
HARD_STOP = -0.06
TP_STOP   = 0.12
TRAIL_DD  = 0.025
POS_SIZE  = 50000        # 单票5万 = 最多100个仓位

def decode_tdx_date(raw):
    """通达信分钟线日期解码"""
    y = raw // 2048 + 2004
    md = raw % 2048
    m = md // 100
    d = md % 100
    try:
        return datetime(y, m, d).date()
    except:
        return None

def parse_tdx_min(filepath):
    """解析通达信 .lc1/.lc5 分钟线文件"""
    with open(filepath, 'rb') as f:
        data = f.read()
    rec_size = 32
    n = len(data) // rec_size
    if n == 0:
        return pd.DataFrame()
    records = []
    for i in range(n):
        off = i * rec_size
        rd = struct.unpack_from('H', data, off)[0]
        rt = struct.unpack_from('H', data, off + 2)[0]
        op = struct.unpack_from('f', data, off + 4)[0]
        hi = struct.unpack_from('f', data, off + 8)[0]
        lo = struct.unpack_from('f', data, off + 12)[0]
        cl = struct.unpack_from('f', data, off + 16)[0]
        amt = struct.unpack_from('f', data, off + 20)[0]
        vol = struct.unpack_from('f', data, off + 24)[0]

        dt_date = decode_tdx_date(rd)
        if dt_date is None:
            continue
        hour = rt // 60
        minute = rt % 60
        try:
            dt = datetime.combine(dt_date, datetime.min.time()) + timedelta(hours=hour, minutes=minute)
        except:
            continue

        records.append({
            'datetime': dt, 'open': op, 'high': hi, 'low': lo, 'close': cl,
            'volume': vol if vol > 0 else 0, 'amount': amt
        })
    return pd.DataFrame(records) if records else pd.DataFrame()


def load_min_data(ext, dir_name, max_stocks, start_dt=None, end_dt=None):
    """从通达信目录加载分钟数据"""
    all_dfs = []
    for mkt_name, mkt_dir in [('SH', 'sh'), ('SZ', 'sz')]:
        d = Path(r'E:\NEW_TDX\vipdoc') / mkt_dir / dir_name
        files = sorted(d.glob(f'*.{ext}'))
        count = 0
        for f in files:
            code = f.stem
            raw = code[2:] if code.startswith(('sh','sz')) else code
            if len(raw) != 6 or not raw.isdigit():
                continue
            if raw[0] not in '603':
                continue
            count += 1
            if count > max_stocks:
                break

            df = parse_tdx_min(str(f))
            if len(df) < 100:
                continue
            if raw.startswith('6'):
                code_full = raw + '.SH'
            elif raw.startswith(('0','3')):
                code_full = raw + '.SZ'
            else:
                code_full = raw
            df['code'] = code_full
            all_dfs.append(df)

    if not all_dfs:
        return pd.DataFrame()
    df = pd.concat(all_dfs, ignore_index=True)
    if start_dt:
        df = df[df['datetime'] >= start_dt]
    if end_dt:
        df = df[df['datetime'] <= end_dt]
    return df.sort_values(['code','datetime']).reset_index(drop=True)


def generate_signals_wide(bars, period_label):
    """为分钟/日线数据生成 MA5 角度突破信号"""
    results = []
    for code, grp in bars.groupby('code'):
        time_col = 'datetime' if 'datetime' in grp.columns else 'date'
        df = grp.sort_values(time_col).copy()
        if len(df) < 300:
            continue
        time_col = 'datetime' if 'datetime' in df.columns else 'date'
        df = df.set_index(time_col)

        df['ma5']  = df['close'].rolling(5).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()

        df['x1'] = (df['ma5'] - df['ma5'].shift(5)) / df['ma5'].shift(5) * 100
        df['x2'] = df['x1'].rolling(5).mean()
        df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
        df['cond_angle'] = df['cross_up'] & (df['x1'] > df['x1'].shift(1))

        df['avg_vol_20'] = df['volume'].shift(1).rolling(20).mean()
        df['cond_vol'] = df['volume'] > df['avg_vol_20'] * 1.5

        h_l_range = df['high'] - df['low']
        h_l_range = h_l_range.replace(0, np.nan)
        df['close_pos'] = ((df['close'] - df['low']) / h_l_range).fillna(0.5).clip(0, 1)
        df['cond_close'] = df['close_pos'] > 0.8

        df['rh20'] = df['high'].shift(1).rolling(20).max()
        df['rl20'] = df['low'].shift(1).rolling(20).min()
        df['rm20'] = (df['rh20'] + df['rl20']) / 2
        df['cond_price'] = (df['close'] > df['ma20']) & (df['close'] > df['rm20']) & (df['ma60'] >= df['ma60'].shift(10))

        df['za'] = df['cond_angle'] & df['cond_price'] & df['cond_vol'] & df['cond_close']
        za_int = df['za'].astype(int)
        df['count_20'] = za_int.rolling(20, min_periods=1).sum()
        df['buy'] = df['za'] & (df['count_20'] == 1)
        df['buy_signal'] = df['buy'] & (~df['buy'].shift(1).fillna(False))

        sig = df[df['buy_signal']].reset_index()
        sig['code'] = code
        sig = sig[[time_col, 'code', 'close']].copy()
        if len(sig) > 0:
            results.append(sig)
    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()


def run_vbt_backtest(bars, signals, period_label, freq='D'):
    """执行 VectorBT 回测"""
    time_col = 'datetime' if 'datetime' in bars.columns else 'date'
    bars[time_col] = pd.to_datetime(bars[time_col])

    def to_wide(df, field):
        t = df.pivot_table(index=time_col, columns='code', values=field, aggfunc='first').sort_index()
        t.index = pd.to_datetime(t.index)
        return t

    close_w = to_wide(bars, 'close')
    open_w  = to_wide(bars, 'open')
    high_w  = to_wide(bars, 'high')
    low_w   = to_wide(bars, 'low')

    if close_w.empty:
        return None, None, 0

    # 出场信号
    ma5_w  = close_w.rolling(5).mean()
    ma20_w = close_w.rolling(20).mean()
    x1_w = (ma5_w - ma5_w.shift(5)) / ma5_w.shift(5) * 100
    x2_w = x1_w.rolling(5).mean()
    cross_down = (x1_w < x2_w) & (x1_w.shift(1) >= x2_w.shift(1))
    # 出场：分钟线完全依赖 VectorBT 原生止盈止损，日线保留MA出场
    if freq == 'D':
        exits_w = cross_down | (close_w < ma20_w)
        exits_w = exits_w.shift(1).fillna(False).astype(bool)
    else:
        exits_w = None  # 分钟线：只用 sl_stop/tp_stop/sl_trail

    # 入场信号
    entries_w = pd.DataFrame(False, index=close_w.index, columns=close_w.columns)
    signals[time_col] = pd.to_datetime(signals[time_col])
    for _, r in signals.iterrows():
        d = r[time_col]
        raw = str(r['code']).replace('.SH','').replace('.SZ','').replace('.BJ','')
        for c in close_w.columns:
            c_raw = str(c).replace('.SH','').replace('.SZ','').replace('.BJ','')
            if c_raw == raw and d in entries_w.index:
                entries_w.loc[d, c] = True
                break

    n_entry = entries_w.sum().sum()
    if n_entry == 0:
        return None, None, 0

    if exits_w is not None:
        exits_w = exits_w & ~entries_w

    import vectorbt as vbt

    # 分钟线用更紧的止损止盈（加快资本周转）
    if freq == 'D':
        sl, tp, tr = abs(HARD_STOP), TP_STOP, TRAIL_DD
    elif freq == '5min':
        sl, tp, tr = 0.03, 0.05, 0.01
    else:  # 1min
        sl, tp, tr = 0.02, 0.03, 0.008

    portfolio = vbt.Portfolio.from_signals(
        close=close_w, entries=entries_w, exits=exits_w, price=open_w,
        init_cash=INIT_CAP, fees=FEES, slippage=SLIPPAGE, freq=freq,
        size_type='value', size=POS_SIZE,
        sl_stop=sl, tp_stop=tp, sl_trail=tr,
        high=high_w, low=low_w, direction='longonly', cash_sharing=True,
    )
    stats = portfolio.stats()
    trades = portfolio.trades.records_readable
    return stats, trades, n_entry


# ══════════════════════════════════════════════════
print("=" * 70)
print("MA5 角度突破 — 三周期回测")
print("=" * 70)

results_summary = {}

# ─── 1. 日线回测 ──────────────────────────────────
print("\n[1/3] 日线回测 (2022-01 ~ 2026-05)")
print("-" * 40)

DAILY_DIR = ROOT / "data" / "parquet" / "daily"
files = sorted(DAILY_DIR.glob("*.parquet"))
target = set()
for f in files:
    code = f.stem
    if len(code) == 6 and code.isdigit() and code[0] in '603':
        target.add(code)
        if len(target) >= 100:
            break

dfs = []
for f in files:
    code = f.stem
    if code in target:
        df = pd.read_parquet(str(f), columns=['date','open','high','low','close','volume'])
        if len(df) < 500:
            continue
        df['code'] = code
        dfs.append(df)

bars_d = pd.concat(dfs, ignore_index=True)
bars_d['date'] = pd.to_datetime(bars_d['date'])
for c in ['open','high','low','close','volume']:
    bars_d[c] = pd.to_numeric(bars_d[c], errors='coerce')
bars_d = bars_d.dropna(subset=['close']).sort_values(['code','date']).reset_index(drop=True)
bars_d = bars_d[(bars_d['date'] >= '2022-01-01') & (bars_d['date'] <= '2026-05-12')]
# datetime column needed
bars_d['datetime'] = bars_d['date']

print(f"  股票: {bars_d['code'].nunique()}  |  记录: {len(bars_d):,}")

sig_d = generate_signals_wide(bars_d, '日线')
print(f"  信号: {len(sig_d)} 个")

if len(sig_d) > 0:
    stats_d, trades_d, n_d = run_vbt_backtest(bars_d, sig_d, '日线', freq='D')
    if stats_d is not None:
        eq_d = stats_d.get('End Value', INIT_CAP)
        ret_d = (eq_d / INIT_CAP - 1) * 100
        dd_d = stats_d.get('Max Drawdown [%]', 0)
        n_t_d = len(trades_d)
        wr_d = (trades_d['PnL'] > 0).sum() / n_t_d * 100 if n_t_d > 0 else 0
        sh_d = stats_d.get('Sharpe Ratio', 0)
        results_summary['日线'] = {
            'trades': n_t_d, 'win_rate': wr_d, 'return': ret_d,
            'dd': dd_d, 'sharpe': sh_d, 'end_value': eq_d,
            'signals': n_d,
        }
        print(f"  交易: {n_t_d}笔  胜率: {wr_d:.1f}%  收益: {ret_d:+.2f}%  DD: {dd_d:.2f}%  夏普: {sh_d:.2f}")
    else:
        results_summary['日线'] = None
        print(f"  无交易")
else:
    results_summary['日线'] = None
    print(f"  无信号")

# ─── 2. 5分钟回测 ──────────────────────────────────
print("\n[2/3] 5分钟回测 (2024-06 ~ 2026-04)")
print("-" * 40)

bars_5 = load_min_data('lc5', 'fzline', 30,
                        start_dt=datetime(2024,6,27),
                        end_dt=datetime(2026,4,30))
if len(bars_5) > 0:
    print(f"  股票: {bars_5['code'].nunique()}  |  记录: {len(bars_5):,}")
    sig_5 = generate_signals_wide(bars_5, '5分钟')
    print(f"  信号: {len(sig_5)} 个")

    if len(sig_5) > 0:
        stats_5, trades_5, n_5 = run_vbt_backtest(bars_5, sig_5, '5分钟', freq='5min')
        if stats_5 is not None:
            eq_5 = stats_5.get('End Value', INIT_CAP)
            ret_5 = (eq_5 / INIT_CAP - 1) * 100
            dd_5 = stats_5.get('Max Drawdown [%]', 0)
            n_t_5 = len(trades_5)
            wr_5 = (trades_5['PnL'] > 0).sum() / n_t_5 * 100 if n_t_5 > 0 else 0
            sh_5 = stats_5.get('Sharpe Ratio', 0)
            results_summary['5分钟'] = {
                'trades': n_t_5, 'win_rate': wr_5, 'return': ret_5,
                'dd': dd_5, 'sharpe': sh_5, 'end_value': eq_5,
                'signals': n_5,
            }
            print(f"  交易: {n_t_5}笔  胜率: {wr_5:.1f}%  收益: {ret_5:+.2f}%  DD: {dd_5:.2f}%  夏普: {sh_5:.2f}")
        else:
            results_summary['5分钟'] = None
            print(f"  无交易")
    else:
        results_summary['5分钟'] = None
        print(f"  无信号")
else:
    results_summary['5分钟'] = None
    print(f"  无数据")

# ─── 3. 1分钟回测 ──────────────────────────────────
print("\n[3/3] 1分钟回测 (最近100天)")
print("-" * 40)

today = datetime.now()
start_100d = today - timedelta(days=100)
bars_1 = load_min_data('lc1', 'minline', 30,
                        start_dt=start_100d,
                        end_dt=today)
if len(bars_1) > 0:
    print(f"  股票: {bars_1['code'].nunique()}  |  记录: {len(bars_1):,}")
    sig_1 = generate_signals_wide(bars_1, '1分钟')
    print(f"  信号: {len(sig_1)} 个")

    if len(sig_1) > 0:
        stats_1, trades_1, n_1 = run_vbt_backtest(bars_1, sig_1, '1分钟', freq='1min')
        if stats_1 is not None:
            eq_1 = stats_1.get('End Value', INIT_CAP)
            ret_1 = (eq_1 / INIT_CAP - 1) * 100
            dd_1 = stats_1.get('Max Drawdown [%]', 0)
            n_t_1 = len(trades_1)
            wr_1 = (trades_1['PnL'] > 0).sum() / n_t_1 * 100 if n_t_1 > 0 else 0
            sh_1 = stats_1.get('Sharpe Ratio', 0)
            results_summary['1分钟'] = {
                'trades': n_t_1, 'win_rate': wr_1, 'return': ret_1,
                'dd': dd_1, 'sharpe': sh_1, 'end_value': eq_1,
                'signals': n_1,
            }
            print(f"  交易: {n_t_1}笔  胜率: {wr_1:.1f}%  收益: {ret_1:+.2f}%  DD: {dd_1:.2f}%  夏普: {sh_1:.2f}")
        else:
            results_summary['1分钟'] = None
            print(f"  无交易")
    else:
        results_summary['1分钟'] = None
        print(f"  无信号")
else:
    results_summary['1分钟'] = None
    print(f"  无数据")

# ══════════════════════════════════════════════════
# 对比表格
# ══════════════════════════════════════════════════
print("\n" + "=" * 70)
print("三周期对比")
print("=" * 70)

periods = [
    ('日线',   '2022-01 ~ 2026-05'),
    ('5分钟',  '2024-06 ~ 2026-04'),
    ('1分钟',  f'{start_100d.strftime("%Y-%m-%d")} ~ {today.strftime("%Y-%m-%d")}'),
]

header = f"{'指标':<12s}  {'日线':>18s}  {'5分钟':>18s}  {'1分钟':>18s}"
print(header)
print("-" * 70)

for label, key in [('入场信号', 'signals'), ('交易笔数', 'trades'), ('胜率(%)', 'win_rate'),
                     ('总收益(%)', 'return'), ('最大回撤(%)', 'dd'), ('夏普比率', 'sharpe')]:
    vals = []
    for pname, _ in periods:
        r = results_summary.get(pname)
        if r and r[key] is not None:
            if key in ('win_rate', 'return', 'dd'):
                vals.append(f"{r[key]:+.1f}%")
            elif key == 'sharpe':
                vals.append(f"{r[key]:.2f}")
            else:
                vals.append(f"{r[key]:.0f}")
        else:
            vals.append('N/A')
    print(f"  {label:<10s}: {vals[0]:>18s}  {vals[1]:>18s}  {vals[2]:>18s}")

print("-" * 70)
print("结论:")
print("  - 日线: 长期趋势跟踪，信号低频，胜率相对高")
print("  - 5分钟: 日内波段，信号密度高，需警惕过度交易")
print("  - 1分钟: 噪声大，MA5角度信号在分钟级容易漂移")
print("=" * 70)
