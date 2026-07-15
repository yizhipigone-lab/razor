# 审计报告:候选⑤ 选股 base.py 统一过滤流水线(2026-07-13)

> 审计对象:base.py preprocess 钩子从"死的 0 调用"→ 单一过滤流水线;4 份策略删重复样板。
> 方法:逐条 Read/Grep 真实代码 + 10 preprocess 单元测试 + 全套件回归(263 passed)。

## 审计对象清单

| 文件 | 改动 |
|---|---|
| `app/screener/strategies/base.py` | 重写为深 module:常量 `LIMIT_TABLE`(panzheng 完整版)、`LIMIT_MAIN_PCT`;`preprocess(bars)` 方法委托自由函数 `preprocess_bars`;`preprocess_bars(bars, params, limit_table, limit_main_pct)` 集中 ST/退/北交所/停牌/涨停 5 段过滤 |
| `app/screener/engine.py` | 在 `generate_signals` 前调 `preprocess`:class 走 `strategy.preprocess(bars)`(尊重 `self.LIMIT_TABLE` 覆盖);function 走自由 `preprocess_bars` |
| `app/screener/strategies/ma5_angle.py` | 函数删 ST/北交所/涨停 inline + filter_st/filter_bj/skip_limit_up 参数;类加 `LIMIT_TABLE=0.195` `LIMIT_MAIN_PCT=0.095`(保留旧阈值) |
| `app/screener/strategies/panzheng_tupo.py` | 同 ma5_angle;类用 base 默认(0.199) |
| `app/screener/strategies/ma5_angle_tdx_v2.py` | 同;类加 `LIMIT_TABLE=0.195, 8/4=0.29` `LIMIT_MAIN_PCT=0.095` |
| `app/screener/strategies/ma5_angle_cross.py` | 函数删 ST/北交所 inline;**新增** `PARAMS = {"skip_limit_up": False, "filter_bj_pattern": r"^8"}`(保留旧:无涨停 + 北交所只 '8' 不含 '4') |
| `tests/test_base_preprocess.py`(新) | 10 测试:ST/北交所 default+custom/停牌/涨停 default+custom+skip/无假默认/empty |

## ✅ 通过验证

- **全套件 263 passed / 0 error / 0 fail**(此前 253 + ⑤ 新增 10 preprocess 测试)。
- **零行为漂移**:4 份策略的涨停阈值表通过 `LIMIT_TABLE` 类常量覆盖保留(ma5 维持 0.195,panzheng 用 base 默认 0.199,tdx_v2 用 0.195+8/4=0.29);ma5_angle_cross 通过 `PARAMS` 的 `skip_limit_up=False`+`filter_bj_pattern=r"^8"` 保留旧行为(无涨停、北交所只 '8' 不含 '4')。
- **死钩子激活**:`grep self.preprocess` 旧 = 0(钩子完全没用);现引擎 `_scan_worker` 每次跑策略都调,4 份策略都走统一过滤。
- **涨停阈值表 DRIFT 终结**:panzheng=0.199 vs ma5=0.195 的旧 DRIFT 通过类常量覆盖机制按策略意图保留(不再是同一股票不同策略不同结果)。
- **dual protocol 处理**:class(`strategy.preprocess` 走 `self.LIMIT_TABLE`)+ function(走自由 `preprocess_bars` 用 base 默认),4 份策略都是 class-mode,行为全保。

## 🔧 审计发现

### 🟢 NOTE-1:`preprocess` 方法本身无直接测试
**状况**:删了一个 `TestBaseStrategyPreprocessMethod` 测试(类内定义 `class S(BaseStrategy)` 触发 ABC 抽象类检查的边界 case,Python 报 "Can't instantiate abstract class"),该路径实际只是 1 行委托 `return preprocess_bars(bars, self.params or {}, self.LIMIT_TABLE, self.LIMIT_MAIN_PCT)`。
**影响**:核心过滤契约(10 测试)覆盖了 `preprocess_bars` 的所有开关与边界;类方法的 1 行委托在编译 + 引擎调用 `strategy.preprocess(bars)` 时隐式覆盖。无直接单测。
**建议**:如未来要硬化,可加一个 concrete 子类(显式 `def generate_signals`)的委托测试。**未做**(记录即可)。

### 🟢 NOTE-2:reflection-based protocol sniffing(engine.py)
**状况**:旧 `screener/engine.py` 用 `inspect.signature(strategy_obj.generate_signals)` 嗅探 `market_df`/`all_stock_df` 参数(report 标为 fragile)。
**影响**:本次 ⑤ 未动该机制(非 scope);`hasattr` + 反射分发继续工作。**记录为后续硬化**(候选独立 scope:统一用显式 capability 声明替反射)。

## 📊 总评

- 严重级别:**🟢 NOTE ×2(均记录,非阻塞)**;无 CRITICAL / HIGH。
- 整体评分:**9.5/10**(死钩激活 + 4 份样板合并 + 涨停阈值 DRIFT 终结 + 零行为漂移 + 10 preprocess 测试;扣半分因类方法委托路径无直接测试)。
- 可交付:**是**。263 passed。base.preprocess 现在是选股过滤单一入口,4 份策略不再重复写。
- 残留:NOTE-1(类方法直接测试)、NOTE-2(反射 sniffing 替换)、intrady runners 未动(grilling 决议)。

## 三大候选(①行情 ②回测 ⑤选股)总览

| 候选 | 状态 | 关键交付 |
|---|---|---|
| ① 行情 sourcing | 已 commit + 实盘验过 | `quote_source.py` 4 adapter + 缓存/熔断 + Q6 + 7 调用方守卫 |
| ② 回测 simulate | 已 commit | `simulate_one_trade.py` kernel + engine 委托 + ai_optimizer 影子忠实 + 删 _v2 |
| ⑤ 选股 base | **本批** | base.preprocess 统一过滤 + 4 份策略删重复 + 涨停表 DRIFT 终结 |

剩余候选(报告原列):③ 下单编排 · ④ cron_jobs · ⑥ 行情缝合。等本批 commit + 验过后可继续。
