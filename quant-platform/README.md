# 睿奕量化 (Eurica Quant)

> A 股量化交易一体化平台 —— 选股 · 模拟盘 · 实盘 · 回测 · 风控

## 快速启动

```bat
# 1. 安装依赖（首次）
venv313\Scripts\pip install -r requirements.txt

# 2. 一键启动（API 8888 + 实盘 8001）
start.bat

# 3. 打开前端
浏览器访问 http://localhost:8888
```

停止服务：`stop.bat`

---

## 系统架构

```
┌─────────────────────────────────────────────────────┐
│                   前端 (index.html)                  │
│            http://localhost:8888                      │
├─────────────┬───────────────────┬───────────────────┤
│  API 服务    │   模拟盘引擎      │   实盘交易服务     │
│  :8888       │   SimTraderEngine │   :8001            │
│  FastAPI     │                   │   live_trader      │
├─────────────┼───────────────────┼───────────────────┤
│  数据层      │   选股桥接         │   行情/交易网关    │
│  DuckDB      │   TDX Bridge      │   QMT (xtquant)   │
│  Parquet     │   公式选股         │   腾讯HTTP(回退)   │
└─────────────┴───────────────────┴───────────────────┘
```

- **API 服务** (`main.py`)：端口 8888，提供所有前端接口 + WebSocket 推送
- **实盘服务** (`app/live_trader/`)：端口 8001，独立进程，通过 QMT 下单
- **数据存储**：DuckDB（结构化）+ Parquet（时序行情）+ JSON（运行态）

---

## 功能清单

### 📡 行情与数据

| 功能 | 说明 | 入口 |
|------|------|------|
| QMT 实时行情 | 首选行情源，Windows 端 8001 | `/live/quotes` |
| 腾讯 HTTP 行情 | QMT 失败时自动回退 | `data_loader.py` |
| Parquet 历史数据 | 兜底行情源 | `data/parquet/daily/` |
| Tushare 数据同步 | 日线/分钟线同步 | `app/data_manager/` |
| 板块热度 | 概念板块同步+热度计算 | `app/hot_sector/` |
| 自选股管理 | 增删查+备注 | `app/api/watchlist.py` |

### 🔍 选股

| 功能 | 说明 | 入口 |
|------|------|------|
| TDX 公式选股 | 通达信公式桥接，支持动态切换公式名 | `app/tqsdk/` |
| 策略工厂选股 | 自然语言→选股公式 | `app/strategy_factory/` |
| 条件选股 | 多维度筛选（板块/市值/涨跌幅） | `app/screener/` |
| MA5 角度策略 | 均线角度选股 | `app/screener/strategies/` |
| 盘整突破策略 | 横盘突破选股 | `app/screener/strategies/` |

### 📈 模拟盘

| 功能 | 说明 | 入口 |
|------|------|------|
| 自动选股买入 | TDX 信号自动执行 | `app/sim_trader/` |
| 8 大离场规则 | 硬止损/保本/首日/时间/追踪止盈/目标价/时间条件/量价背离 | `app/sim_trader/` |
| 净值曲线 | 实时净值+净值可信度标记 | `state.json` → 前端图表 |
| 冷却期 | 同股买入冷却 | `SAME_STOCK_COOLDOWN` |
| 暂停/恢复 | 一键暂停/恢复交易 | 前端开关 |
| 策略名自定义 | 前端修改策略别名，保存即生效 | 前端"策略名"输入框 |

### 💹 实盘交易

| 功能 | 说明 | 入口 |
|------|------|------|
| dry-run / live 模式 | 模拟模式不真下单 | `LIVE_TRADER_MODE` 环境变量 |
| 10 闸门风控 (RiskGate) | 单笔/仓位/日亏/连续拒绝/同股冷却等 10 层保护 | `app/live_trader/` |
| Kill Switch | 三态(内存/文件/DB)紧急熔断 | 前端按钮 + API |
| 离场扫描 | 8 条离场规则，可配置扫描间隔(10~300s) | `/live/config/scan-interval` |
| 对账 | 三方比对(QMT/本地/成交)，4 时点自动+手动触发 | `/live/reconcile` |
| 审计回放 | 按 order_id 回放完整决策链 | `/live/audit/replay/{oid}` |
| 清仓锁 | 卖出时自动上锁防重复 | `ClearanceLock` |
| 信号桥接 | API 服务→实盘服务推送买入信号 | `/live/buy-signal` |
| 买入量计算 | 按资金比例+风控闸门自动算量 | `buy_volume.py` |
| 持仓接管 | 启动时接管 QMT 已有持仓，ETF 保留不操作 | `preserved_codes` |

### 📊 回测

