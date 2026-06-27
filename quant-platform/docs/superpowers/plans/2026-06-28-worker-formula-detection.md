# Worker 公式变量探测 + Value 归一化 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 worker 能正确处理任意 TDX 公式（任意变量名 + 任意 Value 范围），不再受 `ZP:Value=1` 限制。验证 QUANTQQ + GUPIAO_011 都能正确选股。

**Architecture:**
- **遍历变量名**（学 VERA）：不再 `val.get("ZP")`，而是遍历 `val.items()` 拿到所有字段
- **归一化 Value 判断**：非零且非空即视为信号（兼容 1/100/0.5/任意非零）

**Tech Stack:** Python 3 (worker 脚本是 E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py)

---

### Task 1: worker 加 `_is_signal_value()` 工具函数

**Files:**
- Modify: `E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py:1-15`

- [ ] **Step 1: 在 worker 顶部加工具函数**

打开 worker，在 `BATCH_SIZE = 50` 后面加：

```python
def _is_signal_value(value_str):
    """
    判断 TDX 返回的 Value 是否代表"选中"。
    兼容:
      - '1', '100', '0.5' → 选中（非零）
      - '0', '0.0', '', None → 未选中（零或空）
    """
    if value_str is None:
        return False
    s = str(value_str).strip()
    if s == "" or s == "0" or s == "0.0":
        return False
    try:
        return float(s) != 0.0
    except (ValueError, TypeError):
        return False
```

- [ ] **Step 2: 验证函数 import**

Run: `python -c "exec(open(r'E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py').read().split('def main()')[0]); print(_is_signal_value('1'), _is_signal_value('100'), _is_signal_value('0'), _is_signal_value(''))"`
Expected: `True True False False`

- [ ] **Step 3: 提交**

```bash
cd "e:/1target/p9_project/quant-platform"
git add "E:/NEW_TDX/PYPlugins/user/tqsdk_bridge_worker.py"  # 路径在 Windows 上
# 如果 git 不接受绝对路径, 用相对路径或先 cp 进 scripts/
```

---

### Task 2: `_do_screen()` 改用遍历变量名 + 归一化

**Files:**
- Modify: `E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py:139-153`

- [ ] **Step 1: 替换 _do_screen 内部的 result 解析**

把这段：
```python
        for code, val in result.items():
            if code == "ErrorId" or not val or not isinstance(val, dict):
                continue
            raw = val.get(output_var)
            if not raw or not isinstance(raw, list) or len(raw) == 0:
                continue
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                dt = str(entry.get("Date", ""))
                if dt != end_time:
                    continue
                if str(entry.get("Value", "")) == match_value:
                    matched.append(code)
                break
```

替换成：
```python
        for code, val in result.items():
            if code == "ErrorId" or not val or not isinstance(val, dict):
                continue
            # 遍历所有字段（不挑变量名），兼容 ZP/ZT/中文名/任意
            hit = False
            for var_name, entries in val.items():
                if var_name == "ErrorId" or not isinstance(entries, list) or len(entries) == 0:
                    continue
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    dt = str(entry.get("Date", ""))
                    if dt != end_time:
                        continue
                    # 非零即信号（兼容 1/100/任意非零）
                    if _is_signal_value(entry.get("Value")):
                        hit = True
                        break
                if hit:
                    break
            if hit:
                matched.append(code)
```

- [ ] **Step 2: 提交**

```bash
git add ...
git commit -m "feat(worker): _do_screen 遍历所有变量名 + 非零即信号"
```

---

### Task 3: `_do_range()` 改用遍历变量名 + 归一化

**Files:**
- Modify: `E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py:201-212`

- [ ] **Step 1: 替换 _do_range 内部的 result 解析**

把这段：
```python
        for code, val in sig_result.items():
            if code == "ErrorId" or not val or not isinstance(val, dict):
                continue
            raw = val.get(output_var)
            if not raw or not isinstance(raw, list) or len(raw) == 0:
                continue
            dates = [item["Date"] for item in raw]
            values = [str(item["Value"]) for item in raw]
            has_signal = "1" in values
            signals[code] = {"Date": dates, output_var: values}
            if has_signal:
                signal_codes.add(code)
```

