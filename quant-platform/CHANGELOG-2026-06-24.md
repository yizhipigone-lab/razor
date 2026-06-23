# 2026-06-24 Bug 修复 CHANGELOG

> **修复内容**:PY/TDX 回测引擎的日线路径把 bar.low 替换为 bar.close,导致"盘中跌 5% 但日线 close 没跌 5%"的票**漏触发 HS**。

## 1. 问题

**根因**(已读源码核实):

| 路径 | 文件 | bar["low"] 来源 | 等价于 |
|---|---|---|---|
| `simple_runner.run_backtest`(line 695) | [app/backtest/simple_runner.py:695](app/backtest/simple_runner.py#L695) | `closes[d].get(code, 0)` | **close** |
| `simple_runner.run_backtest` 无 5min 数据(line 359) | [app/backtest/simple_runner.py:359](app/backtest/simple_runner.py#L359) | `closes[d].get(code, 0)` | **close** |
| `tdx_runner._run_daily_backtest`(line 608) | [app/backtest/tdx_runner.py:608](app/backtest/tdx_runner.py#L608) | 没设 low 字段,build_context fallback 到 close | **close** |
| `tdx_runner._check_stops_daily` daily fallback(line 164) | [app/backtest/tdx_runner.py:164](app/backtest/tdx_runner.py#L164) | 显式 `low: close_p` | **close** |
| `strict_runner.run_strict`(line 263) | [app/backtest/strict_runner.py:263](app/backtest/strict_runner.py#L263) | `closes[d].get(code, 0)` | **close** |

**`rule_hard_stop` 注释明确说"用 Low 检测"**(line 88),**但调用方把 low 字段替换成 close**。

## 2. 修复

按"修 4 个文件(全面)"方案,所有 daily 路径都从 parquet 补真实 low:

| 文件 | 改动 |
|---|---|
| `app/backtest/simple_runner.py` | line 293-296 加载时补 `lows` 字典;line 359, 695 用真实 low |
| `app/backtest/tdx_runner.py` | line 224-242 解析价格时从 parquet 补 low(用 low_cache 避免重复读);line 594-617 同样;line 164, 444 `_check_stops_daily` 接受 `low_p` 参数 |
| `app/backtest/strict_runner.py`(未在 git 跟踪) | 内部加 `_get_low()` 从 parquet 读 |

## 3. 修复前后对比(2026-01-01 ~ 2026-06-22,strategy=QUANTQQ,daily)

| 指标 | 修复前 | 修复后 | 差异 |
|---|---|---|---|
| **总收益** | +405.49% | **+72.36%** | **-333%** |
| **最大回撤** | 1.55% | **6.48%** | +4.93% |
| **胜率** | 82.6% | **53.8%** | -28.8% |
| **年化收益** | 3994% | 248% | -3746% |
| Sharpe | 18.79 | 5.35 | -13.44 |
| 最终资金 | 5,054,937 | 1,723,590 | -3,331,347 |
| **HS 触发数** | 51 | **167** | **+116 (3.3x)** |
| TR 触发数 | 195 | 146 | -49 |
| TP1 触发数 | 65 | 43 | -22 |
| TC 触发数 | 15 | 18 | +3 |
| FE 触发数 | 12 | 12 | 0 |
| TF 触发数 | 2 | 10 | +8 |

## 4. 关键结论

**修复前**:
- 405% 收益是"漏触发止损"导致的**虚高**
- 51 笔 HS 全是"全天封跌停"的票(close 恰好等于 0.95 × entry)
- "盘中跳水但尾盘拉回"的票全部漏触发(回测显示盈利,实际应止损)

**修复后**:
- 72% 收益是**接近实盘的真实数字**
- 167 笔 HS 中包含"盘中跳水"和"全天封跌停"两类
- max_drawdown 6.48% — 真实的回撤风险

**与实盘的对应**:
- 修复后 6.48% 回撤 vs 同期指数最大回撤 ~10%(创业板 -5% / HS300 -8% — 需要进一步对比)
- 修复后胜率 53.8% — 与之前 4.5 年回测(2022-2026)的 54.5% 胜率非常接近
- 修复后年化 248% — 仍高于指数,但在合理范围(短线策略 + 满仓轮动)

## 5. 已知限制

1. **strict_runner.py 不在 git 跟踪中**(`?? app/backtest/strict_runner.py`),改动不入版本控制
   - 文件本身存在,修复有效
   - 但 git status 显示 untracked,需要单独处理(可能上批漏了 `git add`)

2. **回测性能**:从 parquet 读 low 增加 I/O,回测时间增加约 1.5x(原本 5 分钟 → 7-8 分钟)

3. **未来回测**:建议默认 `intraday_freq=5m`,这样有真实 5min OHLC,不再需要 daily fallback 路径

## 6. 相关文件

- **修复前基线**: [output/backtest_results/before_low_fix/](output/backtest_results/before_low_fix/)
- **修复后结果**: [output/backtest_results/bt_20260624_011206_after_low_fix.json](output/backtest_results/bt_20260624_011206_after_low_fix.json)
- **Git commit**: `1a82e50 fix(backtest): use real parquet low for daily stop-loss`
- **网络问题**: `git push origin master` 失败(github.com:443 无法连接),需要您网络恢复后手动 push

## 7. 后续建议

1. **网络恢复后推送 commit**: `git push origin master`
2. **用户决策**: 修复后 72% 收益(年化 248%)是否需要继续调整策略参数
3. **strict_runner.py 入版本控制**: `git add -f app/backtest/strict_runner.py` 然后单独 commit
4. **后续修复**: HTTP GET / hang(base 已知问题)、test_determinism.py GBK 编码
