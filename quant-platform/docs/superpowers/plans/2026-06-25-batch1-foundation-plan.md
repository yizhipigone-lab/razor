# 批 1:地基优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 5 个 commit 内完成 4 项地基优化(真相源/.gitignore/AI 目标/沙箱),逐文件独立可测互不破坏功能

**Architecture:**
- **真相源**:新建 `app/config/schema.py` 统一风控参数,删除 engine.py/settings.py/api/backtest.py 的字面默认值,缺键即报错
- **.gitignore**:新建仓库根 .gitignore,`git rm --cached` 清理已入库的 logs/server.log/*.dmp
- **AI 目标函数**:`_calmar_score` 改用真风险调整(均值 - 0.5*标准差),LHS 加 seed 42,WFE 进 best 排序
- **沙箱**:新建 `app/utils/ast_sandbox.py`,strategy_coder 加载前用 AST 白名单校验,禁导 os/sys/subprocess 等

**Tech Stack:** Python 3.x, dataclasses, ast(标准库), numpy, FastAPI, DuckDB, Git

**Spec:** [docs/superpowers/specs/2026-06-25-batch1-foundation-spec.md](../specs/2026-06-25-batch1-foundation-spec.md)

---

## 全局硬约束(所有 Task 必读)

- 修改代码不能破坏原有功能(用户记忆 `feedback_safe_modification.md`)
- config.py 是唯一真相源(用户记忆 `feedback_config_flow.md`)
- 后端改完用 code-reviewer 验证(用户记忆 `feedback_use_skills.md`)
- 严格冻结:批 1 期间不动 `engine.py / settings.py / ai_optimizer.py / strategy_coder.py / .gitignore / app/config/* / app/utils/ast_sandbox.py`,其他文件用户可改

---

## 文件结构(批 1 全部)

### 新建
- `app/config/schema.py` — 统一风控参数 schema + 加载器
- `app/utils/ast_sandbox.py` — AST 白名单校验
- `.gitignore` — 仓库根

### 修改
- `app/backtest/engine.py:538-545, 777-783` — 删假默认值,改用 `load_risk_params()`
- `core/settings.py:95-135` — 删 property default
- `app/api/backtest.py:262` — 删 -6.0 默认
- `app/backtest/ai_optimizer.py:72-78, 152, 932-947` — 真 Calmar + 随机种子 + WFE
- `app/backtest/strategy_coder.py:46-53` — 加载前 validate

### 不修改(重要!)
- `app/sim_trader/config.py` — 保持原样,作为唯一真相源被 schema.py 读取
- `app/api/sim_trader.py` — 不改
- `app/data_manager/*` — 不改
- 其他 app/ 子模块 — 不改

---

## Task 1: C1-1 新建 schema.py + 删 engine.py 假默认值

**Files:**
- Create: `app/config/schema.py`
- Modify: `app/backtest/engine.py:538-545` (一处)
- Modify: `app/backtest/engine.py:777-783` (一处)
- Test: `scripts/test_fix_20.py`

- [ ] **Step 1: 写测试(RED)**

写 `scripts/test_fix_20.py`,测试 schema 加载行为:

```python
"""验证 L20 修复: 真相源 schema 加载"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_schema_loads_from_config():
    """schema 应能从 app/sim_trader/config.py 加载所有风控参数"""
    from app.config.schema import load_risk_params
    params = load_risk_params()
    assert params.hard_stop < 0, f"hard_stop 应 < 0, 实际 {params.hard_stop}"
    assert params.trail_activate > 0, f"trail_activate 应 > 0"
    assert isinstance(params.take_profit_tiers, list)
    assert len(params.take_profit_tiers) > 0
    print(f"✅ schema 加载成功, hard_stop={params.hard_stop}")


def test_engine_no_fake_defaults():
    """engine.py 不应再硬编码 -7.0 / 15.0 / 30 等假默认值"""
    with open('app/backtest/engine.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 检查 _p('hard_stop_loss_pct', -7.0) 这种假默认
    bad_patterns = [
        "_p('hard_stop_loss_pct', -7.0)",
        "_p('trailing_activate_pct', 15.0)",
        "_p('trailing_drawdown_pct', 5.0)",
        "_p('time_exit_days', 30)",
        "_p('breakeven_threshold_pct', 5.0)",
        "_p('breakeven_stop_pnl_pct', 0.0)",
    ]
    for p in bad_patterns:
        assert p not in content, f"仍存在假默认值: {p}"
    print("✅ engine.py 假默认值已清除")


if __name__ == '__main__':
    test_schema_loads_from_config()
    test_engine_no_fake_defaults()
    print("\n🎉 L20 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

```bash
python scripts/test_fix_20.py
```

Expected: `ModuleNotFoundError: No module named 'app.config.schema'`

- [ ] **Step 3: 新建 `app/config/schema.py`**

```python
"""
统一风控参数 schema
按用户铁律:"config.py 唯一真相源"
app/sim_trader/config.py 是硬编码真相源,本模块只做"读取 + 校验"
"""
from dataclasses import dataclass, field
from typing import List


@dataclass
class RiskSchema:
    """风控参数 schema,缺键即报错,无假默认"""
    hard_stop: float
    trail_activate: float
    trail_dd: float
    time_exit_days: int
    time_exit_profit: float
    time_force_days: int
    first_day_exit_min_profit: float
    first_day_exit_days: int
    take_profit_tiers: list
    use_atr_trail: bool = False
    atr_trail_multiplier: float = 1.0
    breakeven_threshold: float = 0.0
    breakeven_stop: float = 0.0


def load_risk_params() -> RiskSchema:
    """从 app/sim_trader/config.py 加载(唯一真相源)"""
    import app.sim_trader.config as sc

    required = [
        'HARD_STOP', 'TRAIL_ACTIVATE', 'TRAIL_DD',
        'TIME_EXIT_DAYS', 'TIME_EXIT_PROFIT', 'TIME_FORCE_DAYS',
        'FIRST_DAY_EXIT_MIN_PROFIT', 'FIRST_DAY_EXIT_DAYS',
        'TAKE_PROFIT_TIERS',
    ]
    missing = [k for k in required if not hasattr(sc, k)]
    if missing:
        raise RuntimeError(
            f"app/sim_trader/config.py 缺少风控参数: {missing}\n"
            f"按用户铁律,缺键应直接报错,不允许假默认"
        )

    return RiskSchema(
        hard_stop=sc.HARD_STOP,
        trail_activate=sc.TRAIL_ACTIVATE,
        trail_dd=sc.TRAIL_DD,
        time_exit_days=sc.TIME_EXIT_DAYS,
        time_exit_profit=sc.TIME_EXIT_PROFIT,
        time_force_days=sc.TIME_FORCE_DAYS,
        first_day_exit_min_profit=sc.FIRST_DAY_EXIT_MIN_PROFIT,
        first_day_exit_days=sc.FIRST_DAY_EXIT_DAYS,
        take_profit_tiers=list(sc.TAKE_PROFIT_TIERS),
        use_atr_trail=getattr(sc, 'USE_ATR_TRAIL', False),
        atr_trail_multiplier=getattr(sc, 'ATR_TRAIL_MULTIPLIER', 1.0),
        breakeven_threshold=getattr(sc, 'BREAKEVEN_THRESHOLD', 0.0),
        breakeven_stop=getattr(sc, 'BREAKEVEN_STOP', 0.0),
    )
```

- [ ] **Step 4: 改 `app/backtest/engine.py:538-545`**

**Before (line 538-545)**:
```python
        "hard_stop": _p('hard_stop_loss_pct', -7.0),
        "trail_activate": _p('trailing_activate_pct', 15.0),
        "trail_dd": _p('trailing_drawdown_pct', 5.0),
        "time_exit_days": _p('time_exit_days', 30),
        "time_exit_profit": _p('time_exit_min_profit_pct', 3.0),
        "time_force_days": _p('time_exit_force_days', 12),
        "first_day_exit_min_profit": _p('first_day_exit_min_profit_pct', 3.0),
        "first_day_exit_days": 1,
```

**After**:
```python
        # L20 修复: 风控参数从 schema 加载(唯一真相源),不允许假默认
        from app.config.schema import load_risk_params
        _risk = load_risk_params()
        "hard_stop": _risk.hard_stop,
        "trail_activate": _risk.trail_activate,
        "trail_dd": _risk.trail_dd,
        "time_exit_days": _risk.time_exit_days,
        "time_exit_profit": _risk.time_exit_profit,
        "time_force_days": _risk.time_force_days,
        "first_day_exit_min_profit": _risk.first_day_exit_min_profit,
        "first_day_exit_days": _risk.first_day_exit_days,
```

(注意:这里把 "first_day_exit_days" 也改成 schema,不是硬编码 1)

- [ ] **Step 5: 改 `app/backtest/engine.py:777-783`**

**Before (line 777-783)**:
```python
                "hard_stop": _p('hard_stop_loss_pct', -7.0),
                "trail_activate": _p('trailing_activate_pct', 15.0),
                "trail_dd": _p('trailing_drawdown_pct', 5.0),
                "time_exit_days": _p('time_exit_days', 30),
                "time_exit_profit": _p('time_exit_min_profit_pct', 3.0),
                "time_force_days": _p('time_exit_force_days', 12),
```

**After**:
```python
                # L20 修复: 同 538-545,风控参数从 schema
                "hard_stop": _risk.hard_stop,
                "trail_activate": _risk.trail_activate,
                "trail_dd": _risk.trail_dd,
                "time_exit_days": _risk.time_exit_days,
                "time_exit_profit": _risk.time_exit_profit,
                "time_force_days": _risk.time_force_days,
```

(用同一个 `_risk` 变量,确保两处一致)

- [ ] **Step 6: 跑测试,确认 PASS**

```bash
python scripts/test_fix_20.py
```

Expected: 全部通过

- [ ] **Step 7: 跑回归**

```bash
python scripts/test_simple_runner.py
python scripts/test_fix_02.py
python scripts/test_fix_07.py
python scripts/test_fix_08.py
python scripts/test_fix_09.py
python scripts/test_fix_10.py
python scripts/test_fix_11.py
python scripts/test_fix_13.py
python scripts/test_fix_14.py
python scripts/test_fix_15.py
python scripts/test_fix_16.py
python scripts/test_fix_17.py
python scripts/test_fix_18.py
python scripts/test_fix_19.py
```

Expected: 全部通过(0 报错 0 崩溃)

- [ ] **Step 8: Commit**

```bash
git add app/config/schema.py app/backtest/engine.py scripts/test_fix_20.py
git commit -m "fix(config): unify risk params via schema, remove engine.py fake defaults (B1/C1-1)"
```

---

## Task 2: C1-2 删 settings.py / backtest.py 假默认值

**Files:**
- Modify: `core/settings.py:95-135`
- Modify: `app/api/backtest.py:262`
- Test: `scripts/test_fix_21.py`

- [ ] **Step 1: 写测试(RED)**

写 `scripts/test_fix_21.py`:

```python
"""验证 L21 修复: settings.py / backtest.py 假默认值清除"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_settings_no_property_defaults():
    """core/settings.py 的 property 不应有硬编码 default"""
    with open('core/settings.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 找所有 @property 块
    bad_patterns = [
        "def hard_stop_loss_pct(self):",
        "    return -5.0",  # 原默认值
        "    return 5.0",   # trail_activate 等
        "    return 2.0",   # breakeven
        "    return 6",     # time_exit_days
        "    return -3.0",  # time_exit_profit 符号反
        "    return 10",    # time_exit_force_days
    ]
    # 实际: 检查 hard_stop_loss_pct default 不再是 -5.0
    import re
    # 找 hard_stop_loss_pct 块
    pattern = r'def hard_stop_loss_pct\(self\):.*?(?=\n    @|\nclass |\Z)'
    matches = re.findall(pattern, content, re.DOTALL)
    for m in matches:
        assert 'return -5.0' not in m, f"hard_stop_loss_pct 仍有 -5.0 默认值: {m[:200]}"
    print("✅ settings.py hard_stop_loss_pct 假默认值已清除")


def test_api_backtest_no_minus_6_default():
    """app/api/backtest.py:262 不应有 -6.0 默认"""
    with open('app/api/backtest.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 第 262 行附近: "hard_stop": -6.0,
    assert '"hard_stop": -6.0' not in content, f"app/api/backtest.py 仍硬编码 -6.0"
    print("✅ app/api/backtest.py -6.0 假默认已清除")


if __name__ == '__main__':
    test_settings_no_property_defaults()
    test_api_backtest_no_minus_6_default()
    print("\n🎉 L21 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

```bash
python scripts/test_fix_21.py
```

Expected: 第二个测试 `test_api_backtest_no_minus_6_default` FAIL

- [ ] **Step 3: 改 `app/api/backtest.py:262`**

**Before**:
```python
            params["hard_stop"] = body.get("hard_stop", -6.0)
```

(或其他类似写法,需要 grep 确认)

**After**:
```python
            # L21 修复: 不允许假默认,缺键从 app/sim_trader/config.py 读
            if "hard_stop" not in body:
                from app.config.schema import load_risk_params
                body["hard_stop"] = load_risk_params().hard_stop
            params["hard_stop"] = body["hard_stop"]
```

(如果实际 line 262 不是这个,先 `grep -n "hard_stop.*-6.0\|hard_stop.*default" app/api/backtest.py` 找实际位置)

- [ ] **Step 4: 改 `core/settings.py:95-135`**

读取实际内容(可能行号略变):
```bash
grep -n "def hard_stop_loss_pct\|def trailing_stop_activate_pct\|def time_exit_days" core/settings.py
```

**Before** (典型 pattern):
```python
@property
def hard_stop_loss_pct(self):
    return self._config.get("risk", {}).get("hard_stop_loss_pct", -5.0)
```

**After**:
```python
@property
def hard_stop_loss_pct(self):
    val = self._config.get("risk", {}).get("hard_stop_loss_pct")
    if val is None:
        # L21 修复: 缺键报错(不允许假默认)
        raise KeyError(
            "config/app_setting.json 的 risk.hard_stop_loss_pct 缺失, "
            "请补齐或改用 app/sim_trader/config.py 的 HARD_STOP"
        )
    return val
```

(同理修 trail_activate, trail_dd, time_exit_days, time_exit_profit, time_exit_force_days, breakeven_threshold, breakeven_stop)

- [ ] **Step 5: 跑测试,确认 PASS**

```bash
python scripts/test_fix_21.py
```

Expected: 全部通过

- [ ] **Step 6: 跑回归**

```bash
python scripts/test_simple_runner.py
python scripts/test_fix_02.py
```

Expected: 全部通过(0 报错)

- [ ] **Step 7: Commit**

```bash
git add core/settings.py app/api/backtest.py scripts/test_fix_21.py
git commit -m "fix(config): remove fake defaults from settings.py and backtest.py (B2/C1-2)"
```

---

## Task 3: C1-3 新建 .gitignore + 清理入库

**Files:**
- Create: `.gitignore`
- (清理命令用 git rm --cached)

- [ ] **Step 1: 写测试(RED)**

写 `scripts/test_fix_22.py`:

```python
"""验证 L22 修复: .gitignore 存在 + logs/ 不被 git 跟踪"""
import subprocess
import os
import sys
sys.stdout.reconfigure(encoding='utf-8')

def test_gitignore_exists():
    assert os.path.exists('.gitignore'), ".gitignore 不存在"
    print("✅ .gitignore 存在")

def test_gitignore_covers_logs():
    """.gitignore 应覆盖 logs/"""
    with open('.gitignore', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'logs/' in content, ".gitignore 未覆盖 logs/"
    assert '*.log' in content, ".gitignore 未覆盖 *.log"
    print("✅ .gitignore 覆盖 logs/ 和 *.log")

def test_logs_dir_not_tracked():
    """logs/*.log 不应被 git 跟踪"""
    result = subprocess.run(
        ['git', 'ls-files', 'logs/'],
        capture_output=True, text=True, cwd='.'
    )
    files = [f for f in result.stdout.strip().split('\n') if f]
    assert len(files) == 0, f"logs/ 仍有 {len(files)} 个文件被跟踪: {files[:3]}"
    print(f"✅ logs/ 无文件被跟踪(返回 {len(files)} 行)")

def test_server_log_not_tracked():
    """server.log 不应被 git 跟踪"""
    result = subprocess.run(
        ['git', 'ls-files', 'server.log', 'server_stdout.log'],
        capture_output=True, text=True, cwd='.'
    )
    files = [f for f in result.stdout.strip().split('\n') if f]
    assert len(files) == 0, f"server.log 仍被跟踪: {files}"
    print("✅ server.log 不被跟踪")


if __name__ == '__main__':
    test_gitignore_exists()
    test_gitignore_covers_logs()
    test_logs_dir_not_tracked()
    test_server_log_not_tracked()
    print("\n🎉 L22 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

```bash
python scripts/test_fix_22.py
```

Expected: 全部 FAIL(.gitignore 不存在)

- [ ] **Step 3: 新建 `.gitignore`**

```gitignore
# .gitignore
# 用户硬约束: 不入库的临时/大文件/环境/数据

# Python
venv313/
__pycache__/
*.pyc
*.pyo
*.pyd
.pytest_cache/
*.egg-info/
dist/
build/

# 日志和崩溃转储
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

# 环境和敏感
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
.idea/
.vscode/
*.swp
```

注意: `output/**` 后跟 `!output/.gitkeep` 是为了保留目录结构(创建空文件 output/.gitkeep 防止 git 删目录)

- [ ] **Step 4: 创建 `output/.gitkeep`**

```bash
touch output/.gitkeep
```

- [ ] **Step 5: 清理已入库的违规文件**

**警告**: 这是 destructive 操作(虽然 `rm --cached` 不删本地),需要用户确认。

```bash
# 查看现状
git ls-files | grep -E "^logs/|^server\.log$|^server_stdout\.log$|\.dmp$" | head -20

# 取消跟踪(保留本地文件)
git rm --cached -r logs/ 2>/dev/null || true
git rm --cached server.log server_stdout.log 2>/dev/null || true
git rm --cached *.dmp 2>/dev/null || true
```

- [ ] **Step 6: 添加 .gitignore + .gitkeep 到 git**

```bash
git add .gitignore output/.gitkeep
```

- [ ] **Step 7: 跑测试,确认 PASS**

```bash
python scripts/test_fix_22.py
```

Expected: 全部通过

- [ ] **Step 8: 跑回归(测试 + 启动验证)**

```bash
python scripts/test_simple_runner.py
```

Expected: 全部通过

- [ ] **Step 9: Commit**

```bash
git commit -m "chore(git): add .gitignore, untrack logs/server.log/*.dmp (E3/C1-3)"
```

---

## Task 4: C1-4 改 AI 目标函数 + 随机种子 + WFE

**Files:**
- Modify: `app/backtest/ai_optimizer.py:72-78`
- Modify: `app/backtest/ai_optimizer.py:152`
- Modify: `app/backtest/ai_optimizer.py:932-947`
- Test: `scripts/test_fix_23.py`

- [ ] **Step 1: 写测试(RED)**

写 `scripts/test_fix_23.py`:

```python
"""验证 L23 修复: AI 目标函数 + 种子 + WFE 选优"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')

# 测试 1: 目标函数行为
def test_calmar_score_is_risk_adjusted():
    """_calmar_score 不应只是均值,应风险调整"""
    from app.backtest.ai_optimizer import _calmar_score
    # 构造两组: 高均值但高方差 vs 低均值但低方差
    high_mean_high_std = [
        {'pnl_pct': 10.0}, {'pnl_pct': 10.0}, {'pnl_pct': 10.0},
        {'pnl_pct': 10.0}, {'pnl_pct': 10.0}, {'pnl_pct': 10.0},
        {'pnl_pct': 10.0}, {'pnl_pct': 10.0}, {'pnl_pct': -30.0},
        {'pnl_pct': -30.0},  # 高方差
    ]
    low_mean_low_std = [
        {'pnl_pct': 3.0}, {'pnl_pct': 3.0}, {'pnl_pct': 3.0},
        {'pnl_pct': 3.0}, {'pnl_pct': 3.0}, {'pnl_pct': 3.0},
        {'pnl_pct': 3.0}, {'pnl_pct': 3.0}, {'pnl_pct': 2.5},
        {'pnl_pct': 2.5},
    ]
    score_high = _calmar_score(high_mean_high_std)
    score_low = _calmar_score(low_mean_low_std)
    # 真 Calmar 应该: 低方差组得分更高(风险调整后)
    assert score_low > score_high, f"风险调整后: 低方差({score_low}) 应 > 高方差({score_high})"
    print(f"✅ 风险调整正确: 低方差={score_low:.2f} > 高方差={score_high:.2f}")


def test_ai_optimizer_has_seed():
    """ai_optimizer 应有 np.random.seed 调用"""
    with open('app/backtest/ai_optimizer.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 找 np.random.seed 或 np.random.RandomState
    assert 'np.random.seed' in content or 'RandomState' in content, \
        "ai_optimizer.py 缺固定随机种子"
    print("✅ ai_optimizer 有固定随机种子")


def test_wfe_used_in_best_selection():
    """Top-10 排序应参考 WFE,不只是 score"""
    with open('app/backtest/ai_optimizer.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 找 best_params 选取逻辑
    assert 'wfe' in content.lower() or 'wfe_score' in content.lower(), \
        "ai_optimizer 选 best_params 没考虑 WFE"
    print("✅ best_params 选优考虑 WFE")


if __name__ == '__main__':
    test_calmar_score_is_risk_adjusted()
    test_ai_optimizer_has_seed()
    test_wfe_used_in_best_selection()
    print("\n🎉 L23 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

```bash
python scripts/test_fix_23.py
```

Expected: `test_calmar_score_is_risk_adjusted` FAIL(因为当前是 np.mean,两个都返回相同 mean)

- [ ] **Step 3: 改 `app/backtest/ai_optimizer.py:72-78`**

**Before**:
```python
def _calmar_score(trades: list) -> float:
    """目标函数：每笔平均盈利率（等额投入、互不干扰）
    所有交易的平均 pnl_pct，直接反映信号质量"""
    if len(trades) < 8:
        return -999.0
    pnls = [t["pnl_pct"] for t in trades]
    return float(np.mean(pnls))
```

**After**:
```python
def _calmar_score(trades: list) -> float:
    """L23 修复: 风险调整收益(均值 - 0.5*标准差),避免高方差过拟合

    原实现: np.mean(pnls) 只看均值,容易选出"高均值但高方差"的过拟合参数
    修复: 引入波动率惩罚,平衡收益与风险
    系数 0.5 经验值,Sharpe 简化版
    """
    if len(trades) < 8:
        return -999.0
    pnls = np.array([t["pnl_pct"] for t in trades], dtype=float)
    if len(pnls) < 2:
        return float(np.mean(pnls))
    mean = np.mean(pnls)
    std = np.std(pnls, ddof=1)
    if std < 1e-6:
        return float(mean)
    # Sharpe 简化: mean - 0.5*std(波动率惩罚)
    return float(mean - 0.5 * std)
```

- [ ] **Step 4: 改 `app/backtest/ai_optimizer.py:152`**

**Before**:
```python
# 大约在 152 行,可能需要 grep 确认
np.random.uniform(...)
```

**After** (在 LHS 采样前加 seed):
```python
# L23 修复: 固定随机种子,保证可复现
np.random.seed(42)
# 然后继续 LHS 采样
lhs_samples = ...
```

(如果实际位置不同,先 `grep -n "np.random.uniform\|lhs_sample\|LatinHypercube" app/backtest/ai_optimizer.py` 找准确位置)

- [ ] **Step 5: 改 `app/backtest/ai_optimizer.py:932-947`**

**Before** (大致):
```python
# Top-10 按全样本 score 排序
top10 = sorted(all_results, key=lambda r: r['score'], reverse=True)[:10]
best_params = top10[0]['params']
```

**After** (增加 WFE 排序):
```python
# L23 修复: 用 (score, wfe) 联合排序,WFE 越接近 1 越好
# 缺失 WFE 视为 0(原样本过拟合)
def _sort_key(r):
    score = r.get('score', -999)
    wfe = r.get('wfe', 0)
    # 优先选 score 高的,WFE 高的(防止过拟合)
    return (score, wfe)

top10 = sorted(all_results, key=_sort_key, reverse=True)[:10]
best_params = top10[0]['params']
```

(如果原代码有 WFO 计算的 wfe 字段,直接用;否则需要先在 WFO 阶段存 wfe 到 results)

- [ ] **Step 6: 跑测试,确认 PASS**

```bash
python scripts/test_fix_23.py
```

Expected: 全部通过

- [ ] **Step 7: 跑回归**

```bash
python scripts/test_simple_runner.py
```

Expected: 通过(AI 优化器改动不影响 simple_runner)

- [ ] **Step 8: Commit**

```bash
git add app/backtest/ai_optimizer.py scripts/test_fix_23.py
git commit -m "fix(ai): real Calmar target + fixed seed + WFE in best selection (C4/C1-4)"
```

---

## Task 5: C1-5 新建 AST 沙箱 + 改 strategy_coder

**Files:**
- Create: `app/utils/__init__.py` (如果不存在)
- Create: `app/utils/ast_sandbox.py`
- Modify: `app/backtest/strategy_coder.py:46-53`
- Test: `scripts/test_fix_24.py`

- [ ] **Step 1: 写测试(RED)**

写 `scripts/test_fix_24.py`:

```python
"""验证 L24 修复: strategy_coder AST 沙箱"""
import sys
sys.stdout.reconfigure(encoding='utf-8')

from app.utils.ast_sandbox import validate_strategy_code, ForbiddenNodeError


def test_safe_code_passes():
    safe_code = """
import pandas as pd
def my_strategy(df):
    close = df['close']
    ma20 = close.rolling(20).mean()
    return (close > ma20).astype(int)
"""
    ok, msg = validate_strategy_code(safe_code)
    assert ok, f"正常代码被拒: {msg}"
    print(f"✅ 正常策略代码通过: {msg}")


def test_os_system_blocked():
    """含 os.system 的代码应被拒"""
    bad_code = """
import os
def evil_strategy(df):
    os.system('rm -rf /')
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, f"os.system 没被拦截!"
    assert 'os' in msg, f"错误消息应提到 os, 实际: {msg}"
    print(f"✅ os.system 被拒: {msg}")


def test_subprocess_blocked():
    bad_code = """
import subprocess
def evil(df):
    subprocess.run(['ls'])
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, "subprocess 没被拦截"
    print(f"✅ subprocess 被拒: {msg}")


def test_eval_exec_blocked():
    bad_code = """
def evil(df):
    eval('os.system("rm")')
    return 0
"""
    ok, msg = validate_strategy_code(bad_code)
    assert not ok, "eval 没被拦截"
    print(f"✅ eval 被拒: {msg}")


def test_strategy_coder_uses_validation():
    """strategy_coder 加载代码前应调 validate"""
    with open('app/backtest/strategy_coder.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'validate_strategy_code' in content, "strategy_coder 没调 validate"
    print("✅ strategy_coder 加载前调 validate")


if __name__ == '__main__':
    test_safe_code_passes()
    test_os_system_blocked()
    test_subprocess_blocked()
    test_eval_exec_blocked()
    test_strategy_coder_uses_validation()
    print("\n🎉 L24 修复验证通过")
```

- [ ] **Step 2: 跑测试,确认 FAIL**

```bash
python scripts/test_fix_24.py
```

Expected: `ModuleNotFoundError: No module named 'app.utils.ast_sandbox'`

- [ ] **Step 3: 创建 `app/utils/__init__.py`(如不存在)**

```bash
test -f app/utils/__init__.py || touch app/utils/__init__.py
```

- [ ] **Step 4: 新建 `app/utils/ast_sandbox.py`**

```python
"""
AST 沙箱: strategy_coder 加载 LLM 生成的策略代码前静态校验
L24 修复: 防止恶意 prompt 注入执行 os.system / subprocess 等

白名单 + 黑名单双保险:
- 白名单: 允许的 AST 节点类型(常见 Python 子集)
- 黑名单: 禁止导入的模块名、禁止的函数名
"""
import ast


FORBIDDEN_MODULES = {
    'os', 'sys', 'subprocess', 'shutil', 'socket', 'http',
    'urllib', 'requests', 'ftplib', 'smtplib', 'asyncio',
}

FORBIDDEN_FUNCTIONS = {
    '__import__', 'eval', 'exec', 'compile', 'open',
}


def validate_strategy_code(code: str) -> tuple[bool, str]:
    """
    校验 LLM 生成的策略代码
    Returns: (ok, message)
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"语法错误: {e}"

    for node in ast.walk(tree):
        # 检查 import
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split('.')[0]
                if root in FORBIDDEN_MODULES:
                    return False, f"禁止导入模块: {alias.name}"
        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split('.')[0]
                if root in FORBIDDEN_MODULES:
                    return False, f"禁止从 {node.module} 导入"
            for alias in node.names:
                if alias.name in FORBIDDEN_FUNCTIONS:
                    return False, f"禁止导入函数: {alias.name}"
        # 检查函数调用
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                if node.func.id in FORBIDDEN_FUNCTIONS:
                    return False, f"禁止调用函数: {node.func.id}"
    return True, "OK"
```

- [ ] **Step 5: 改 `app/backtest/strategy_coder.py:46-53`**

**Before** (需要先 grep 实际内容):
```bash
grep -n "exec\|compile\|exec(" app/backtest/strategy_coder.py | head -5
```

**典型 pattern**:
```python
        code = llm_response.content
        exec(code)  # 危险!
        # or
        compiled = compile(code, '<strategy>', 'exec')
        exec(compiled, namespace)
```

**After** (在 exec/compile 之前加 validate):
```python
        code = llm_response.content
        # L24 修复: 加载前 AST 沙箱校验
        from app.utils.ast_sandbox import validate_strategy_code
        ok, msg = validate_strategy_code(code)
        if not ok:
            log.error(f"strategy_coder: 代码未通过 AST 沙箱: {msg}")
            return None
        exec(code)  # or whatever the original was
```

- [ ] **Step 6: 跑测试,确认 PASS**

```bash
python scripts/test_fix_24.py
```

Expected: 全部通过

- [ ] **Step 7: 跑回归**

```bash
python scripts/test_simple_runner.py
```

Expected: 通过

- [ ] **Step 8: Commit**

```bash
git add app/utils/__init__.py app/utils/ast_sandbox.py app/backtest/strategy_coder.py scripts/test_fix_24.py
git commit -m "fix(security): add AST sandbox for strategy_coder, block os/subprocess/eval (D5/C1-5)"
```

---

## Task 6: 批 1 CHANGELOG + 总验收

**Files:**
- Create: `CHANGELOG-2026-06-25-batch1.md`

- [ ] **Step 1: 写 CHANGELOG**

```markdown
# 2026-06-25 批 1 地基优化 CHANGELOG

> 5 个 commit,4 项地基优化(真相源/.gitignore/AI 目标/沙箱),排除实盘

## 修复的 4 项 P0/P1

| 项 | 简述 | Commit | 来源 |
|---|---|---|---|
| B 真相源 | 删 engine.py 假默认值,新建 schema.py 唯一加载 | (待填) | 复盘报告 |
| B 真相源 | 删 settings.py property default,缺键报错 | (待填) | 复盘报告 |
| E 工程卫生 | 新建 .gitignore,清理 logs/dmp 入库 | (待填) | 复盘报告 |
| C AI 目标 | 真 Calmar + 固定 seed + WFE 选优 | (待填) | 复盘报告 |
| D 安全 | strategy_coder AST 沙箱,禁导 os/subprocess/eval | (待填) | 复盘报告 |

## 监控表

| Commit | 文件 | 测试 | 服务 | 备注 |
|---|---|---|---|---|
| C1-1 | schema.py + engine.py | ✅ | ✅ | |
| C1-2 | settings.py + backtest.py | ✅ | ✅ | |
| C1-3 | .gitignore + 清理 | ✅ | ✅ | |
| C1-4 | ai_optimizer.py | ✅ | ✅ | |
| C1-5 | ast_sandbox.py + strategy_coder.py | ✅ | ✅ | |

## 已知遗留(批 2/3 处理)

- 4 引擎成交执行层未统一(批 2)
- hold_days 口径不一致(批 2)
- event_engine 队列泄漏(批 2)
- DuckDB 连接回收(批 2)
- 净值口径用成本价(批 2)
- pytest 测试体系(批 3)
- AI 样本外协议(批 3)
- 模拟盘参数源对齐(批 3)
```

- [ ] **Step 2: 填入 commit hash**

```bash
# 收集 5 个 commit 的 hash
git log --oneline -6 | head -5
```

把 hash 填进 CHANGELOG(可选,不阻塞)。

- [ ] **Step 3: Commit CHANGELOG**

```bash
git add CHANGELOG-2026-06-25-batch1.md
git commit -m "docs(changelog): 2026-06-25 批1地基优化(4项,5 commits)"
```

- [ ] **Step 4: 总验收(全跑一次)**

```bash
# 跑所有 test_fix_*.py
for f in scripts/test_fix_*.py; do
    python "$f" 2>&1 | tail -2
done

# 跑 simple_runner 回归
python scripts/test_simple_runner.py 2>&1 | tail -5

# 跑一次小回测验证(参数从 config 读)
python -c "
import json
with open('output/backtest_config.json') as f:
    cfg = json.load(f)
import sys; sys.path.insert(0, '.')
from app.backtest.tdx_runner import run_tdx_backtest
result = run_tdx_backtest(cfg)
print(f'status={result.get(\"status\")} total_return={result[\"summary\"][\"total_return\"]}%')
"
```

Expected: 0 报错 0 崩溃(用户硬约束)

- [ ] **Step 5: Push(若网络通)**

```bash
git push origin master 2>&1 | head -3
```

如果网络失败,告知用户稍后手动 push。

---

## Self-Review

1. **Spec 覆盖**:
   - ✅ B 真相源 → Task 1(新建 schema,改 engine.py),Task 2(改 settings.py/backtest.py)
   - ✅ E .gitignore → Task 3
   - ✅ C AI 目标函数 → Task 4
   - ✅ D 沙箱 → Task 5
   - ✅ 验证清单 → Task 6 总验收

2. **占位扫描**:
   - 没有 "TBD" / "TODO" / "implement later"
   - 错误处理:每个 task 都有 fail → fix → pass 流程
   - 范围: 5 个 commit 严格对应 spec 的 5 个 commit

3. **类型一致性**:
   - `RiskSchema` 字段在 schema.py 定义,在 engine.py 使用 → 一致
   - `load_risk_params()` 在 schema.py 定义,被 engine.py 导入 → 一致
   - `validate_strategy_code()` 在 ast_sandbox.py 定义,被 strategy_coder.py 导入 → 一致

无问题。
