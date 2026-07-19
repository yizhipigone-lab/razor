# TDX 回测性能优化 — 研究报告

> 日期: 2026-07-18
> 范围: 仅 TDX 通达信公式回测(用户指定),含缓存命中路径 + worker 冷路径 + 日内(5m)路径
> 方法: 真实缓存数据(897万行)cProfile 实测 + 向量化原型验证 + worker 静态分析
> 前置工作: 2026-07-16 已完成引擎层零差异优化(1.33x),本报告聚焦**下一轮**更大的结构性瓶颈

---

## 一、核心结论(先说人话)

**回测慢,慢的不是"回测"本身。**

实测一份典型回测(QUANTQQ / 2025-05-01~2026-07-15 / 日线 / 4960 笔交易)的缓存命中路径:

| 阶段 | 耗时 | 占比 | 干的是什么 |
|---|---|---|---|
| 读缓存 parquet | 0.86s | 4% | 磁盘 → DataFrame |
| **parquet → Python dict(字符串化)** | **7.05s** | **29%** | 把 897 万个浮点数逐个转成**字符串** |
| **dict → 回测可用的价格表(逐行解析)** | **~13.6s** | **57%** | 把字符串再逐个 `float()` 解析回来 |
| **真正的回测引擎(买卖/止损/净值)** | **~0.7s** | **3%** | 逐日回放 |
| 构建结果/指标 | 0.09s | <1% | summary/sharpe 等 |
| **合计** | **~24s** | | |

**真正的回测计算只占 3%。剩下 97% 是"数据格式来回翻译"**:parquet 里本来就是浮点数,先转成字符串字典(`df_to_signals_prices`),再在 `tdx_runner` 里逐行解析回浮点数和日期。897 万行 × 两遍纯 Python 循环 = 20 秒白干。

向量化原型实测(同样输入、同样输出结构):

| 阶段 | 现状 | 向量化原型 | 倍数 |
|---|---|---|---|
| 读 parquet(带日期谓词下推) | 0.86s | 0.30s | 2.9x |
| 信号解析 | ~7s(含在 seg2) | 0.41s | — |
| 价格解析 | ~13.6s | 0.63s | — |
| **解析段合计** | **~21.5s** | **~1.3s** | **~16x** |

**整条缓存命中路径预计 ~24s → ~2.5s(约 10 倍)**,且可做到回测数字零差异(方法见第五节)。

---

## 二、测量方法(可复现)

```bash
python scripts/_perf_profile_tdx.py   # cProfile 跑缓存命中路径(读 parquet→转 dict→日线回放)
```

- 输入: `output/tdx_cache/af3909ac6571e7ce.parquet`(8973024 行 / 5421 只 / 2014-03~2026-07)
- profile 结果: `_run_daily_backtest` 函数体自身 tottime = **13.59s**(即函数体内的两个解析循环),引擎调用(`sell_phase` 0.19s / `buy` 0.03s / `sell` 0.03s / `check_stops` 0.15s / `build_context` 0.05s)合计 <1s
- 结果锚点: trades=4960 / total_return=40.96%,与 7-16 报告基线一致

---

## 三、热点清单(按链路位置)

### A. 缓存命中路径(每次重复回测都走)——实测

