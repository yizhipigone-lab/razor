# A股量化交易平台 - 项目目录结构

```
p8/                           # 项目根目录
├── run.py                    # 主启动入口
├── requirements.txt          # 依赖列表
├── config/
│   └── app_setting.json      # 全局可配置参数（金额上限、止盈止损等）
├── app/                      # 自定义 VN.PY 插件 / App 模块
│   ├── __init__.py
│   ├── data_manager/         # 数据管理 App (下载、更新 A 股数据)
│   │   ├── __init__.py
│   │   ├── engine.py         # 数据下载/更新引擎
│   │   └── ui.py             # 数据管理 UI
│   ├── screener/             # 选股 App (VectorBT 加速)
│   │   ├── __init__.py
│   │   ├── engine.py         # 选股逻辑 & 策略注册
│   │   ├── strategies/       # 各类策略文件
│   │   │   ├── __init__.py
│   │   │   ├── base.py       # 策略基类
│   │   │   ├── ma_cross.py   # 均线策略
│   │   │   └── macd.py       # MACD 策略
│   │   └── ui.py             # 选股 UI
│   ├── monitor/              # 盘中实时监控 App
│   │   ├── __init__.py
│   │   ├── engine.py         # 3 分钟轮询 & 风控引擎
│   │   └── ui.py             # 监控面板 UI
│   ├── trader/               # 交易执行 App
│   │   ├── __init__.py
│   │   ├── engine.py         # 订单路由 & 金额计算器
│   │   ├── gateways/
│   │   │   ├── ths.py        # easytrader 同花顺接口
│   │   │   └── qmt.py        # XtQuant QMT 接口（预留）
│   │   └── ui.py             # 手工交易面板
│   └── backtest/             # 回测 App
│       ├── __init__.py
│       ├── engine.py         # 向量化回测引擎 (VectorBT)
│       └── ui.py             # 回测结果图表 UI
├── database/
│   ├── __init__.py
│   ├── duckdb_manager.py     # DuckDB + Parquet 持久化管理
│   └── models.py             # 数据模型定义
├── core/
│   ├── __init__.py
│   ├── event_engine.py       # 核心事件引擎 (VN.PY 兼容)
│   ├── settings.py           # 全局设置读取/写入
│   └── logger.py             # 审计日志模块
├── data/                     # 数据存储 (自动创建)
│   ├── parquet/              # K 线数据 Parquet 文件
│   │   ├── daily/            # 日线数据 by 股票代码
│   │   └── min5/             # 5分钟线数据 by 股票代码
│   └── meta/                 # 股票基础信息 (板块、题材等)
└── logs/                     # 日志文件 (自动创建，按日滚动)
```
