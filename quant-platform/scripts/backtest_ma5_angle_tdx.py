"""
MA5 角度突破策略 -- TdxQuant + VectorBT 回测验证
==================================================
数据源：优先 TdxQuant，备选本地 parquet
回测引擎：VectorBT 1.x
策略：app/screener/strategies/ma5_angle.py 改进版
"""
import sys, os, glob, warnings
warnings.filterwarnings('ignore')
import pandas as pd
import numpy as np

# ── 1. 环境 ──────────────────────────────────────────
TDX_PLUGIN = r'E:\NEW_TDX\PYPlugins\user'
if os.path.exists(TDX_PLUGIN):
    sys.path.insert(0, TDX_PLUGIN)
LOCAL_PARQUET = r'e:\1target\p9_project\quant-platform\data\parquet\daily'

# ── 2. 参数 ──────────────────────────────────────────
TARGET_START = '20250101'
TARGET_END   = '20260509'
MAX_STOCKS   = 50          # 回测股票数上限
INIT_CASH    = 1000000      # 初始100万
FEES         = 0.0003       # 万三

# 策略参数（与 screener 一致）
VOL_THRESHOLD = 1.5
CLOSE_POS_THRESHOLD = 0.8

print("=" * 70)
print("MA5 角度突破策略 -- VectorBT 回测")
print("=" * 70)

# ── 3. 数据获取 ──────────────────────────────────────
print("\n[1/4] 获取数据...")

def load_local_data(max_stocks=MAX_STOCKS):
    """从本地 parquet 加载数据"""
    files = sorted(glob.glob(os.path.join(LOCAL_PARQUET, '*.parquet')))
    if not files:
        raise FileNotFoundError(f"无数据: {LOCAL_PARQUET}")

    target_codes = set()
    for f in files:
        code = os.path.basename(f).replace('.parquet', '')
        # 只取沪深主板 + 创业板
        if code.startswith(('6', '0', '3')):
            target_codes.add(code)
            if len(target_codes) >= max_stocks:
                break

    dfs = []
    for f in files:
        code = os.path.basename(f).replace('.parquet', '')
        if code in target_codes:
            df = pd.read_parquet(f, columns=['date', 'open', 'high', 'low', 'close', 'volume'])
            if len(df) < 120:
                continue
            df['code'] = code
            dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)
    df['date'] = pd.to_datetime(df['date'])
    # 统一列名
    df.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low',
                       'close': 'Close', 'volume': 'Volume'}, inplace=True)
    # 补充市场后缀
    def add_suffix(c):
        c = str(c)
        if c.startswith('6'): return f'{c}.SH'
        if c.startswith(('0', '3')): return f'{c}.SZ'
        return c
    df['code'] = df['code'].apply(add_suffix)
    return df


def fetch_data_tdx():
    """通过 TdxQuant API 获取"""
    from tqcenter import tq
    tq.initialize(r'E:\NEW_TDX\PYPlugins\user\tdxdata_test.py')

    stock_list = ['000001.SZ', '000002.SZ', '600519.SH', '300750.SZ', '002594.SZ',
                  '601318.SH', '000858.SZ', '600036.SH', '002415.SZ', '300059.SZ',
                  '600887.SH', '002475.SZ', '300124.SZ', '000333.SZ', '600900.SH']
    start_time = (pd.to_datetime(TARGET_START) - pd.Timedelta(days=120)).strftime('%Y%m%d')
    result = tq.get_market_data(
        field_list=['Open', 'High', 'Low', 'Close', 'Volume'],
        stock_list=stock_list, start_time=start_time,
        end_time=TARGET_END, dividend_type='front', period='1d', fill_data=True
    )
    if isinstance(result, dict):
        dfs = []
        for field in ['Open', 'High', 'Low', 'Close', 'Volume']:
            df_field = tq.price_df(result, field, column_names=stock_list)
            df_long = df_field.stack().reset_index()
            df_long.columns = ['date', 'code', field]
            dfs.append(df_long.set_index(['date', 'code']))
        df = pd.concat(dfs, axis=1).reset_index()
        return df
    return result


df = None
data_source = ""

# 优先用本地数据（数据全、速度快），TdxQuant 作为备选
try:
    df = load_local_data()
    data_source = "本地 parquet"
    print(f"  [OK] 本地数据加载成功")