| # | 位置 | 问题 | 实测开销 |
|---|---|---|---|
| A1 | [result_cache.py:234-262](../app/tqsdk/result_cache.py#L234-L262) `df_to_signals_prices` | DataFrame → 嵌套 dict,每个浮点经 `_float_to_str` 转**字符串**,897万行纯 Python 循环 | 7.05s |
| A2 | [tdx_runner.py:660-682](../app/backtest/tdx_runner.py#L660-L682) 信号解析循环 | 897万行逐行:字符串切日期、`date(int(...))`、`_is_signal_value` 字符串解析 | ~7s(估,含在函数体 13.6s 内) |
| A3 | [tdx_runner.py:710-754](../app/backtest/tdx_runner.py#L710-L754) 价格解析循环 | 逐行 `float()` + try/except + 逐行 dict 构造 | ~6s(估) |
| A4 | [bridge.py:201-202](../app/tqsdk/bridge.py#L201-L202) | 读全量 897 万行再过滤;区间外(2014~2025-04)实测占 82.5%(897万 vs 区间内 157万),全白读 | 0.56s 可省 |

注: A2+A3 的精确拆分被 cProfile 归并到 `_run_daily_backtest` 函数体(13.59s tottime),二者合计即该值减去少量循环控制开销。

### B. worker 冷路径(首次/换公式/换区间时走)——静态分析

| # | 位置 | 问题 | 预估开销 |
|---|---|---|---|
| B1 | [tqsdk_bridge_worker.py:371-385](../app/tqsdk/worker/tqsdk_bridge_worker.py#L371-L385) `_col_to_values` | **逐单元格 `float(df.loc[dt, col])`**:QUANTQQ 实测信号股 5134 只 × ~1655天 × 4字段 ≈ **3400万次 .loc 访问**(.loc 是 pandas 最慢访问方式,~5-10µs/次;量级随公式信号密度变化,稀疏公式相应减小) | **估 3-6 分钟** |
| B2 | worker → 主进程 stdout | 897万行 signals dict 压成**单行 JSON**(100MB+,见 bridge.py:137 注释)经管道传输,worker 端 `json.dumps` + 父进程 `json.loads`,还有 120s join 超时风险 | 估 30-90s |
| B3 | worker [tqsdk_bridge_worker.py:288](../app/tqsdk/worker/tqsdk_bridge_worker.py#L288) + [:348](../app/tqsdk/worker/tqsdk_bridge_worker.py#L348) "取两遍数据" | Step1 `formula_process_mul_xg` 扫全市场算公式,Step2 又 `get_market_data` 对信号股重取一遍 OHLC(7-16 报告已立项) | TDX 侧,估分钟级 |
| B4 | [result_cache.py:74-81](../app/tqsdk/result_cache.py#L74-L81) 缓存 key 含 `end_time` | **end_date 变一天 → 897万行全量重取**。用户每天改日期跑最新数据,缓存形同虚设 | 冷路径全成本 |
| B5 | [tqsdk_bridge_worker.py:459-477](../app/tqsdk/worker/tqsdk_bridge_worker.py#L459-L477) `_do_fetch_intraday` 双重 `.loc` 循环 | 同 B1,日内 K 线逐单元格取值 | 5m 数据量大时分钟级 |

### C. 日内(5m)回放路径——静态分析

| # | 位置 | 问题 |
|---|---|---|
| C1 | [tdx_runner.py:572-573](../app/backtest/tdx_runner.py#L572-L573) | 每天每持仓 `next(b for b in reversed(bars_intra[:bar_idx]))` 向前扫 K 线找当日价 → **O(天数×持仓×K线数)**,百万级 bars 时是平方级灾难 |
| C2 | [tdx_runner.py:415-417](../app/backtest/tdx_runner.py#L415-L417) | 每个买入信号 `next(b for b in bars_intra[bar_idx:])` 线性扫描 |
| C3 | [tdx_runner.py:342-349](../app/backtest/tdx_runner.py#L342-L349) | 百万级 bars 按**字符串** sort + 逐 bar `date.fromisoformat` |
| C4 | [bridge.py:332](../app/tqsdk/bridge.py#L332) | `df.to_dict(orient='records')` 把百万行 DataFrame 拆成百万个 dict,慢且吃内存 |
| C5 | [bridge.py:262,311-313](../app/tqsdk/bridge.py#L262-L313) | `MAX_5M_BARS=5000万` 上限。估算基数是**区间内信号股**(bridge.py:309),非全市场:QUANTQQ 实测区间信号股 3924 只 × 308 天 × 48 根 ≈ **5800万根 → 超限降级日线**。信号稀疏的公式不一定超限,但 QUANTQQ 这类 dense 公式下,用户以为在用 5m 精度,实际跑的是日线(有 log.warning 但无 UI 透传) |
| C6 | [tdx_runner.py:241-329](../app/backtest/tdx_runner.py#L241-L329) | **日内路径有自己的一份信号/价格解析循环**,与日线同样逐行 `float()`/切日期字符串。**默认精度就是 5m**(tdx_runner.py:101),即用户默认走的正是这条路径——向量化红利必须同时落到日内解析,否则默认路径只吃到一半收益 |
| C7 | [tdx_runner.py:277,305-322](../app/backtest/tdx_runner.py#L277-L322) | **日内路径仍存 7-16 已修过的两个老问题**:① `low_cache` 仍是 per-(date,code) 粒度,同一只股每个新日期重读整个 parquet(O(N²) 读盘风暴,日线已在 :692-708 修为整股映射);② `has_ohlc=False` 整股永久翻转(:305),一行脏数据把整只股票永久打到 parquet fallback(日线已修为逐行判断) |
| C8 | [bridge.py:268-273](../app/tqsdk/bridge.py#L268-L273) + [tdx_runner.py:169-184](../app/backtest/tdx_runner.py#L169-L184) | **5m 超限降级时白付两次转换**:`execute_screen_range_intraday` Step1 已 cache 命中并跑过一次 `df_to_signals_prices`(~7s);超限降级后 tdx_runner 丢掉已有 sig_result,再调 `execute_screen_range` 又命中又转一次。降级场景白付 ~7s |

### D. 批量/寻优场景

| # | 位置 | 问题 |
|---|---|---|
| D1 | [api/backtest.py:746](../app/api/backtest.py#L746) | 回测跑在**线程**里(`run_in_thread`),CPU 密集 + GIL → 多回测并行无收益。参数寻优(78 公式批量)如需并行要走 ProcessPool |
| D2 | — | 引擎本身已 <1s,**不值得**做 numba/改写引擎等微优化(投入大收益小) |

---

## 四、优化方案(按收益/风险分级)

### P0 — 高收益、可零差异、建议立即做

**P0-1 缓存命中路径向量化(预期 ~24s → ~2.5s,约 10x)**

- 杀掉"parquet → 字符串 dict → 解析回 float"的来回翻译:`df_to_signals_prices` 改为直接把 **DataFrame** 传给回测层(或新写向量化解析函数),信号/价格解析改用 `pd.to_numeric` + `groupby` 一次成型
- **日线和日内两条路径的解析循环都要改**(C6: 默认精度 5m 走日内路径,只改日线等于默认路径只吃一半收益)
- `read_parquet` 加日期谓词下推(`filters=[('date','>=',...),('date','<=',...)]`),区间外 82.5% 的行不读
- 原型已验证: 解析段 21.5s → 1.3s
- **风险: 中**。触及解析语义,必须零差异验证(见第五节,含审计发现的两个等价性陷阱)。`df_to_signals_prices` 生产调用方仅 bridge.py:202 一处,爆炸半径小
- 不改缓存文件格式、不改 `_CACHE_VERSION`、不碰 worker → **老缓存全部复用**

**P0-2 5m 降级时复用已有 sig_result(C8)**

- `execute_screen_range_intraday` 超限降级时,Step1 的 signals/prices 已在手,直接返回走日线,不要再调一次 `execute_screen_range`
- 降级场景省 ~7s;**风险: 低**(纯复用,不改数据)

### P1 — 高收益、中风险、第二批

**P1-1 worker 直写 parquet,消灭 100MB+ JSON 管道(B2)**
- range 任务复用 intraday 已验证的 `bars_path` 模式:worker 把 signals/prices 直接写临时 parquet,stdout 只回路径
- 顺带消掉 `save_cache_from_dict` 的 dict→rows 二次转换
- **风险: 中**。改 worker + bridge 协议,需一次性重建缓存(`_CACHE_VERSION` 升 v3)

**P1-2 `_col_to_values` 向量化(B1,估省 3-6 分钟/次冷跑)**
- `float(df.loc[dt,col])` 逐格 → `df[col].tolist()` / numpy 批转,逻辑等价
- **风险: 低**(纯 worker 内部,输出格式不变)。可与 P1-1 同批做

**P1-3 日内路径 O(N²) 扫描消除 + 7-16 遗留模式修复(C1/C2/C3/C7)**
- bars 预分组: `{code: [(date, o,h,l,c), ...]}`(numpy 数组),买入/估值改 O(1) 查表;"当日价"用逐日前进指针代替向前扫描
- 顺带把 7-16 在日线路径修过的两个模式补到日内路径(C7): `low_cache` 改整股映射、`has_ohlc` 改逐行判断
- **风险: 中高**。日内撮合语义敏感(买入价=当天第一根 bar close),需逐字段 diff 验证
- 前置依赖 C5 决策(见下)

**P1-4 C5 决策: 5m 路径上限**
- 要么把 `MAX_5M_BARS` 上限提高/改为分块处理,要么在 UI 明确提示"区间过大已降级日线"
- **这是正确性/透明度问题,不是性能问题**,但不解决它,P1-3 优化的是一条实际跑不到的路径

### P2 — 高收益、高风险、需独立计划

**P2-1 增量缓存(B4)**
- 缓存 key 去掉 `end_time`,按 (公式+指纹) 存全量历史;请求新区间时只对**缺失的尾部日期**调 worker 补数、append 进缓存
- 用户每天跑"截至今天"从"全量重取 897 万行"变成"只取 1 天"
- **风险: 高**。缓存 key 重设计 + 一次性重建 + 并发写保护;需独立计划与充分测试(7-16 报告已标记)

**P2-2 消除"取两遍数据"(B3)**
- range 扫描阶段让 worker 顺带输出信号股的 OHLC(或缩小 `get_market_data` 的 count 到实际所需)
- **风险: 高**。改 worker 与 TDX API 交互,需实测 TDX 行为

**P2-3 批量寻优 ProcessPool 并行(D1)**
- 78 公式批量场景:数据获取已被缓存覆盖后,单公式回放 ~2.5s,ProcessPool 8  worker 可再砍到 1/6 墙钟
- **风险: 中**。DuckDB/缓存文件并发写需串行化;收益依赖 P0-1 先落地

### 不建议做

- ❌ 引擎微优化(numba/重写 `sell_phase` 等): 引擎只占 3%,优化 50% 也只省 0.35s
- ❌ 换语言/重写回测引擎: 同上,病根不在引擎
- ❌ 盲目上多进程跑单次回测: 解析向量化后单进程已够快

---

## 五、零差异验证方法(沿用 7-16 流程)

1. 改前存基线: 同参数跑 summary + trades + equity 三件套落盘
2. 每项改动后重跑,逐字段 diff(float 容差 1e-6),任一差异即回退
3. 等价性论证要点:
   - **信号值(含审计发现的陷阱)**: `"0"/"0.0"/""/None/非法串` → 非信号;其余 → 信号。`pd.to_numeric(errors='coerce')` 对这些输入产出 NaN→fillna(0)→False,非法串原实现 `float()` 抛异常→False,逐项等价;"` 1 `"(带空格)两者都解析为 1 ✓
   - **⚠️ 陷阱 1 — `"nan"` 信号串(code-reviewer 审计发现)**: 原实现 `float("nan") != 0.0` 得 **True**(nan 比较语义)→ 被当成信号;而 `to_numeric("nan")` 解析成功得 NaN → fillna(0) → False。**两者判定相反**。该输入真实可达: TDX 对历史不足的 bar 会返回 NaN,worker `str(Value)` 后就是 `"nan"` 存进缓存。这同时暴露**现有路径的潜在正确性 bug**(NaN 信号值今天会被当成买入信号)。**这是一个"逐位复制 bug 还是顺手修 bug"的决策点,实施 P0-1 前需用户拍板**;建议向量化写法 `v.notna() & (v != 0)`(即修 bug 口径),并在报告/ changelog 里显式记录行为变化
   - **⚠️ 陷阱 2 — 价格 NaN vs `"0"` sentinel(审计发现)**: `_float_to_str` 把 NaN/<=0 归一成 `"0"`,旧路径拿到 `0.0`;向量化直接读 parquet 拿到 **NaN**。分歧点: tdx_runner.py:428 `if px <= 0: continue` 对 `0.0` 跳过、对 `NaN` **不跳过**(NaN 比较恒 False)→ 可能用 NaN 价格买入。向量化实现必须补 `.fillna(0)` + 非正截零才能零差异
   - **价格 float 精度**: 对有限正浮点,`str(float)` 是 repr 往返无损,直接用 float 与原路径 float(str(v)) 结果逐位一致
   - **日期过滤**: 谓词下推的边界与解析循环的 `start <= dt_date <= end` 一致(cache date 列为 8 位定长 YYYYMMDD 字符串,字典序=日期序 ✓;非法日期串向量化 parse 后 drop NaT 即等价)
4. 回测测试集: `pytest -k "backtest or tdx or exit or execution or cost"`(7-16 审计验证为 84 个)

---

## 六、实施顺序建议

| 批次 | 内容 | 预期累计效果(缓存命中路径) |
|---|---|---|
| 第 1 批 | P0-1 向量化解析 + 谓词下推(日线+日内两条路径) + P0-2 降级复用 | ~24s → ~2.5s(10x);降级场景再省 ~7s |
| 第 2 批 | P1-1 worker 直写 parquet + P1-2 `_col_to_values` 向量化 | 冷路径估省 4-8 分钟;需重建缓存一次 |
| 第 3 批 | C5 决策 + P1-3 日内 O(N²) 消除 | 5m 路径从"实际不可用"变为"可用且快" |
| 第 4 批(独立计划) | P2-1 增量缓存 / P2-2 取数合并 / P2-3 批量并行 | 日常重复回测接近零等待 |

每批独立交付、独立零差异验证,任何一批出问题不影响已落地批次。

---

## 七、审计与迭代记录(计划书审计流程)

初稿完成后经 code-reviewer agent 独立审计(Verdict: WARNING),发现 3 HIGH / 3 MEDIUM / 3 LOW,已全部处理:

| 发现 | 级别 | 处理 |
|---|---|---|
| `"nan"` 信号串与向量化判定相反,且暴露现有路径把 NaN 当信号的潜在 bug | HIGH | 已写入第五节陷阱 1,标记为实施前需用户拍板的决策点 |
| 价格 NaN/`"0"` sentinel 被忽略,`px <= 0` 检查对 NaN 行为分歧 | HIGH | 已写入第五节陷阱 2,向量化实现必须 fillna(0)+截零 |
| C5/B1 算术误用全市场股票数,应用区间信号股数 | HIGH | 已用真实缓存数据重算(3924 只 → 5800万根,QUANTQQ 仍超限;注明公式依赖性) |
| 日内路径解析循环未纳入 P0-1(默认精度 5m 走日内) | MEDIUM | P0-1 已改为覆盖日线+日内两条路径 |
| 日内 low_cache 读盘风暴 + has_ohlc 整股翻转(7-16 已修日线、日内未修) | MEDIUM | 已补入热点 C7 与 P1-3 范围 |
| 5m 降级时 sig_result 被丢弃、双重转换白付 ~7s | MEDIUM | 已补入热点 C8,新增 P0-2 |
| 引用错位 2 处(B3/D1)、"静默降级"措辞、83% 数字 | LOW | 已修正引用与措辞,82.5% 经实测确认 |

---

## 八、遗留问题提醒(非本次性能范围,已在册)

- TDX 日线涨停过滤大面积降级(`prev_close=None`,7-16 报告第七节)— **正确性问题,建议优先于本报告 P1 批次处理**
- parquet low 的 `YYYY-MM-DD` vs `YYYYMMDD` 格式不匹配致 fallback 实质失效(7-16 审计 MEDIUM-2)
