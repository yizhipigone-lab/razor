"""
MA5 角度突破策略 — 双周期回测对比
====================================
日线: 2022-01-01 ~ 2026-05-12 (本地 parquet)
5分钟: 2024-06-27 ~ 2026-04-30 (通达信 fzline/*.lc5)
回测引擎: VectorBT + 统一的止盈止损规则
"""
import sys, os, warnings, glob, struct
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = ROOT / "data" / "parquet" / "daily"

# ── 公共参数 ──────────────────────────────────────────
INIT_CAP   = 1_000_000
MAX_STOCKS = 100        # 日线回测股票数
MIN5_STOCKS = 50        # 5分钟回测股票数（数据量大，减少数量）
HARD_STOP  = -0.06
TP_STOP    = 0.12
TRAIL_DD   = 0.025
FEES       = 0.0003
SLIPPAGE   = 0.001

print("=" * 70)
print("MA5 角度突破策略 — 双周期回测对比")
print("=" * 70)

# ══════════════════════════════════════════════════
# PART 1: 日线回测 (2022-2026)
# ══════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[1] 日线回测 (2022-01-01 ~ 2026-05-12)")
print("=" * 70)

def load_daily(max_stocks=MAX_STOCKS):
    """加载本地 parquet 日线数据"""
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
            df = pd.read_parquet(str(f), columns=['date','open','high','low','close','volume'])
            if len(df) < 500:
                continue
            df['code'] = code
            dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])
    for c in ['open','high','low','close','volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.dropna(subset=['close'])
    return df.sort_values(['code','date']).reset_index(drop=True)


print("  加载日线数据...")
bars_daily = load_daily(MAX_STOCKS)
codes_daily = sorted(bars_daily['code'].unique())
print(f"  股票: {len(codes_daily)} 只  |  记录: {len(bars_daily):,}")

# 生成信号
from app.screener.strategies.ma5_angle import generate_signals

print("  生成 MA5 角度突破信号...")
bars_buf = bars_daily[bars_daily['date'] >= date(2021, 9, 1)]
sig_daily = generate_signals(bars_buf, version="improved",
    filter_st=True, filter_bj=True,
    vol_threshold=1.5, close_position_threshold=0.8,
    disable_quality_sort=False,
    filter_consecutive_up=False, filter_gap_quality=False)

sig_daily['date'] = pd.to_datetime(sig_daily['date'])
sig_daily = sig_daily[sig_daily['date'] >= pd.Timestamp('2022-01-01')]
print(f"  入场信号: {len(sig_daily)} 个")

if len(sig_daily) == 0:
    print("  无信号，跳过日线回测")
else:
    # 转宽表
    bars_bt = bars_daily[(bars_daily['date'] >= pd.Timestamp('2022-01-01')) & (bars_daily['date'] <= pd.Timestamp('2026-05-12'))]

    def to_wide(df, field):
        tbl = df.pivot_table(index='date', columns='code', values=field, aggfunc='first').sort_index()
        tbl.index = pd.to_datetime(tbl.index)
        return tbl

    close_d = to_wide(bars_bt, 'close')
    open_d  = to_wide(bars_bt, 'open')
    high_d  = to_wide(bars_bt, 'high')
    low_d   = to_wide(bars_bt, 'low')

    # 入场信号宽表
    entries_d = pd.DataFrame(False, index=close_d.index, columns=close_d.columns)
    for _, r in sig_daily.iterrows():
        d = pd.Timestamp(r['date'])
        raw = str(r['code']).replace('.SH','').replace('.SZ','').replace('.BJ','')
        for c in close_d.columns:
            if str(c) == raw:
                if d in entries_d.index:
                    entries_d.loc[d, c] = True
                break

    print(f"  入场信号(宽表): {entries_d.sum().sum()} 丨 回测区间: {close_d.index[0].date()} ~ {close_d.index[-1].date()}")

    # 出场信号（与之前相同：X1下穿X2 或 跌破MA20）
    ma5_d = close_d.rolling(5).mean()
    ma20_d = close_d.rolling(20).mean()
    x1_d = (ma5_d - ma5_d.shift(5)) / ma5_d.shift(5) * 100
    x2_d = x1_d.rolling(5).mean()
    cross_down_d = (x1_d < x2_d) & (x1_d.shift(1) >= x2_d.shift(1))
    exits_d = cross_down_d | (close_d < ma20_d)
    exits_d = exits_d.shift(1).fillna(False).astype(bool)
    exits_d = exits_d & ~entries_d

    # VectorBT 回测
    import vectorbt as vbt

    portfolio_d = vbt.Portfolio.from_signals(
        close=close_d, entries=entries_d, price=open_d,
        init_cash=INIT_CAP, fees=FEES, slippage=SLIPPAGE, freq='D',
        size_type='value', size=50000,
        sl_stop=abs(HARD_STOP), tp_stop=TP_STOP, sl_trail=TRAIL_DD,
        high=high_d, low=low_d, direction='longonly', cash_sharing=True,
    )

    stats_d = portfolio_d.stats()
    trades_d = portfolio_d.trades.records_readable
    n_d = len(trades_d)
    eq_d = stats_d.get('End Value', INIT_CAP)
    ret_d = (eq_d / INIT_CAP - 1) * 100
    dd_d = stats_d.get('Max Drawdown [%]', 0)
    sharpe_d = stats_d.get('Sharpe Ratio', 0)
    wr_d = (trades_d['PnL'] > 0).sum() / n_d * 100 if n_d > 0 else 0

    print(f"\n  日线回测结果:")
    print(f"    交易: {n_d} 笔  |  胜率: {wr_d:.1f}%")
    print(f"    收益: {ret_d:+.2f}%  |  最大回撤: {dd_d:.2f}%  |  夏普: {sharpe_d:.2f}")
    print(f"    初始: {INIT_CAP:,.0f}  →  最终: {eq_d:,.0f}")

# ══════════════════════════════════════════════════
# PART 2: 5分钟回测 (2024-06 ~ 2026-04)
# ══════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[2] 5分钟回测 (2024-06-27 ~ 2026-04-30)")
print("=" * 70)

def parse_tdx_lc5(filepath):
    """
    解析通达信 .lc5 文件（5分钟线）
    返回 DataFrame [date, time, open, high, low, close, volume, amount]
    """
    with open(filepath, 'rb') as f:
        data = f.read()

    rec_size = 32
    n = len(data) // rec_size
    if n == 0:
        return pd.DataFrame()

    records = []
    for i in range(n):
        offset = i * rec_size
        raw_date = struct.unpack_from('H', data, offset)[0]
        raw_time = struct.unpack_from('H', data, offset + 2)[0]
        op = struct.unpack_from('f', data, offset + 4)[0]
        hi = struct.unpack_from('f', data, offset + 8)[0]
        lo = struct.unpack_from('f', data, offset + 12)[0]
        cl = struct.unpack_from('f', data, offset + 16)[0]
        amt = struct.unpack_from('f', data, offset + 20)[0]
        vol = struct.unpack_from('f', data, offset + 24)[0]

        # 日期解码
        y = raw_date // 2048 + 2004
        md = raw_date % 2048
        m = md // 100
        d = md % 100
        # 时间解码：分钟数从 00:00 起
        hour = raw_time // 60
        minute = raw_time % 60

        try:
            dt = datetime(y, m, d, hour, minute)
        except (ValueError, OverflowError):
            continue

        records.append({
            'datetime': dt,
            'open': op, 'high': hi, 'low': lo, 'close': cl,
            'volume': vol, 'amount': amt,
        })

    if not records:
        return pd.DataFrame()
    return pd.DataFrame(records)


def load_min5(max_stocks=MIN5_STOCKS):
    """从通达信 fzline 目录加载 5分钟数据"""
    all_dfs = []

    for market, mkt_dir in [('SH', 'sh'), ('SZ', 'sz')]:
        fz_dir = Path(r'E:\NEW_TDX\vipdoc') / mkt_dir / 'fzline'
        files = sorted(fz_dir.glob('*.lc5'))

        target_count = 0
        for f in files:
            code = f.stem  # e.g., 'sh000001'
            raw_code = code[2:] if code.startswith('sh') else code[2:] if code.startswith('sz') else code
            if len(raw_code) != 6 or not raw_code.isdigit():
                continue
            if raw_code[0] not in '603':
                continue

            target_count += 1
            if target_count > max_stocks:
                break

            df = parse_tdx_lc5(str(f))
            if len(df) < 100:
                continue

            df['code'] = raw_code
            # 补充后缀
            if raw_code.startswith('6'):
                df['code'] = raw_code + '.SH'
            elif raw_code.startswith(('0', '3')):
                df['code'] = raw_code + '.SZ'

            all_dfs.append(df)

    if not all_dfs:
        return pd.DataFrame()

    df = pd.concat(all_dfs, ignore_index=True)
    return df.sort_values(['code', 'datetime']).reset_index(drop=True)


print("  加载5分钟数据（解析 TDX .lc5 文件）...")
min5_bars = load_min5(MIN5_STOCKS)
n_stocks_5 = min5_bars['code'].nunique() if len(min5_bars) > 0 else 0
print(f"  股票: {n_stocks_5} 只  |  记录: {len(min5_bars):,}")

if len(min5_bars) == 0:
    print("  无5分钟数据，跳过")
else:
    print(f"  日期范围: {min5_bars['datetime'].min()} ~ {min5_bars['datetime'].max()}")

    # 截取回测区间
    min5_bars = min5_bars[(min5_bars['datetime'] >= pd.Timestamp('2024-06-27')) &
                           (min5_bars['datetime'] <= pd.Timestamp('2026-04-30'))]

    # 生成5分钟级别信号（与日线策略逻辑一致，参数按比例缩放）
    print("  生成5分钟 MA5 角度突破信号...")

    # 按股票分组计算
    results = []
    for code, grp in min5_bars.groupby('code'):
        df = grp.sort_values('datetime').copy()
        if len(df) < 300:  # 至少300根K线
            continue

        # 均线（5分钟周期对应日线参数）
        df['ma5']  = df['close'].rolling(5).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()

        # X1/X2
        df['x1'] = (df['ma5'] - df['ma5'].shift(5)) / df['ma5'].shift(5) * 100
        df['x2'] = df['x1'].rolling(5).mean()

        # 金叉
        df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
        df['cond_angle'] = df['cross_up'] & (df['x1'] > df['x1'].shift(1))

        # 量确认
        df['avg_vol_20'] = df['volume'].shift(1).rolling(20).mean()
        df['cond_vol'] = df['volume'] > df['avg_vol_20'] * 1.5

        # 收盘位置
        df['close_pos'] = (df['close'] - df['low']) / (df['high'] - df['low'])
        # 避免除零
        df['close_pos'] = df['close_pos'].replace([np.inf, -np.inf], 0.5).fillna(0.5)
        df['cond_close'] = df['close_pos'] > 0.8

        # 价格结构
        df['rh20'] = df['high'].shift(1).rolling(20).max()
        df['rl20'] = df['low'].shift(1).rolling(20).min()
        df['rm20'] = (df['rh20'] + df['rl20']) / 2
        df['cond_price'] = (df['close'] > df['ma20']) & (df['close'] > df['rm20']) & (df['ma60'] >= df['ma60'].shift(10))

        # 综合信号
        df['za'] = df['cond_angle'] & df['cond_price'] & df['cond_vol'] & df['cond_close']
        za_int = df['za'].astype(int)
        df['count_20'] = za_int.rolling(20, min_periods=1).sum()
        df['buy'] = df['za'] & (df['count_20'] == 1)
        df['buy_signal'] = df['buy'] & (~df['buy'].shift(1).fillna(False))

        signals = df[df['buy_signal']][['datetime', 'code', 'close']].copy()
        if len(signals) > 0:
            results.append(signals)

    sig_min5 = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
    print(f"  入场信号: {len(sig_min5)} 个")

    if len(sig_min5) == 0:
        print("  无信号，跳过5分钟回测")
    else:
        # 转宽表
        min5_bars['date'] = min5_bars['datetime'].dt.date

        def to_wide_5(df, field):
            tbl = df.pivot_table(index='datetime', columns='code', values=field, aggfunc='first').sort_index()
            tbl.index = pd.to_datetime(tbl.index)
            return tbl

        close_5 = to_wide_5(min5_bars, 'close')
        open_5  = to_wide_5(min5_bars, 'open')
        high_5  = to_wide_5(min5_bars, 'high')
        low_5   = to_wide_5(min5_bars, 'low')

        # 确保列一致
        all_codes = sorted(list(set(close_5.columns) & set(open_5.columns) & set(high_5.columns) & set(low_5.columns)))
        close_5 = close_5[all_codes]
        open_5  = open_5[all_codes]
        high_5  = high_5[all_codes]
        low_5   = low_5[all_codes]

        print(f"  宽表: {close_5.shape[0]} 行 x {close_5.shape[1]} 列")

        # 入场信号宽表
        entries_5 = pd.DataFrame(False, index=close_5.index, columns=close_5.columns)
        for _, r in sig_min5.iterrows():
            d = pd.Timestamp(r['datetime'])
            raw = str(r['code']).replace('.SH','').replace('.SZ','').replace('.BJ','')
            for c in close_5.columns:
                c_raw = str(c).replace('.SH','').replace('.SZ','').replace('.BJ','')
                if c_raw == raw:
                    if d in entries_5.index:
                        entries_5.loc[d, c] = True
                    break

        n_entry_5 = entries_5.sum().sum()
        print(f"  入场信号(宽表): {n_entry_5}")

        if n_entry_5 == 0:
            print("  宽表无信号，跳过5分钟回测")
        else:
            # 出场信号
            ma5_5 = close_5.rolling(5).mean()
            ma20_5 = close_5.rolling(20).mean()
            x1_5 = (ma5_5 - ma5_5.shift(5)) / ma5_5.shift(5) * 100
            x2_5 = x1_5.rolling(5).mean()
            cross_down_5 = (x1_5 < x2_5) & (x1_5.shift(1) >= x2_5.shift(1))
            exits_5 = cross_down_5 | (close_5 < ma20_5)
            exits_5 = exits_5.shift(1).fillna(False).astype(bool)
            exits_5 = exits_5 & ~entries_5

            import vectorbt as vbt

            portfolio_5 = vbt.Portfolio.from_signals(
                close=close_5, entries=entries_5, price=open_5,
                init_cash=INIT_CAP, fees=FEES, slippage=SLIPPAGE, freq='5min',
                size_type='value', size=50000,
                sl_stop=abs(HARD_STOP), tp_stop=TP_STOP, sl_trail=TRAIL_DD,
                high=high_5, low=low_5, direction='longonly', cash_sharing=True,
            )

            stats_5 = portfolio_5.stats()
            trades_5 = portfolio_5.trades.records_readable
            n_5 = len(trades_5)
            eq_5 = stats_5.get('End Value', INIT_CAP)
            ret_5 = (eq_5 / INIT_CAP - 1) * 100
            dd_5 = stats_5.get('Max Drawdown [%]', 0)
            sharpe_5 = stats_5.get('Sharpe Ratio', 0)
            wr_5 = (trades_5['PnL'] > 0).sum() / n_5 * 100 if n_5 > 0 else 0

            print(f"\n  5分钟回测结果:")
            print(f"    交易: {n_5} 笔  |  胜率: {wr_5:.1f}%")
            print(f"    收益: {ret_5:+.2f}%  |  最大回撤: {dd_5:.2f}%  |  夏普: {sharpe_5:.2f}")
            print(f"    初始: {INIT_CAP:,.0f}  →  最终: {eq_5:,.0f}")

# ══════════════════════════════════════════════════
# 对比总结
# ══════════════════════════════════════════════════
print("\n" + "=" * 70)
print("[3] 双周期回测对比")
print("=" * 70)

# 提取指标
def safe(val, fmt='.2f'):
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return 'N/A'
    if isinstance(val, float):
        return f'{val:{fmt}}'
    return str(val)

rows = [
    ("周期", "日线", "5分钟"),
    ("区间", "2022-01 ~ 2026-05", "2024-06 ~ 2026-04"),
    ("股票数", str(len(codes_daily)) if 'codes_daily' in dir() else 'N/A',
     str(n_stocks_5) if 'n_stocks_5' in dir() else 'N/A'),
    ("入场信号", str(len(sig_daily)) if 'sig_daily' in dir() else 'N/A',
     str(len(sig_min5)) if 'sig_min5' in dir() and len(sig_min5) > 0 else 'N/A'),
]

if 'n_d' in dir():
    rows += [
        ("交易笔数", str(n_d), str(n_5) if 'n_5' in dir() else 'N/A'),
        ("胜率", f"{wr_d:.1f}%", f"{wr_5:.1f}%" if 'wr_5' in dir() else 'N/A'),
        ("总收益", f"{ret_d:+.2f}%", f"{ret_5:+.2f}%" if 'ret_5' in dir() else 'N/A'),
        ("最大回撤", f"{dd_d:.2f}%", f"{dd_5:.2f}%" if 'dd_5' in dir() else 'N/A'),
        ("夏普比率", f"{sharpe_d:.2f}", f"{sharpe_5:.2f}" if 'sharpe_5' in dir() else 'N/A'),
        ("年化收益", f"{(((1+ret_d/100)**(365/(365*4+130)))-1)*100:+.1f}%",
         f"{(((1+ret_5/100)**(365/(365*2-57)))-1)*100:+.1f}%" if 'ret_5' in dir() and 'n_5' in dir() else 'N/A'),
        ("平均盈利", f"{trades_d[trades_d['PnL']>0]['Return'].mean()*100:.2f}%" if n_d > 0 and (trades_d['PnL']>0).sum() > 0 else 'N/A',
         f"{trades_5[trades_5['PnL']>0]['Return'].mean()*100:.2f}%" if 'n_5' in dir() and n_5 > 0 and (trades_5['PnL']>0).sum() > 0 else 'N/A'),
        ("平均亏损", f"{trades_d[trades_d['PnL']<0]['Return'].mean()*100:.2f}%" if n_d > 0 and (trades_d['PnL']<0).sum() > 0 else 'N/A',
         f"{trades_5[trades_5['PnL']<0]['Return'].mean()*100:.2f}%" if 'n_5' in dir() and n_5 > 0 and (trades_5['PnL']<0).sum() > 0 else 'N/A'),
    ]

for label, daily, min5 in rows:
    print(f"  {label:<15s}: {daily:>20s}  |  {min5:>20s}")

print("\n" + "=" * 70)
print("结论:")
print("  - 日线: 数据覆盖4年+，信号稳定低频，适合中期趋势跟踪")
print("  - 5分钟: 数据覆盖近2年，信号更频繁，适合日内波段")
print("  - 5分钟回测计算量约为日线的48倍（每天48根5分钟K线）")
print("=" * 70)
