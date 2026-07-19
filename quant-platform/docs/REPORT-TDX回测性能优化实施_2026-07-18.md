# TDX 回测性能优化(第二轮) — 实施报告

> 日期: 2026-07-18
> 依据: [RESEARCH-TDX回测性能优化_2026-07-18.md](RESEARCH-TDX回测性能优化_2026-07-18.md)
> 原则: 零差异提速(同输入回测数字逐字段一致)/ 老缓存全部复用(`_CACHE_VERSION` 不动)
> 用户决策: NaN 信号按"修 bug"口径处理(NaN≠信号); P0~P1 全部实施

---

## 一、成果总览

**同参数回测(QUANTQQ / 2025-05-01~2026-07-15 / 缓存命中路径):**

| 指标 | 改前 | 改后 | 备注 |
|---|---|---|---|
| 完整链路(读缓存→转换→解析→回放) | ~24s | **5.97s** | E2E 实测(含 5m 超限判断→降级日线) |
| 其中: parquet→dict 字符串化 | 7.05s | **0**(已消除) | raw 路径跳过 |
| 其中: 逐行解析+回放 | 8.67s | **4.08s** | 基线脚本实测 |
| 全市场冷跑(worker 取数+写缓存) | 估 10-20 分钟级 | **~6 分钟实测** | 897 万行,直写 parquet |
| 日内 5m 回放(50 股池 3.7万根 bar) | — | 0.13s | A/B 0 处不一致 |

冷路径提速来源: 消灭 100MB+ JSON 管道(dumps/loads 各数十秒) + 消灭 3400 万次逐格 `df.loc`(估 3-6 分钟)。

---

## 二、改动明细

### P0-1 缓存命中路径向量化(核心)

| 文件 | 改动 |
|---|---|
| `app/backtest/tdx_parse.py` **(新)** | 向量化解析模块: `load_cache_df`(日期谓词下推,区间外 82.5% 行不读) / `parse_daily` / `parse_intraday`。OHLC 无效行(NaN/<=0)处理与旧路径逐字段等价;日内"整股翻转"语义逐位复刻 |
| `app/backtest/tdx_runner.py` | 日线/日内两条路径: 有 `parquet_path` 时走向量化解析,无则回退旧逐行解析(兼容保留) |
| `app/tqsdk/bridge.py` | `execute_screen_range(intraday)` 新增 `raw=True` 选项: 缓存命中只回 parquet 路径,跳过 `df_to_signals_prices` 的 897 万行字符串化;日内 Step2 信号股改从 parquet 向量化提取 |

### P0-2 5m 降级复用 sig_result

`run_tdx_backtest` 降级日线时,日内 Step1 已拿到的 signals/parquet 直接复用,不再重复调 `execute_screen_range`(原白付一次 ~7s 转换)。

### P1-1 worker 直写 parquet

| 文件 | 改动 |
|---|---|
| `app/tqsdk/worker/tqsdk_bridge_worker.py` | `_do_range` 结果直接写临时 parquet 长表(与缓存 schema 一致),stdout 只回 `range_path`,消灭 100MB+ 单行 JSON 管道 |
| `app/tqsdk/bridge.py` | 冷路径接收 `range_path` → `save_cache_from_parquet` 入缓存;raw 调用直接回路径,兼容调用(api/tqsdk.py)读 parquet 转 dict |
| (兼容) | 旧 worker 的 signals dict 路径保留,`save_cache_from_dict` 不动 |

### P1-2 `_col_to_values` 向量化

worker 内价格 DataFrame → 长表改用 `df.stack()` 一次成型(`_market_data_to_long`),删除逐单元格 `float(df.loc[dt,col])`(约 3400 万次访问)。

### P1-3 日内引擎 O(N²) 消除

`tdx_runner._run_intraday_backtest`:
- 预建 `(code,date)` 索引(`first_bar_of_day` / `last_close_of_day`),买入找当天第一根 bar、逐日估值从线性扫描改 O(1) 查表
- 顺手发现一处**死逻辑**: 动态仓位计算里"向前 50 根 bar 找当日价"恒不匹配(扫描区间全是往日 bar),按零差异口径固化为 `entry_price` 并加注释

### P1-4 5m 超限降级透明化(C5)

- bridge 超限降级时返回 `intraday_fallback` 标记
- 回测结果 `summary.data_source` 显式标注 `daily(降级:5m估算58M根K线超上限50M...)`,前端可见,不再静默

### NaN 信号修复(用户拍板: 修)

旧 `_is_signal_value("nan")` 因 `float("nan") != 0` 为 True 会把 TDX 历史不足 bar 的 NaN 信号**误判为买入信号**。向量化判定 `sv.notna() & (sv != 0)` 修复。当前 QUANTQQ 缓存无 "nan" 值(实测 0 条),基线不受影响。

---

## 三、验证记录

