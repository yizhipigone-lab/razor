# 批 1 地基优化 Spec

> 日期:2026-06-25
> 作者:Claude
> 项目:quant-platform 全面优化(3 批拆分)
> 批 1 范围:4 项地基优化,P0/P1 风险,5 个 commit
> 状态:待用户 review,批准后写 plan,执行
> 上批成果:CHANGELOG-2026-06-25.md / OPUS/Quant-Platform-全局复盘报告.md

---

## 0. 上下文与硬约束

### 0.1 上一批(2026-06-25)做了什么
- TDX worker 改造 + tdx_runner 解析真实 OHLC + open 字段修复
- 回测数字: 405%(虚高) → 144.62%(更接近实盘)

### 0.2 复盘报告发现(本批要修的)
1. B. 参数真相源:同一止损参数 4 套默认值散落(config.py / app_setting.json / settings.py / engine.py 假默认)
2. C. AI 优化目标函数:`_calmar_score` 名为 Calmar 实为 np.mean(pnls),WFO 不参与选优,全样本最优直接 apply
3. D. strategy_coder:LLM 生成代码无沙箱,直接 exec,安全风险
4. E. 工程卫生:无 .gitignore,日志/.dmp 入库

### 0.3 硬约束(继承自项目 + 用户原话)
- 批间必须 merge 才能继续(用户原话:"冻结 11 项涉及文件")
- 修改代码不能破坏原有功能(用户记忆 feedback_safe_modification.md)
- config.py 唯一真相源(用户记忆 feedback_config_flow.md)
- 后端写完用 code-reviewer(用户记忆 feedback_use_skills.md)
- 所有沟通和思考使用中文(用户记忆 feedback_language.md)

### 0.4 批 1 范围外(明确不做)
- 批 2 的成交执行层、hold_days、event_engine 等
- 批 3 的 pytest、AI 样本外、模拟盘对齐
- 实盘网关(用户原话:"除了实盘部分,其他全部进行优化")

---

## 1. 目标

5 个 commit 内完成 4 项地基优化,每个 commit 独立可验证、可回滚、互不破坏功能。

---

## 2. 设计

### 2.1 项 B 真相源:删所有假默认值,建立 config.py → settings → 引擎 单一链路

**当前问题**(已确认事实):
- `app/backtest/engine.py:538-545,777-783`:`_p('hard_stop_loss_pct', -7.0)`、`trailing_activate_pct=15.0` 等字面默认值
- `core/settings.py:105,110,115,125,130,135` property 默认值与 config.py 矛盾
- `app/api/backtest.py:262` AI 基线 -6.0

**目标**:
- 删除所有下游"假默认值"(用户记忆铁律)
- 新建 `app/config/schema.py` 定义统一参数 schema(必填项,无默认)
- `app/sim_trader/config.py` 作为唯一真相源,启动时校验
- settings.py 改为"读取 + 缺键报错"而非"读 + 假默认"

**改动设计**:

```python
# 新建 app/config/schema.py
class RiskSchema:
    """风控参数 schema,无默认值,缺键即报错"""
    hard_stop: float
    trail_activate: float
    trail_dd: float
    time_exit_days: int
    time_exit_profit: float
    time_force_days: int
    first_day_exit_min_profit: float
    first_day_exit_days: int
    take_profit_tiers: list
    breakeven_threshold: float = 0.0
    breakeven_stop: float = 0.0
    use_atr_trail: bool = False
    # ...

def load_risk_params() -> RiskSchema:
    """从 app/sim_trader/config.py 加载 + 校验"""
    import app.sim_trader.config as sc
    return RiskSchema(
        hard_stop=sc.HARD_STOP,  # 必须存在,无兜底
        ...
    )
```

**改动文件清单**:
- 新建 `app/config/schema.py`
- 改 `app/backtest/engine.py:538-545,777-783` 删字面默认值,改用 `load_risk_params()`
- 改 `core/settings.py:95-135` property 删 default,改为读取 + 校验
- 改 `app/api/backtest.py:262` 删 -6.0 默认值