except Exception as e:
    print(f"  本地数据: {e}")
    print(f"  切换到 TdxQuant...")
    try:
        df = fetch_data_tdx()
        data_source = "TdxQuant API"
        print(f"  [OK] TdxQuant 数据获取成功")
    except Exception as e2:
        print(f"  TdxQuant 也失败: {e2}")
        sys.exit(1)

if df is None or df.empty:
    print("无数据")
    sys.exit(1)

codes = sorted(df['code'].unique())
n_stocks = len(codes)
print(f"  数据源: {data_source}  |  记录: {len(df)}  |  股票: {n_stocks} 只")

# ── 4. 转为宽表 ──────────────────────────────────────
print("\n[2/4] 计算信号...")

df['date'] = pd.to_datetime(df['date'])

def to_wide(df, field):
    return df.pivot_table(index='date', columns='code', values=field, aggfunc='first').sort_index()

close_df = to_wide(df, 'Close')
open_df  = to_wide(df, 'Open')
high_df  = to_wide(df, 'High')
low_df   = to_wide(df, 'Low')
vol_df   = to_wide(df, 'Volume')

# 截取回测区间
close_df = close_df.loc[TARGET_START:TARGET_END]
open_df  = open_df.loc[TARGET_START:TARGET_END]
high_df  = high_df.loc[TARGET_START:TARGET_END]
low_df   = low_df.loc[TARGET_START:TARGET_END]
vol_df   = vol_df.loc[TARGET_START:TARGET_END]

print(f"  区间: {close_df.index[0].date()} ~ {close_df.index[-1].date()}  |  {len(close_df)} 个交易日")

# ── 5. 信号生成（与 screener 改进版完全一致）───────
# 均线
ma5  = close_df.rolling(5).mean()
ma20 = close_df.rolling(20).mean()
ma60 = close_df.rolling(60).mean()

# X1: MA5 5日变化率
x1 = (ma5 - ma5.shift(5)) / ma5.shift(5) * 100
# X2: X1 的 5日均线
x2 = x1.rolling(5).mean()

# 金叉
cross_up = (x1 > x2) & (x1.shift(1) <= x2.shift(1))
# 角度条件
cond_angle = cross_up & (x1 > x1.shift(1))

# 量条件
avg_vol_20 = vol_df.shift(1).rolling(20).mean()
cond_vol = vol_df > avg_vol_20 * VOL_THRESHOLD

# 收盘位置
close_pos = (close_df - low_df) / (high_df - low_df)
cond_close = close_pos > CLOSE_POS_THRESHOLD

# 价格结构
rh20 = high_df.shift(1).rolling(20).max()
rl20 = low_df.shift(1).rolling(20).min()
rm20 = (rh20 + rl20) / 2
cond_price = (close_df > ma20) & (close_df > rm20) & (ma60 >= ma60.shift(10))

# 综合信号 ZA
za = cond_angle & cond_price & cond_vol & cond_close

# 20天首次
za_int = za.astype(int)
count_20 = za_int.rolling(20, min_periods=1).sum()
buy = za & (count_20 == 1)
buy_signal = buy & (~buy.shift(1).fillna(False))

# 出场：X1下穿X2（动量衰竭）
cross_down = (x1 < x2) & (x1.shift(1) >= x2.shift(1))
# 合并：动量衰竭 或 跌破MA20 或 持仓超20天无新信号
exit_signal = cross_down | (close_df < ma20)

# 信号后移1日（次日开盘成交）
entries = buy_signal.shift(1).fillna(False).astype(bool)
exits   = exit_signal.shift(1).fillna(False).astype(bool)

# 避免出入同一天（entry优先）
exits = exits & ~entries

n_entry = entries.sum().sum()
n_exit  = exits.sum().sum()
print(f"  买入信号: {n_entry}  |  卖出信号: {n_exit}")

# 调试：逐条件打印
print(f"  [调试] 各条件触发次数:")
print(f"    cross_up:          {cross_up.sum().sum()}")
print(f"    cond_angle:        {cond_angle.sum().sum()}")
print(f"    cond_vol:          {cond_vol.sum().sum()}")
print(f"    cond_close:        {cond_close.sum().sum()}")
print(f"    cond_price:        {cond_price.sum().sum()}")
print(f"    za (四条件交集):    {za.sum().sum()}")
print(f"    buy (20日首信号):  {buy.sum().sum()}")
print(f"    buy_signal:        {buy_signal.sum().sum()}")

# 显示几个买入信号的日期和股票
if n_entry > 0:
    entry_rows, entry_cols = np.where(entries.values)
    print(f"\n  买入信号明细 (前10条):")
    for i, (r, c) in enumerate(zip(entry_rows, entry_cols)):
        if i >= 10: break
        print(f"    {entries.index[r].date()}  {entries.columns[c]}")