替换成：
```python
        for code, val in sig_result.items():
            if code == "ErrorId" or not val or not isinstance(val, dict):
                continue
            # 遍历所有字段（不挑变量名）
            dates = []
            values = []
            has_signal = False
            hit_var = None
            for var_name, entries in val.items():
                if var_name == "ErrorId" or not isinstance(entries, list) or len(entries) == 0:
                    continue
                cur_dates = [str(item.get("Date", "")) for item in entries if isinstance(item, dict)]
                cur_values = [str(item.get("Value", "")) for item in entries if isinstance(item, dict)]
                if any(_is_signal_value(v) for v in cur_values):
                    if not has_signal:
                        has_signal = True
                        hit_var = var_name
                        dates = cur_dates
                        values = cur_values
            if not dates:
                continue
            # 用探测到的变量名作 key（向后兼容）
            signals[code] = {"Date": dates, hit_var or output_var: values}
            if has_signal:
                signal_codes.add(code)
```

- [ ] **Step 2: 提交**

```bash
git add ...
git commit -m "feat(worker): _do_range 遍历所有变量名 + 探测 hit_var"
```

---

### Task 4: `_probe_formulas()` 改用归一化

**Files:**
- Modify: `E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py:90-98`

- [ ] **Step 1: 替换 _probe_formulas 的 trigger 判定**

把这段：
```python
        sig_payload = r.get(probe_code) or r.get(probe_code.split(".")[0])
        if isinstance(sig_payload, dict):
            dates = sig_payload.get("Date", [])
            vals = sig_payload.get(name) or sig_payload.get("ZP", [])
            triggers = [(d, v) for d, v in zip(dates, vals) if str(v) == "1"]
            rec["trigger_count"] = len(triggers)
            rec["last_trigger"] = triggers[-1][0] if triggers else None
```

替换成：
```python
        sig_payload = r.get(probe_code) or r.get(probe_code.split(".")[0])
        if isinstance(sig_payload, dict):
            # 遍历所有字段找信号（兼容任意变量名 + 非零 Value）
            triggers = []
            hit_var = None
            for var_name, entries in sig_payload.items():
                if var_name == "ErrorId" or not isinstance(entries, list):
                    continue
                cur = [(e["Date"], e["Value"]) for e in entries if isinstance(e, dict) and _is_signal_value(e.get("Value"))]
                if cur and not hit_var:
                    hit_var = var_name
                    triggers = cur
            rec["trigger_count"] = len(triggers)
            rec["last_trigger"] = triggers[-1][0] if triggers else None
            rec["hit_var"] = hit_var
```

- [ ] **Step 2: 提交**

```bash
git add ...
git commit -m "feat(worker): _probe_formulas 用归一化判定"
```

---

### Task 5: 自检 — 跑 QUANTQQ + GUPIAO_011 验证

**Files:**
- New: `e:\1target\p9_project\quant-platform\scripts\verify_formula_detection.py`

- [ ] **Step 1: 写验证脚本**

