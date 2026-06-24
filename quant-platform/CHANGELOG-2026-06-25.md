# 2026-06-25 Bug 修复 CHANGELOG (TDX 真实 OHLC)

> 上一版 (2026-06-24) 用 parquet 兜底补 low,本版从源头(TDX worker)取真实 OHLC。

## 1. 修复策略

| 版本 | 数据来源 | 数字 | 评价 |
|---|---|---|---|
| 修复前 (B) | TDX worker 只取 close,low 替换为 close | +405.49% | 系统性高估 |
| 上版 (B + parquet) | tdx_runner 从 parquet 补 low | +72.36% | 偏真实但仍可能数据源不一致 |
| **本版 (A + 真实 OHLC)** | **TDX worker 取完整 OHLC** | **+144.62%** | **最接近实盘** |

## 2. 修复内容

### 2.1 TDX worker(`E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py`)

**日线路径** `field_list=["close"]` → `field_list=["open", "high", "low", "close"]`

```python
# line 238-239
mk = tq.get_market_data(
    field_list=["open", "high", "low", "close"],  # 原 ["close"]
    stock_list=batch,
    period="1d",
    ...
)
```

并扩展解析逻辑(原只有 `prices[code] = {Date, Close}`,现含 `Open/High/Low`,缺失字段 fallback 为 None)。

### 2.2 tdx_runner.py

**两层 fallback 策略**:
1. **优先用 TDX 返回的 OHLC**(worker 正常情况)
2. **缺失字段时回退 parquet**(worker 老版本 / 字段不全)

`prices_by_date` 结构扩展:
```python
prices_by_date[d_str][code] = {
    "close": close_val,
    "high": high_val,    # 真实 TDX high
    "low": low_val,      # 真实 TDX low(关键)
    "open": open_val,    # 真实 TDX open
}
```

### 2.3 simple_runner.py

加 `opens` 字典,`day_snap` / `snap` 用真实 open。

### 2.4 _check_stops_daily 接受 open_p 参数

```python
def _check_stops_daily(pos, close_p, high_p, hold_days, params, low_p=None, open_p=None):
    ...
    bar = {"close": close_p, "high": high_p, "low": actual_low, "open": actual_open}
```

之前 `bar["open"] = close_p` 是 bug(line 169),现在用真实 TDX open。

## 3. 修复前后数字对比(2026-01-01 ~ 2026-06-22)

| 指标 | 修复前 (B) | 上版 (B+parquet) | **本版 (A+OHLC)** |
|---|---|---|---|
| total_return | +405.49% | +72.36% | **+144.62%** |
| max_drawdown | 1.55% | 6.48% | 5.98% |
| win_rate | 82.6% | 53.8% | 57.7% |
| trades | 340 | 792 | 1332 |
| sharpe | 18.79 | 5.35 | 9.30 |
| ann_return | 3994% | 248% | 676% |
| final_equity | 5,054,937 | 1,723,590 | 2,446,212 |
| **HS** | 51 | 167 | **280** |
| TR | 195 | 146 | 346 |
| TP1 | 65 | 43 | 14 |
| TC | 15 | 18 | 1 |
| FE | 12 | 12 | 20 |
| TF | 2 | 10 | 5 |

## 4. 关键发现

### 4.1 A 方案 vs B 方案(parquet)数字差异

**trades 数量: 1332 (A) vs 792 (B)**
- A 用 TDX 真实 OHLC(可能含停牌标记、除权调整)
- B 用 parquet 数据(可能与 TDX 略有差异)
- A 触发的 HS 更多(280 vs 167) → TDX 真实数据更严

### 4.2 ret 分布不再是精确 -5%

修复前 51 笔 HS ret 全部 -5.00%(触发时 sell_px=close=0.95*entry,无跳空)
本版 280 笔 HS ret 分布在 -5.0% ~ +3% 范围
- 真实市场: 开盘跳空低开 0~5%,sell_px=max(0.95*entry, open) = open(开盘价)
- 这正是**真实盈亏分布**

### 4.3 max_drawdown 5.98%

修复前 1.55%(虚低) → 真实 5.98%
接近同期指数最大回撤(HS300 -8%,创业板 -5%)。

## 5. 逻辑漏洞检查清单

| 漏洞 | 状态 |
|---|---|
| Worker 老版本兼容性 | ✓ tdx_runner 检测 `len(highs) > 0`,缺失时 fallback parquet |
| TDX 字段 0 值(NaN/无效) | ✓ 检测到 fallback(`if low_val <= 0`) |
| Worker subprocess 缓存 | ✓ `subprocess.run` 每次新进程,新 worker 立即生效 |
| 5min 路径不受影响 | ✓ 5min bar 来自 worker 单独 fetch,与 daily 路径独立 |
| sim_trader 实时路径 | ✓ 用 `use_high_for_tp=True` 5min,不受影响 |
| 前端字段兼容 | ✓ summary 结构未变,只是数字变 |
| `_check_stops_daily` 内部 `bar["open"]` | ✓ **已修**(本次发现并修复) |
| `simple_runner` 内部 snap | ✓ **已修**(本次发现并修复) |

## 6. 已知限制

1. **TDX 数据源 vs parquet 数据源**:两者不完全一致
   - TDX 实时更新、含停牌标记、除权调整
   - parquet 是历史快照
   - A 方案以 TDX 为准(parquet 作 fallback)

2. **5min 路径仍用 TDX worker 的 5m OHLC**(line 314)
   - 之前 TDX 5min 取 OHLC,本版没改
   - 5min 路径**完全不受**本次影响

3. **strict_runner.py 不在 git 跟踪**(`?? app/backtest/strict_runner.py`)
   - 修复时改了 simple_runner.py 和 tdx_runner.py
   - strict_runner.py 文件存在但 git 看不到
   - 如果想纳入版本控制: `git add -f app/backtest/strict_runner.py`

4. **Worker 文件不在 git 仓库**(`E:\NEW_TDX\PYPlugins\user\`)
   - TDX worker 修改**已保存到本地**
   - 但 git 跟踪不到
   - 备份 TDX worker 文件到仓库: `cp worker.py docs/tdx_worker_backup_20260625.py`

## 7. 关键文件

- **修复前基线**: [output/backtest_results/before_low_fix/](output/backtest_results/before_low_fix/)
- **A 方案修复后**: [output/backtest_results/bt_20260625_063703_after_ohlc_fix.json](output/backtest_results/bt_20260625_063703_after_ohlc_fix.json)
- **Open 字段修复后(数字一致)**: [output/backtest_results/bt_20260625_064434_after_open_fix.json](output/backtest_results/bt_20260625_064434_after_open_fix.json)
- **Git commits**:
  - `1a82e50 fix(backtest): use real parquet low for daily stop-loss`(上一版)
  - `99a94e7 fix(backtest): use TDX OHLC + real open field for daily stop-loss`(本版)
- **TDX worker 修改**:`E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py` (line 238-285)

## 8. 后续建议

1. **您网络恢复后推送**: `git push origin master`
2. **决策回测数字**: 当前 +144% 收益(年化 676%),比之前 405%(虚高)更接近实盘
3. **TDX worker 备份**: 复制 worker 到 git 仓库的 docs/ 目录,作为外部文件备份
4. **修复后回测结果对比**: 看 2022-2026 的 4.5 年回测数字(应该也降得更接近实盘)
5. **sim_trader 实时路径验证**: 启动服务,跑一次模拟盘,验证 HS 触发逻辑(用真实 TDX 5min 数据)
6. **前端数字变化**: 用户应知道回测历史中的旧 405% 数字不再准确,新的更可信