if n_entry == 0:
    print("\n  [WARN] 无买入信号，尝试放宽条件...")
    # 放宽：去掉价格结构中的 MA60 条件
    cond_price_relaxed = (close_df > ma20) & (close_df > rm20)
    za2 = cond_angle & cond_price_relaxed & cond_vol
    za2_int = za2.astype(int)
    count2 = za2_int.rolling(20, min_periods=1).sum()
    buy2 = za2 & (count2 == 1)
    buy_signal2 = buy2 & (~buy2.shift(1).fillna(False))
    entries = buy_signal2.shift(1).fillna(False).astype(bool)
    exits   = exit_signal.shift(1).fillna(False).astype(bool)
    exits   = exits & ~entries
    n_entry = entries.sum().sum()
    print(f"  放宽后买入信号: {n_entry}")
    if n_entry == 0:
        print("  仍无信号，退出")
        sys.exit(0)

# ── 6. VectorBT 回测 ─────────────────────────────────
print(f"\n[3/4] 执行 VectorBT 回测...")

import vectorbt as vbt

portfolio = vbt.Portfolio.from_signals(
    close=close_df,
    entries=entries,
    exits=exits,
    price=open_df,
    init_cash=INIT_CASH,
    fees=FEES,
    slippage=0.001,
    freq='D',
)

# ── 7. 结果 ──────────────────────────────────────────
print(f"\n[4/4] 回测结果")
print("=" * 70)

stats = portfolio.stats()

key_metrics = [
    'Start Value', 'End Value',
    'Total Return [%]',
    'Benchmark Return [%]',
    'Max Drawdown [%]',
    'Sharpe Ratio',
    'Calmar Ratio',
    'Win Rate [%]',
    'Expectancy',
    'Total Trades',
    'Total Closed Trades',
    'Profit Factor',
    'Total Fees Paid',
]

# 交易明细
trades = portfolio.trades.records_readable
n_trades = len(trades)

for m in key_metrics:
    if m in stats.index:
        val = stats[m]
        if m in ('Total Trades', 'Total Closed Trades'):
            print(f"  {m:<25s}: {n_trades:>12d}")
        elif isinstance(val, float):
            if np.isnan(val):
                print(f"  {m:<25s}: {'N/A':>12s}")
            else:
                print(f"  {m:<25s}: {val:>12.4f}")
        else:
            print(f"  {m:<25s}: {str(val):>12s}")

# 手工计算胜率
if n_trades > 0:
    wins = (trades['PnL'] > 0).sum()
    win_rate = wins / n_trades * 100
    total_pnl = trades['PnL'].sum()
    avg_win = trades[trades['PnL'] > 0]['PnL'].mean() if wins > 0 else 0
    avg_loss = trades[trades['PnL'] < 0]['PnL'].mean() if wins < n_trades else 0
    print(f"  {'胜率 (实际)':<25s}: {win_rate:>11.1f}%")
    print(f"  {'盈利笔数':<25s}: {wins:>12d}")
    print(f"  {'亏损笔数':<25s}: {n_trades - wins:>12d}")
    print(f"  {'总盈亏':<25s}: {total_pnl:>12.2f}")
    print(f"  {'平均盈利':<25s}: {avg_win:>12.2f}")
    print(f"  {'平均亏损':<25s}: {avg_loss:>12.2f}")

print("-" * 70)

if n_trades > 0:
    print(f"\n交易明细 ({n_trades} 笔):")
    cols = ['Entry Idx', 'Exit Idx', 'Entry Price', 'Exit Price', 'PnL', 'Return', 'Direction']
    avail = [c for c in cols if c in trades.columns]
    print(trades[avail].to_string())
else:
    print("\n无完成交易")

# 总结
print("\n" + "=" * 70)
print(f"策略: MA5 角度突破（改进版）")
print(f"数据源: {data_source}  |  股票池: {n_stocks} 只")
print(f"区间: {TARGET_START} ~ {TARGET_END}")
print(f"入场信号: {n_entry} 次  |  完成交易: {n_trades} 笔")
print(f"初始: {INIT_CASH:,.0f}  |  最终: {stats.get('End Value', 0):,.2f}")
if n_trades > 0:
    print(f"胜率: {win_rate:.1f}%  |  总盈亏: {total_pnl:,.0f}")
print("=" * 70)