| 功能 | 说明 | 入口 |
|------|------|------|
| 标准回测 | BacktestEngine 基础引擎 | `app/backtest/engine.py` |
| 简易回测 | FastEngine 快速回测 | `app/backtest/simple_runner.py` |
| TDX 回测 | 通达信公式回测 | `app/backtest/tdx_runner.py` |
| 严格回测 | StrictEngine，含 T+1/涨跌停/滑点 | `app/backtest/strict_runner.py` |
| 执行器 | 统一交易执行模拟层 | `app/backtest/execution.py` |

> ⚠️ 四种回测引擎的成本/T+1/涨跌停处理各写各的，**结果不可互相对比**。

### 🤖 AI 功能

| 功能 | 说明 | 入口 |
|------|------|------|
| AI 选股分析 | 多 Agent 委员会分析 | `app/agents/` |
| 策略翻译 | 自然语言→选股公式 | `app/strategy_factory/translator.py` |
| AST 沙箱 | 公式安全执行 | `app/utils/ast_sandbox.py` |

### ⚙️ 系统配置

| 功能 | 说明 | 入口 |
|------|------|------|
| 统一配置 | `config/app_setting.json` 唯一真相源 | `core/settings.py` |
| 热加载 | settings.set + save 后立即生效 | 前端保存 |
| TDX 公式名 | 前端修改保存后，选股/信号转发立即使用新公式名 | `/api/settings/tqsdk-formula` |
| 风控参数 | 前端可调闸门比例/资金等 | 实盘配置区 |
| 离场扫描间隔 | 前端可调，保存即生效+持久化 | `/live/config/scan-interval` |

---

## 关键文件

| 文件 | 用途 |
|------|------|
| `main.py` | API 服务入口 (8888) |
| `app/live_trader/main.py` | 实盘服务入口 (8001) |
| `app/scheduler/cron_jobs.py` | 定时任务(选股/同步/信号转发) |
| `app/sim_trader/main.py` | 模拟盘引擎 |
| `app/tqsdk/bridge.py` | TDX 公式选股桥接 |
| `app/live_trader/scheduler.py` | 实盘定时调度 |
| `app/live_trader/config.py` | 实盘配置(frozen dataclass) |
| `core/settings.py` | 统一配置读写 |
| `config/app_setting.json` | 持久化配置文件 |
| `app/trader/gateways/qmt.py` | QMT 交易网关 |

---

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `QMT_ACCOUNT_ID` | QMT 资金账号 | (必填) |
| `QMT_USERDATA_PATH` | QMT 数据目录 | `D:\Program Files\XCXT\userdata_mini` |
| `LIVE_TRADER_MODE` | dry-run / live | `dry-run` |
| `LIVE_TRADER_CAPITAL` | 实盘资金 | `100000` |
| `LIVE_TRADER_URL` | 实盘服务地址 | `http://127.0.0.1:8001` |

---

## 技术栈

- **后端**: Python 3.13 + FastAPI + Uvicorn
- **数据库**: DuckDB + Parquet
- **行情/交易**: QMT (xtquant) + 腾讯HTTP + Tushare
- **选股**: 通达信公式桥接 (pytdx2)
- **AI**: OpenAI + LangChain + LangGraph
- **前端**: 原生 HTML/JS + WebSocket 实时推送
- **调度**: APScheduler
- **日志**: Loguru

---

## 目录结构

```
quant-platform/
├── main.py                 # API 服务入口
├── start.bat / stop.bat    # 启停脚本
├── config/
│   └── app_setting.json    # 统一配置
├── core/
│   ├── settings.py         # 配置读写引擎
│   ├── logger.py           # 日志
│   └── limiter.py          # 速率限制
├── app/
│   ├── api/                # FastAPI 路由
│   ├── sim_trader/         # 模拟盘
│   ├── live_trader/        # 实盘交易
│   ├── tqsdk/              # TDX 选股桥接
│   ├── screener/           # 条件选股
│   ├── backtest/           # 回测引擎
│   ├── strategy_factory/   # 策略工厂
│   ├── agents/             # AI 分析
│   ├── hot_sector/         # 板块热度
│   ├── data_manager/       # 数据管理
│   ├── scheduler/          # 定时任务
│   ├── trader/             # 交易网关(QMT)
│   └── indicators/         # MyTT 指标库
├── database/               # DuckDB 管理
├── static/                 # 前端
├── data/                   # 行情数据(Parquet)
├── output/                 # 输出(state.json等)
├── scripts/                # 工具脚本
└── tests/                  # 测试
```

---

## 注意事项

1. **行情优先级**：QMT 实时 > 腾讯 HTTP > Parquet 历史，不要跳级
2. **state.json 保护**：运行态唯一真相源，灌数/回测输出到 `imports/` 子目录
3. **今日盈亏口径**：当日买入用买入价，过夜持仓用昨收价
4. **回测引擎不可比**：4 种引擎成本/T+1 各写各的，结果不能横向对比
5. **Windows 部署**：本平台运行在 Windows 上，通过 QMT 连接券商，不使用 Docker
