# 任务计划：删除 engine.py 和 run.py 死代码

## 目标

删除 `engine.py` 和 `run.py` 中的废弃回测入口，清理相关死代码。

## 背景（审计确认）

| 调用方 | 状态 |
|--------|------|
| 前端 `runBacktest()` → `POST /api/backtest` | ✅ 死代码，前端无按钮触发 |
| `python run.py backtest` | 极少使用，前端无入口依赖 |
| `python run.py scan` | 运维使用，保留 `download`/`buy` 命令 |

## 当前阶段
阶段 1

---

## 各阶段

### 阶段 1：确认删除范围（进行中）
- [x] 确认 `engine.py` 无活跃 HTTP 调用方
- [x] 确认 `run.py` 的 backtest/scan CLI 为废弃路径
- [x] 确认 `ai_optimizer.py` 中 `self._engine` 为 dead code（待删）
- **状态：** in_progress

### 阶段 2：删除 engine.py
- [ ] 删除 `app/backtest/engine.py` 文件
- [ ] 从 `app/api/backtest.py` 删除 `POST /api/backtest` 端点（line 320-380 附近）
- [ ] 删除 `ai_optimizer.py:29` 的 `from app.backtest.engine import BacktestEngine`
- [ ] 删除 `ai_optimizer.py:263` 的 `self._engine = BacktestEngine()`
- **状态：** pending

### 阶段 3：清理 run.py
- [ ] 删除 `run.py` 中的 `cmd_backtest()` 函数（line 57-78）
- [ ] 删除 `run.py` 中的 `cmd_scan()` 函数（line 38-54）
- [ ] 删除 `run.py` 中的 `backtest` CLI 分支（line 121-123）
- [ ] 删除 `run.py` 中的 `scan` CLI 分支（line 118-120）
- [ ] 保留 `cmd_download()` / `cmd_buy()`（活跃使用）
- **状态：** pending

### 阶段 4：验证与测试
- [ ] 运行 pytest 确认无回归
- [ ] 确认前端回测功能正常（走 `POST /api/backtest/run-simple`）
- [ ] 记录到 progress.md
- **状态：** pending

---

## 删除清单

### 文件删除
- `app/backtest/engine.py` — 全部删除

### 端点删除
- `app/api/backtest.py` — `POST /api/backtest` 端点

### run.py 函数删除
- `run.py:cmd_backtest()` — 删除
- `run.py:cmd_scan()` — 删除
- `run.py` CLI 分支 `backtest` — 删除
- `run.py` CLI 分支 `scan` — 删除

### ai_optimizer.py 清理
- 删除 `app/backtest/ai_optimizer.py:29`：`from app.backtest.engine import BacktestEngine`
- 删除 `app/backtest/ai_optimizer.py:263`：`self._engine = BacktestEngine()`

### run.py 保留
- `cmd_download()` — 活跃使用
- `cmd_buy()` — 活跃使用

---

## 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| `run.py backtest` 仍被 cron 任务调用 | cron 任务失效 | 确认无 cron 调用 run.py backtest |
| `POST /api/backtest` 被其他内部服务调用 | 404 | 确认只有前端用，前端已废弃 |
| `ai_optimizer` 需要 `BacktestEngine` 未来用到 | 未来需重加 | 确认 `self._engine` 从未被任何方法调用 |

---

## 已确认（审计后）

- ✅ `engine.py` 的 HTTP 接口（`POST /api/backtest`）前端已废弃（`runBacktest()` 是死函数）
- ✅ `run.py backtest` CLI 极少使用，无前端入口
- ✅ `run.py scan` CLI 极少使用，无前端入口
- ✅ `ai_optimizer.py:263` 的 `self._engine = BacktestEngine()` 从未被任何方法访问

## 备注
- 2026-07-14：经前端 Grep + Grep 全项目验证，engine.py HTTP 路径已废弃
