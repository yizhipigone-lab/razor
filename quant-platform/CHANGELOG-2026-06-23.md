# 2026-06-23 Bug 修复 CHANGELOG

> 5 个已知遗留(同根源 bug)修复收尾,延续 2026-06-22 的 8 个修复。

## 修复的 5 个已知遗留

| L# | 来源 | 简述 | Commit | 域 |
|---|---|---|---|---|
| L1 | #7 sibling | `backfill_daily_tushare.py` 删 `* 1000` | `9616a7c` | A 数据 |
| L2 | #10 sibling | 3 处 `deepseek-v4-pro` → `deepseek-chat` | `a205786` | B Agent |
| L3 | #11 sideeffect | 维护 `self._today_trades`,API/cron 改用 | `ab01e85` | C 模拟盘 |
| L4 | #11 sideeffect | engine 加 `refresh_trades_from_store()`,reporter/main 修复 | `7b372b7` | C 模拟盘 |
| L5 | #9 sibling | store 持久化 `_prev_day_snap`(复用 sim_state 表) | `92e3c2f` | C 模拟盘 |

**额外 commit**:
- `42b56d7` Revert Task 9(测试文件名冲突,被立即 revert 重做)
- `fcf1f21` 原始 Task 9 commit(被 revert)

## 额外发现 + 修复

1. **`.env` 仍有 `LLM_MODEL=deepseek-v4-pro`**(runtime 配置,不在 git)
   - 已本地修复(`.env` 被 `.gitignore` 保护,无法 commit)
   - 用户机器需要手动确认 `.env` 第 3 行已改为 `deepseek-chat`

2. **main.py:61 实际是 `SimTraderEngine(persist=False)`**(与 __init__ 签名不匹配会抛 TypeError)
   - L4 修复顺手改为 `SimTraderEngine(store=SimTraderStore())`
   - 这是**潜在长期 bug** — 不修复则 main.py 实际无法跑

## 修复期间监控表(全部打勾 ✅)

| Commit | 文件改动 | 测试通过 | 服务启动 | 页面访问 | API 正常 | 前端同步 | 备注 |
|---|---|---|---|---|---|---|---|
| L1 (#7 sib) | ✓ | ✓ | ✓ | ✓ | ✓ | - | |
| L2 (#10 sib) | ✓ | ✓ | ✓ | ✓ | ✓ | - | + .env 手动改 |
| L3 (#11 side) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 涉及 API trade_count/sell_count |
| L4 (#11 side) | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | 顺带修复 main.py TypeError |
| L5 (#9 sib) | ✓ | ✓ | ✓ | ✓ | ✓ | - | 涉及 store 持久化 |

**硬约束触发**:
- 1 次 Revert(L1 测试文件名冲突)
- 1 次 Amend(L4 删除 `self._store` 重复赋值 cosmetic)
- 1 次 Plan bug 修复(L4 测试设计 + main.py persist=False TypeError)

## 用户需要做的操作

### 立即手测

按 [CHANGELOG-2026-06-22.md §10](CHANGELOG-2026-06-22.md) 同样清单:
- [ ] 主页访问
- [ ] 选股 Tab
- [ ] 回测 Tab
- [ ] 模拟盘 Tab
- [ ] 数据同步
- [ ] AI 委员会
- [ ] 设置页面

### 验证 L3 修复效果

- 模拟盘"今日交易数"应准确(不再突然归零)
- cron 触发的 sell_count 应准确

### 验证 L4 修复效果

- 回测完成后报表应显示交易记录(之前是"无交易")
- main.py 现在能正常启动(不抛 TypeError)

### 验证 L5 修复效果

- 重启服务后第一次 sell_phase 应正常(除权跳空保护有昨日 snap 数据)

### 手动确认 .env

```bash
# 用户机器上需要确认 .env 第 3 行
grep LLM_MODEL .env
# 应输出: LLM_MODEL=deepseek-chat
```

## 文档

- Spec: [docs/superpowers/specs/2026-06-23-known-issues-design.md](docs/superpowers/specs/2026-06-23-known-issues-design.md)
- Plan: [docs/superpowers/plans/2026-06-23-known-issues.md](docs/superpowers/plans/2026-06-23-known-issues.md)
- 上批 Spec: [docs/superpowers/specs/2026-06-22-bug-fixes-design.md](docs/superpowers/specs/2026-06-22-bug-fixes-design.md)
- 上批 CHANGELOG: [CHANGELOG-2026-06-22.md](CHANGELOG-2026-06-22.md)
