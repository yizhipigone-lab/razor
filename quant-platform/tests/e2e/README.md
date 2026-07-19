# 前端配置往返端到端测试

防止 `saveRiskSettings` / `loadSettings` 的 **/100 单位 bug** 回归。
2026-07-20 该 bug 在多轮审计（07-13/15/19）全部漏查，根因之一是审计没覆盖前端、没追值、没端到端测试。这套测试 + [tests/test_risk_unit_conventions.py](../test_risk_unit_conventions.py) 一起把闭环守住。

## 测什么

1. 打开页面 → 在硬止损框填 `-60` → 点「保存此卡」
2. GET `/api/settings` 断言 `risk.hard_stop_loss_pct == -60`（不是 `-0.6`）
3. 刷新页面 → 断言输入框回显 `-60`（不是 `-0.6`）

往返任一跳变成 `-0.6` = 前端又多了 `/100`，测试会红。

## 前置

1. **本地服务在跑**（默认 `http://localhost:8000`，用环境变量 `BASE_URL` 改）
2. **Playwright**：全局已有 CLI（`npx playwright --version` 验证）；若缺，`npm install -g @playwright/test`
3. **首选 dry-run / 独立测试实例**：测试会短暂把 `hard_stop_loss_pct` 改成 -60（`afterAll` 自动恢复原值），但恢复前有几秒窗口——**实盘 mode=live 交易时段勿跑**，改硬止损会影响真实风控

## 跑法

```bash
# 默认(localhost:8000)
npx playwright test --config tests/e2e/playwright.config.js

# 指定目标地址
BASE_URL=http://localhost:8000 npx playwright test --config tests/e2e/playwright.config.js

# 只看 console 输出(看存入/回显的实际值)
npx playwright test --config tests/e2e/playwright.config.js --reporter=list
```

## 何时跑

改了以下任一处后**必跑**：

| 文件 | 函数/位置 |
|---|---|
| `static/js/main.js` | `saveRiskSettings` / `loadSettings` / `saveBuyAmtSettings` |
| `static/index.html` | 系统配置卡输入框（`#set-stop` / `#set-trail-act` / `#set-trail-dd` 等） |
| `app/api/backtest.py` | `save_risk_params`（POST `/api/settings/risk-params`） |
| `app/config/risk_params.py` | `load_risk_params`（settings 百分数 → 引擎小数） |

## 安全机制

- `beforeAll` 备份当前 `hard_stop_loss_pct`
- `afterAll` 用 API 写回原值
- 即使测试中途失败，`afterAll` 仍会尝试恢复
- `fullyParallel: false` + `workers: 1` + `retries: 0`：串行、不重试，避免反复改 risk 配置

## 已知限制

- 用 `force: true` 填值绕过"输入框在折叠 tab 里不可见"的问题——若页面结构大改导致 `#set-stop` 不存在，`waitFor` 会超时
- 只覆盖硬止损一个字段（`hard_stop_loss_pct`）作为代表；移动激活/回撤/时间退出盈利同链路，守好一个等同守好一类
- 后端单位约定（settings 百分数 → 引擎小数）由 [tests/test_risk_unit_conventions.py](../test_risk_unit_conventions.py) 的对照表锁死，这里只管前端往返
