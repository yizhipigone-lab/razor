"""排查交易量偏低的原因"""
import sys, os, warnings, glob, struct
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ROOT = Path(__file__).resolve().parent.parent

def decode_tdx_date(raw):
    y = raw // 2048 + 2004
    md = raw % 2048
    m = md // 100
    d = md % 100
    try:
        return datetime(y, m, d).date()
    except:
        return None

def parse_tdx_min(filepath):
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
        records.append({'datetime': dt, 'open': op, 'high': hi, 'low': lo, 'close': cl, 'volume': vol if vol > 0 else 0})
    return pd.DataFrame(records) if records else pd.DataFrame()


def load_min(ext, dir_name, max_stocks, start_dt, end_dt):
    all_dfs = []
    for mkt_dir in ['sh', 'sz']:
        d = Path(r'E:\NEW_TDX\vipdoc') / mkt_dir / dir_name
        files = sorted(d.glob(f'*.{ext}'))
        count = 0
        for f in files:
            code = f.stem[2:] if f.stem.startswith(('sh','sz')) else f.stem
            if len(code) != 6 or not code.isdigit() or code[0] not in '603':
                continue
            count += 1
            if count > max_stocks:
                break
            df = parse_tdx_min(str(f))
            if len(df) < 100:
                continue
            df['code'] = code
            all_dfs.append(df)
    if not all_dfs:
        return pd.DataFrame()
    df = pd.concat(all_dfs, ignore_index=True)
    if start_dt:
        df = df[df['datetime'] >= start_dt]
    if end_dt:
        df = df[df['datetime'] <= end_dt]
    return df.sort_values(['code','datetime']).reset_index(drop=True)


def check_signals(bars, label):
    """检查信号密度和时间分布"""
    print(f"\n{'='*60}")
    print(f"[{label}] 信号诊断")
    print(f"{'='*60}")

    results = []
    for code, grp in bars.groupby('code'):
        time_col = 'datetime' if 'datetime' in grp.columns else 'date'
        df = grp.sort_values(time_col).set_index(time_col).copy()
        if len(df) < 300:
            continue

        df['ma5']  = df['close'].rolling(5).mean()
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        df['x1'] = (df['ma5'] - df['ma5'].shift(5)) / df['ma5'].shift(5) * 100
        df['x2'] = df['x1'].rolling(5).mean()
        df['cross_up'] = (df['x1'] > df['x2']) & (df['x1'].shift(1) <= df['x2'].shift(1))
        df['cond_angle'] = df['cross_up'] & (df['x1'] > df['x1'].shift(1))

        df['avg_vol_20'] = df['volume'].shift(1).rolling(20).mean()
        df['cond_vol'] = df['volume'] > df['avg_vol_20'] * 1.5

        h_l = df['high'] - df['low']
        h_l = h_l.replace(0, np.nan)
        df['close_pos'] = ((df['close'] - df['low']) / h_l).fillna(0.5).clip(0, 1)
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

        # 记录各层过滤统计
        results.append({
            'code': code,
            'bars': len(df),
            'cross_up': int(df['cross_up'].sum()),
            'cond_angle': int(df['cond_angle'].sum()),
            'cond_vol': int(df['cond_vol'].sum()),
            'cond_close': int(df['cond_close'].sum()),
            'cond_price': int(df['cond_price'].sum()),
            'za': int(df['za'].sum()),
            'buy': int(df['buy'].sum()),
            'buy_signal': int(df['buy_signal'].sum()),
        })

    stats = pd.DataFrame(results)
    if stats.empty:
        print("  无数据")
        return

    print(f"\n  逐层过滤统计 (每只股票平均):")
    for col in ['cross_up', 'cond_angle', 'cond_vol', 'cond_close', 'cond_price', 'za', 'buy', 'buy_signal']:
        print(f"    {col:<16s}: 总计 {stats[col].sum():>8d}  |  平均 {stats[col].mean():>8.1f}/只  |  中位 {stats[col].median():>8.0f}/只")

    # 检查信号的时间聚集度
    if 'buy_signal' in stats.columns and stats['buy_signal'].sum() > 0:
        sig_total = stats['buy_signal'].sum()
        # 检查有多少只股票有信号
        has_sig = (stats['buy_signal'] > 0).sum()
        print(f"\n  信号分布: {has_sig}/{len(stats)} 只股票产生了信号")
        print(f"  信号最多的前10只: {stats.nlargest(10, 'buy_signal')[['code','buy_signal']].to_string(index=False)}")

        # 检查无信号的股票占比
        no_sig = (stats['buy_signal'] == 0).sum()
        print(f"  无信号股票: {no_sig}/{len(stats)} ({no_sig/len(stats)*100:.1f}%)")

        # za 层 vs buy 层（20日新鲜度过滤损失）
        za_total = stats['za'].sum()
        buy_total = stats['buy'].sum()
        print(f"\n  ZA → Buy: {za_total} → {buy_total} (20日新鲜度过滤: {za_total-buy_total} 个被过滤)")
        print(f"  Buy → BuySignal: {buy_total} → {sig_total} (连续信号过滤: {buy_total-sig_total} 个被过滤)")


# ─── 日线 ───────────────────────────────────────
print("加载日线数据...")
DAILY_DIR = ROOT / "data" / "parquet" / "daily"
files = sorted(DAILY_DIR.glob("*.parquet"))
target = set()
for f in files:
    code = f.stem
    if len(code) == 6 and code.isdigit() and code[0] in '603':
        target.add(code)
        if len(target) >= 50:
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
bars_d['datetime'] = bars_d['date']
bars_d = bars_d[(bars_d['date'] >= '2022-01-01') & (bars_d['date'] <= '2026-05-12')]
check_signals(bars_d, '日线 50只')

# ─── 5分钟 ──────────────────────────────────────
print("\n加载5分钟数据...")
bars_5 = load_min('lc5', 'fzline', 20,
                   start_dt=datetime(2024,6,27),
                   end_dt=datetime(2026,4,30))
check_signals(bars_5, '5分钟')

# ─── 1分钟 ──────────────────────────────────────
print("\n加载1分钟数据...")
today = datetime.now()
start_100d = today - timedelta(days=100)
bars_1 = load_min('lc1', 'minline', 20,
                   start_dt=start_100d,
                   end_dt=today)
check_signals(bars_1, '1分钟')

# ─── 关键诊断：资金利用率 ────────────────────────
print(f"\n{'='*60}")
print(f"资金利用率分析")
print(f"{'='*60}")
print(f"  假设: 初始资金100万, 单票5万, 最多同时持仓20只")
print(f"  5分钟: {bars_5['code'].nunique()}只股票, {len(bars_5):,}条记录")
# 估算每天同时有多少信号
print(f"  日线: 每只股票约1000个交易日, 信号密度低, 资金充足")
print(f"  5分钟: 每天48根K线, 信号密度约为日线的48倍")
print(f"  1分钟: 每天240根K线, 信号密度约为日线的240倍")
print(f"\n  结论: 分钟线信号密度过高→资金快速耗尽→大量信号被拒绝")
print(f"  解决: 降低单票仓位至1-2万, 或增加初始资金至500万+")