**风险**:
- 配置缺失 → 直接报错(不是静默兜底),可能影响未配置用户
- 缓解:`app/sim_trader/config.py` 必须有所有字段(现有 config.py 已经定义)

### 2.2 项 E .gitignore + 清理入库

**当前问题**:
- 无 .gitignore
- `server.log` (1.3MB)、`server_stdout.log`、`*.dmp` 崩溃转储、`logs/*.log` 40+ 文件被 git 跟踪
- `.env` 当前未跟踪但无保护

**目标**:
- 新建 .gitignore
- 清理已入库的违规文件(`git rm --cached`)

**改动设计**:
```gitignore
# .gitignore 新建
venv313/
__pycache__/
*.pyc
*.pyo
*.pyd
.pytest_cache/

# 日志和崩溃
logs/
*.log
server.log
server_stdout.log
*.dmp

# 数据和输出
data/
output/
output/**
!output/.gitkeep

# 环境和配置
.env
.env.local
.env.*.local

# 临时
scratch/
*.tmp
*.parquet.tmp

# 杂项
.DS_Store
Thumbs.db
__pycache__/
*.egg-info/
dist/
build/
.idea/
.vscode/
*.swp
```

**清理命令**:
```bash
# 取消跟踪但不删除本地文件
git rm --cached -r logs/
git rm --cached server.log server_stdout.log 2>/dev/null
git rm --cached *.dmp 2>/dev/null
```

**风险**:
- 低。`.gitignore` 是 .gitignore,清理是 `rm --cached` 不删本地
- 但要小心:`git rm --cached` 是 prunable 操作,需要用户授权

### 2.3 项 C AI 目标函数 + WFE 选优

**当前问题**:
- `app/backtest/ai_optimizer.py:72-78` `_calmar_score` 名为 Calmar 实为 `np.mean(pnls)`,无风险惩罚
- `ai_optimizer.py:932` 按**全样本** score 排序选 best,WFO 只贴标签不参与
- `ai_optimizer.py:152` LHS 无随机种子,结果不可复现

**目标**:
- 真正实现风险调整收益(Sharpe 或 Calmar)
- WFE(样本外衰减)进入 best_params 排序
- LHS 固定随机种子

**改动设计**:

```python
# app/backtest/ai_optimizer.py 改动

# 1. 修复 _calmar_score:改用真实 Sharpe(年化收益/波动率)
def _calmar_score(trades: list) -> float:
    if len(trades) < 8:
        return -999.0
    pnls = np.array([t["pnl_pct"] for t in trades])
    if len(pnls) < 2:
        return float(np.mean(pnls))
    mean = np.mean(pnls)
    std = np.std(pnls, ddof=1)
    if std < 1e-6:
        return mean
    # 真实 Sharpe(年化):mean/std * sqrt(252),但交易频率不确定,保留原始 std
    # 改用 simpler: 风险调整 = mean - 0.5 * std (penalty 系数可调)
    penalty = 0.5 * std
    score = mean - penalty
    return float(score)

# 2. WFE 进入排序:用 (score, wfe) 联合排序
# 3. LHS 固定种子
np.random.seed(42)
```

**改动文件**:
- 改 `app/backtest/ai_optimizer.py:72-78` _calmar_score
- 改 `ai_optimizer.py:932-947` 排序逻辑
- 改 `ai_optimizer.py:152` 加 seed

**风险**:
- 中。优化目标变了,旧 best_params 失效,但之前本来就有过拟合
- 需要重新跑 1-2 个真实回测对比修复前后数字

### 2.4 项 D strategy_coder AST 沙箱

**当前问题**:
- `app/backtest/strategy_coder.py:46-53` LLM 生成代码无沙箱,直接 exec
- 任何 `os.system('rm -rf /')` 都会被执行

**目标**:
- AST 静态分析,白名单允许的节点类型
- 拒绝 `Import/Call` 涉及危险模块的节点

**改动设计**:

```python
# 新建 app/utils/ast_sandbox.py
import ast

ALLOWED_NODES = (
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
    ast.Return, ast.Yield, ast.If, ast.For, ast.While,
    ast.Assign, ast.AugAssign, ast.Expr, ast.Constant,
    ast.Name, ast.Load, ast.Store, ast.arg, ast.arguments,
    ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare,
    ast.List, ast.Tuple, ast.Dict, ast.Set,
    ast.Subscript, ast.Index, ast.Slice,
    ast.Attribute,  # 需要白名单属性
    ast.Lambda,
)

FORBIDDEN_NAMES = {
    'os', 'sys', 'subprocess', 'shutil', 'socket', 'http',
    'urllib', 'requests', 'ftplib', 'smtplib',
    '__import__', 'eval', 'exec', 'compile',
}

def validate_strategy_code(code: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    for node in ast.walk(tree):
        # 检查 import
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                root = alias.name.split('.')[0]
                if root in FORBIDDEN_NAMES:
                    return False, f"禁止导入模块: {alias.name}"
        # 检查函数调用
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_NAMES:
                    return False, f"禁止调用: {node.func.id}"
    return True, "OK"
```

**改动文件**:
- 新建 `app/utils/ast_sandbox.py`
- 改 `app/backtest/strategy_coder.py:46-53` 加载前先 validate
- 测试:恶意 prompt 注入 `os.system` 应被拒绝

**风险**:
- 低。白名单可能漏过某些隐式调用(如 `__class__`),后续可加 sandbox.exec

---

## 3. 文件改动清单(批 1 全部)

| 文件 | 操作 |
|---|---|
| `app/config/schema.py` | 新建 |
| `app/backtest/engine.py` | 改 538-545, 777-783(删默认值,改用 schema) |
| `core/settings.py` | 改 95-135(删 property 默认值) |
| `app/api/backtest.py` | 改 262(删 -6.0 默认) |
| `.gitignore` | 新建 |
| `app/backtest/ai_optimizer.py` | 改 72-78(真 Calmar), 152(随机种子), 932-947(WFE 排序) |
| `app/utils/ast_sandbox.py` | 新建 |
| `app/backtest/strategy_coder.py` | 改 46-53(加载前 validate) |

---

## 4. 5 个 commit 划分

```
C1-1: 新建 app/config/schema.py,改 app/backtest/engine.py 删假默认值
C1-2: 改 core/settings.py 删 property 默认值,改 app/api/backtest.py 删 -6.0
C1-3: 新建 .gitignore,git rm --cached 清理入库文件
C1-4: 改 app/backtest/ai_optimizer.py 真 Calmar + 随机种子 + WFE
C1-5: 新建 app/utils/ast_sandbox.py,改 strategy_coder 加 validate
```

每个 commit 独立可测,互不破坏功能(逐文件测试)。

---

## 5. 验证清单(每个 commit 必跑)

- [ ] `python scripts/test_fix_02.py` 等所有 test_fix_*.py 通过
- [ ] `python scripts/test_simple_runner.py` 通过
- [ ] 启动服务,跑 1 次回测,数字合理(不报 KeyError)
- [ ] 配置缺失时,启动报错(不是静默兜底)
- [ ] `.gitignore` 生效:touch logs/test.log 后 git status 不显示
- [ ] AI 优化器:同一参数两次跑结果一致(seed 固定)
- [ ] strategy_coder:恶意 prompt 注入 os.system 被拒绝
- [ ] 0 报错 0 崩溃(用户硬约束)

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 删默认值后,未配置用户报错 | config.py 已经有所有字段,不会触发 |
| 改名 / 删函数影响其他调用方 | 全部 grep 调用方,确保新签名兼容 |
| AST 沙箱白名单太严,正常策略被拒 | 测试集覆盖 3-5 个正常策略,确保通过 |
| AI 优化目标改变,旧 best_params 失效 | 接受(之前就是过拟合) |

---

## 7. 范围外(批 2/3)

批 2(引擎统一):成交执行层 / 净值口径 / hold_days / event_engine / DuckDB
批 3(测试/AI/一致性):pytest / AI 样本外 / 模拟盘对齐

---

## 8. 状态

待用户 review。