```python
"""
验证 worker 改动: QUANTQQ (ZP=1) + GUPIAO_011 (ZT=100) 都能正确选股
"""
import sys, subprocess, json
from pathlib import Path

ROOT = Path(r"e:\1target\p9_project\quant-platform")
WORKER = Path(r"E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py")
TDX_DIR = Path(r"E:\NEW_TDX\PYPlugins\user")

def run_probe(formula_name, probe_code="605289.SH"):
    """通过 worker 的 _probe_formulas 路径探测"""
    # 用 task_type=probe_formulas 走探测路径
    task = {
        "task_type": "probe_formulas",
        "candidates": [formula_name],
        "probe_code": probe_code,
        "end_time": "20260627",
        "start_time": "20260101",
        "count": 30,
    }
    import tempfile
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(task, f)
        args_path = f.name

    try:
        result = subprocess.run(
            ["python", str(WORKER), "--args-file", args_path],
            cwd=str(TDX_DIR),
            capture_output=True, text=True, timeout=60,
            env={"PYTHONPATH": str(TDX_DIR), "PYTHONUNBUFFERED": "1"},
        )
        for line in result.stdout.strip().split("\n"):
            try:
                data = json.loads(line)
                if data.get("status") == "ok" and "results" in data:
                    return data["results"][0]
            except json.JSONDecodeError:
                continue
        print(f"[ERROR] worker 输出: {result.stdout[:500]}")
        print(f"[ERROR] worker stderr: {result.stderr[:500]}")
        return None
    finally:
        Path(args_path).unlink(missing_ok=True)


# 测试 1: QUANTQQ
print("=" * 70)
print("Test 1: QUANTQQ (变量 ZP, Value=1)")
print("=" * 70)
result = run_probe("QUANTQQ")
if result:
    print(f"  触发次数: {result.get('trigger_count', 'N/A')}")
    print(f"  最近触发: {result.get('last_trigger', 'N/A')}")
    print(f"  命中变量: {result.get('hit_var', 'N/A')}")
    assert result.get("trigger_count", 0) > 0, "QUANTQQ 应该命中"
    print(f"  [OK] QUANTQQ 通过")

# 测试 2: GUPIAO_011
print("\n" + "=" * 70)
print("Test 2: GUPIAO_011 (变量 ZT, Value=100)")
print("=" * 70)
result = run_probe("GUPIAO_011")
if result:
    print(f"  触发次数: {result.get('trigger_count', 'N/A')}")
    print(f"  最近触发: {result.get('last_trigger', 'N/A')}")
    print(f"  命中变量: {result.get('hit_var', 'N/A')}")
    assert result.get("trigger_count", 0) > 0, "GUPIAO_011 应该命中"
    print(f"  [OK] GUPIAO_011 通过")

# 测试 3: 不存在的公式
print("\n" + "=" * 70)
print("Test 3: NOT_A_FORMULA (期望 ErrorId 非 0/19)")
print("=" * 70)
result = run_probe("NOT_A_FORMULA_XYZ")
if result:
    print(f"  error_id: {result.get('error_id', 'N/A')}")
    print(f"  error: {result.get('error', 'N/A')[:100]}")
    assert result.get("error_id") not in ("0", "19"), "错误公式应该 ErrorId != 0"
    print(f"  [OK] 错误处理正确")
```

- [ ] **Step 2: 跑验证**

Run: `python scripts/verify_formula_detection.py`
Expected: 3 个测试全部通过

- [ ] **Step 3: 提交**

```bash
git add scripts/verify_formula_detection.py
git commit -m "test(worker): verify formula detection works for QUANTQQ + GUPIAO_011"
```

---

## 改动清单总结

| 文件 | 改动 | 关键变化 |
|------|------|---------|
| `E:\NEW_TDX\PYPlugins\user\tqsdk_bridge_worker.py` | 修改 +60/-30 行 | 新增 `_is_signal_value()`，3 个函数改用遍历变量名 + 归一化 |
| `e:\1target\p9_project\quant-platform\scripts\verify_formula_detection.py` | **新建** | 验证脚本 |

**不改动的文件**：
- `app/tqsdk/bridge.py`（task 协议不变，output_var 仍兼容）
- `app/api/tqsdk.py`（API 调用协议不变）
- `app/backtest/tdx_runner.py`（params["strategy_name"] 仍正常传递）
- 前端（不需改）

---

## 测试验证

| 步骤 | 预期 |
|------|------|
| pytest tests/ | 21/21 PASS（不影响现有测试） |
| `python scripts/verify_formula_detection.py` | 3 个测试 PASS（QUANTQQ/GUPIAO_011/NOT_A_FORMULA） |
| 端到端跑 QUANTQQ 公式回测 | 1297 笔交易（同之前） |
| 端到端跑 GUPIAO_011 公式回测 | 3042 笔交易（之前是 0 笔） |

---

## 风险与边界

| 风险 | 缓解 |
|------|------|
| worker 改动影响其他系统 | 文件位于 `E:\NEW_TDX\PYPlugins\user\`（独立目录），改动隔离 |
| Value 归一化漏掉真信号 | 0/0.0/空 → 不命中，1/100/0.5 → 命中，符合"非零=选中"直觉 |
| 多个变量都被命中（理论上 TDX 只返回一个变量） | 第一个命中的变量被记为 `hit_var`，其他被忽略 |