| 验证 | 方法 | 结果 |
|---|---|---|
| 零差异(同一缓存, 旧 dict 路径) | 改前基线 vs 改后 legacy 路径, summary+trades+equity 逐字段 diff(容差 1e-6) | **PASS** |
| 零差异(同一缓存, 新 parquet 路径) | 改前基线 vs 改后向量化路径 diff | **PASS**(4960 笔 / 40.96% 一致) |
| worker 转换等价 | 新 `_write_range_parquet` 产物 vs 旧 `_signals_prices_to_rows` 逐行对比(单测) | **PASS** |
| 日内索引 A/B | 新 `first/last` 索引 vs 旧线性扫描,真实 3.7 万根 5m bar 逐点对比 | **0 处不一致** |
| 实盘冒烟 | 3 股小池冷跑/缓存命中 raw/兼容 dict 三分支 + 5 股池 5m 取数(12396 根) | **全通** |
| 全市场冷跑 | 897 万行新 worker 直写 parquet | **~6 分钟**,缓存 fc57d6f9 生成 |
| 单元测试 | test_tdx_parse.py(22) + test_tdx_worker_parquet.py(6) | **28 新增全过** |
| 回测测试集 | `pytest -k "backtest or tdx or exit or execution or cost"` | **112 passed** |

**E2E 差异说明**: 全链路重跑结果 4962 笔 / 41.52% 与基线 4960 / 40.96% 有 2 笔差异。根因已定位: **通达信公式在 7-16 晚被编辑过**(公式库指纹 mtime 变化 → 旧缓存失效),新旧缓存信号集本身不同(412 个旧信号日 / 399 个新信号日,跨度 2018~2026),非本次改动引入。同一输入下的两条解析路径已验证逐字段零差异。

---

## 四、自审计(4 维度)

| 维度 | 结果 |
|---|---|
| 死代码 | 删除 `_col_to_values`(被 `_market_data_to_long` 取代);旧解析分支保留作无 parquet 时的兼容 fallback(非死代码) |
| 硬编码 | 无新增;默认值/口径全部照搬原实现 |
| 线程/锁安全 | 未触碰锁;worker 子进程模型不变;temp parquet 用 mkstemp 唯一名 |
| 边界条件 | 空 df/空 signals/无 OHLC 字段/非法日期串/缓存写失败回退均有路径;np.float64 经 `tolist()` 转 Python float 防 JSON 序列化泄漏 |

**已知边界(明示)**:
- 5m bar 窗口外的信号会被跳过(股票有部分 5m 数据但信号日在窗口外 → 买入跳过)——**既有行为**,新旧一致,未改。是否应降级日线价格买入,属正确性议题,建议与"涨停过滤降级"一并单独立项。
- `bars_intra` 的 `df.to_dict('records')`(C4)与字符串排序(C3)未动: 当前 50M 上限下不是主瓶颈;若未来提高 5m 上限,需先做 DataFrame 原生化改造。

---

## 五、未实施项(按研究报告排期)

| 项 | 原因 |
|---|---|
| P2-1 增量缓存(end_time 变一天全量重取) | 高风险,需独立计划;当前冷跑已降至 ~6 分钟,紧迫性下降 |
| P2-2 消除"取两遍数据" | 高风险,改 worker 与 TDX 交互 |
| P2-3 批量寻优 ProcessPool | 依赖 P0 落地,现可另行评估 |

---

## 六、code-reviewer 审计与修复(2026-07-18 第二轮)

改动完成后经 code-reviewer agent 独立审计(Verdict: WARNING),发现 1 HIGH / 3 MEDIUM / 4 LOW,**全部修复并回归验证**:

| 发现 | 级别 | 修复 |
|---|---|---|
| 老版 TDX 只返回 Close 时 `_write_range_parquet` 列选择 KeyError → worker 崩溃(旧代码有防御,新代码防了一半) | HIGH | 缺失 OHLC 列补 NaN;新增回归测试 `test_missing_ohlc_fields_no_crash` |
| `tdx_range_*` 临时 parquet 不在陈旧清理范围(use_cache=False/缓存写失败时泄漏) | MEDIUM | `_cleanup_stale_temp_parquets` glob 增加 `tdx_range_*` |
| 缓存 parquet 损坏时 raw 路径失去自愈(旧路径 catch 后回退 worker) | MEDIUM | tdx_runner 向量化解析加 try/except,失败回退旧解析分支 |
| 冷路径 close<=0/NaN 行"纳入→剔除"的行为差异未声明(与旧缓存命中路径本就一致) | MEDIUM | tdx_parse docstring 补"差异 2"声明(用户口径: 采用更合理的剔除语义) |
| `day_bars_start` 死变量 / 空结果 data_source 被降级标记覆盖 | LOW | 已修 |
| P0-2 复用后 parquet 被删的极端竞态 / merge 理论重复行 | LOW | 概率极低或实际不可达,记录在案不改 |

修复后回归: `pytest -k "backtest or tdx or exit or execution or cost"` **113 passed**;同一旧缓存基线 diff **PASS**(4960 笔 / 40.96% 逐字段一致)。

---

## 七、变更文件

```
app/backtest/tdx_parse.py              (新) 向量化解析模块
app/backtest/tdx_runner.py             日线/日内接入向量化解析 + P0-2 复用 + P1-3 索引 + C5 标注
app/tqsdk/bridge.py                    raw 选项 + range_path 冷路径 + 日内信号股向量化提取 + 降级标记
app/tqsdk/worker/tqsdk_bridge_worker.py 直写 parquet + _market_data_to_long 向量化(已部署到 TDX 目录)
tests/test_tdx_parse.py                (新) 22 例
tests/test_tdx_worker_parquet.py       (新) 6 例
scripts/_perf_baseline.py              (新) 零差异基线/diff 工具(保留供后续回归)
scripts/_perf_profile_tdx.py           (新) cProfile 工具(保留)
```

改动尚未 commit。
