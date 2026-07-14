# Deprecation Timeline (2026-07-15)

> 跟踪所有标 DEPRECATED 的接口/模块/函数,定义删除日期。
> 这是工程纪律的硬约束 — 过期不删 = 技术债。

| 接口 | 位置 | 标记日期 | 计划删除 | 原因 | 迁移路径 |
|---|---|---|---|---|---|
| `app.config.schema.load_risk_params()` | `app/config/schema.py:36-66` | 2026-07-14 | **2026-08-15** | risk_params.py 是新唯一入口 | `from app.config.risk_params import load_risk_params` |
| `app.config.schema.RiskSchema` | `app/config/schema.py:14-33` | 2026-07-14 | **2026-08-15** | 同上 | `RiskParams` (字段名带 `_pct` 后缀) |
| `app.backtest.exit_rules._pct()` | `app/backtest/exit_rules.py:19-37` | 2026-07-15 | **2026-08-30** | 单位魔法判定有歧义 | 直接传小数,不走 _pct |
| `app.live_trader.main.py` (legacy qmt_proxy:8081) | 已迁移至 live_trader:8001 | 2026-07-13 | **已删除** | 重复实现 | 全部走 live_trader |

## 验证 (CRITICAL)

跑下面命令确认 deprecated 接口无调用方:
```bash
grep -rn "from app.config.schema\|app.config.schema.load_risk_params" app/ tests/ | grep -v __pycache__
grep -rn "_pct(" app/ tests/ | grep -v __pycache__
```

如果搜索结果只剩 import 行,即可删除兼容层。

## 删除流程

1. 跑上面 grep,记录所有引用方
2. 通知引用方迁移(本 ADR 已发)
3. 等到删除日期后,删除兼容层
4. 在 commit message 引用本 ADR