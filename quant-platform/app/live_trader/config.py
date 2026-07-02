"""实盘交易模块配置(v5.4 §5.6 / §18.9 / §3.3.1)

风控参数全部新建在 live_trader/config.py + app_setting.json 的 live_trader 段。
启动时 schema 校验非零(fail-fast)。
"""
import os
from dataclasses import dataclass, field
from typing import List

from core.settings import settings


@dataclass(frozen=True)
class LiveTraderConfig:
    """实盘配置(不可变,启动时加载一次)"""

    # ===== QMT 连接(从环境变量读,不硬编码账号)=====
    qmt_userdata_path: str = r"D:\Program Files\XCXT\userdata_mini"
    qmt_account_id: str = ""  # 从 QMT_ACCOUNT_ID 环境变量读
    qmt_call_timeout_sec: float = 3.0  # 所有 xtquant 调用超时(v5.1 §10.2)

    # ===== 资金与模式 =====
    live_capital: float = 100000.0  # LIVE_CAPITAL,默认 10 万(用户待确认)
    mode: str = "dry-run"  # dry-run / live(v5.3 mode 字段方案)

    # ===== 持仓接管(§3.3.1 ETF 保留决策)=====
    preserved_codes: List[str] = field(default_factory=lambda: ["159226.SZ", "159290.SZ"])
    # managed=false 的保留持仓:不买卖、不参与 exit_rules、不计风控、对账偏差只告警

    # ===== RiskGate 9 闸门(§5.6,按 LIVE_CAPITAL 比例)=====
    max_single_trade_pct: float = 0.20        # 闸门1 单笔 ≤20%
    cash_reserve_pct: float = 0.10            # 闸门2 买入后现金 ≥10%
    max_position_pct: float = 0.30            # 闸门3 单只 ≤30%(含在途预扣)
    max_total_position_pct: float = 0.90      # 闸门4 总仓 ≤90%(含在途预扣)
    daily_loss_halt_pct: float = 0.03         # 闸门5a 日亏 3% 禁buy
    max_single_loss_pct: float = 0.05         # 闸门5b 单笔浮亏 5% 禁该只再买
    max_consecutive_rejections: int = 5       # 闸门7 连续5次risk/broker拒→kill
    rejection_window_sec: float = 300.0       # 闸门7 5分钟时间窗(H4)

    # ===== 清仓锁(§5.4)=====
    clearance_lock_ttl_sec: float = 300.0     # TTL 300s 兜底
    clearance_lock_max_renew: int = 2         # 最多续2次(尾盘缩短,§19.5 M2)
    clearance_lock_eod_ttl_sec: float = 120.0  # 尾盘(≥14:50)TTL 120s

    # ===== 时点(§7.3)=====
    sell_phase_start: str = "14:50"           # sell_phase 触发(比 sim_trader 14:53 提前)
    no_new_order_after: str = "14:55"         # 只撤不挂
    force_market_after: str = "14:57"         # 强制市价清仓(未跌停才用)

    # ===== 熔断器(§5.2)=====
    circuit_breaker_max_failures: int = 3     # 连续3次失败→熔断
    circuit_breaker_timeout_sec: float = 30.0  # 熔断30s
    restart_backoff_max_sec: float = 1800.0   # 指数退避上限
    restart_max_retries: int = 30             # §19.4 重连最大次数
    restart_max_duration_sec: float = 7200.0  # 累计2小时

    # ===== DuckDB 两层 buffer(§18.6)=====
    buffer_maxlen: int = 2000                 # L1 deque maxlen
    buffer_flush_interval_ms: float = 100.0   # L2 flusher 100ms
    wal_path: str = "data/live_trader/deals.wal"  # 终态同步落盘 WAL(H2)

    # ===== 对账(§5.9 / §19.4)=====
    reconcile_times: List[str] = field(default_factory=lambda: ["09:35", "11:30", "14:55", "15:05"])
    reconcile_diff_min_shares: int = 100      # 最小绝对值
    reconcile_diff_pct: float = 0.005         # 市值0.5%
    reconcile_critical_pct: float = 0.10      # 市值10% CRITICAL

    # ===== 通知(§10 / §19.5 M4)=====
    wework_webhook: str = ""  # 从 app_setting.json 读
    multi_channel_alert: bool = True  # CRITICAL 多通道(企业微信+桌面弹窗+日志标红)

    # ===== 路径 =====
    db_path: str = "data/live_trader/live_trader.duckdb"
    lock_file: str = "data/live_trader/live.lock"  # §16.8 启动互斥
    restart_counter_file: str = "data/live_trader/restart_counter.json"

    # ===== 信号桥接(v1.2.2 §5.2) =====
    buy_position_size: float = 10000.0       # 单只买入软上限(元),闸门1是硬上限
    buy_signal_token: str = ""               # Bearer token 鉴权(从 app_setting.json 读)
    buy_signal_enabled: bool = True          # 冗余开关(buy-signal 端点检查)
    buy_signal_cutoff: str = "14:59"         # buy-signal 截止时点(14:59 后拒收)


def load_config() -> LiveTraderConfig:
    """加载实盘配置(从环境变量 + app_setting.json live_trader 段)

    优先级:环境变量 > app_setting.json > 默认值。
    启动时校验关键参数非零(fail-fast,§16.4)。
    """
    # 环境变量优先(QMT 账号/mode/资金只从环境变量读,不写文件)
    qmt_account_id = os.environ.get("QMT_ACCOUNT_ID", "")
    qmt_userdata_path = os.environ.get("QMT_USERDATA_PATH", r"D:\Program Files\XCXT\userdata_mini")

    # mode 和 live_capital 也支持环境变量(防 app_setting.json 被主 API 覆盖)
    env_mode = os.environ.get("LIVE_TRADER_MODE", "")
    env_capital = os.environ.get("LIVE_TRADER_CAPITAL", "")

    # 从 app_setting.json live_trader 段读(可选)
    cfg_dict = settings.get("live_trader", default={}) or {}

    # 通知 webhook 从单独段读
    wework_webhook = settings.get("notify", "wework_webhook", default="") or ""

    # 合并:环境变量 > JSON > 默认值
    mode = env_mode or cfg_dict.get("mode", "dry-run")
    live_capital = float(env_capital) if env_capital else float(cfg_dict.get("live_capital", 100000.0))

    config = LiveTraderConfig(
        qmt_userdata_path=qmt_userdata_path,
        qmt_account_id=qmt_account_id,
        live_capital=live_capital,
        mode=mode,
        preserved_codes=cfg_dict.get("preserved_codes", ["159226.SZ", "159290.SZ"]),
        max_single_trade_pct=float(cfg_dict.get("max_single_trade_pct", 0.20)),
        cash_reserve_pct=float(cfg_dict.get("cash_reserve_pct", 0.10)),
        max_position_pct=float(cfg_dict.get("max_position_pct", 0.30)),
        max_total_position_pct=float(cfg_dict.get("max_total_position_pct", 0.90)),
        daily_loss_halt_pct=float(cfg_dict.get("daily_loss_halt_pct", 0.03)),
        max_single_loss_pct=float(cfg_dict.get("max_single_loss_pct", 0.05)),
        wework_webhook=wework_webhook,
        buy_position_size=float(cfg_dict.get("buy_position_size", 10000.0)),
        buy_signal_token=cfg_dict.get("buy_signal_token", ""),
        buy_signal_enabled=cfg_dict.get("buy_signal_enabled", True),
        buy_signal_cutoff=cfg_dict.get("buy_signal_cutoff", "14:59"),
    )

    # fail-fast 校验(§16.4)
    _validate_config(config)
    return config


def _validate_config(config: LiveTraderConfig) -> None:
    """启动自检:关键参数非零(fail-fast)"""
    errors = []
    if not config.qmt_account_id:
        errors.append("QMT_ACCOUNT_ID 环境变量未设置")
    if config.live_capital <= 0:
        errors.append("live_capital 必须 > 0")
    if config.max_single_trade_pct <= 0 or config.max_single_trade_pct > 1:
        errors.append("max_single_trade_pct 必须在 (0,1]")
    if config.max_position_pct <= 0 or config.max_total_position_pct <= 0:
        errors.append("max_position_pct / max_total_position_pct 必须 > 0")
    if config.daily_loss_halt_pct <= 0:
        errors.append("daily_loss_halt_pct 必须 > 0")
    if config.mode not in ("dry-run", "live"):
        errors.append(f"mode 必须是 dry-run 或 live,当前 {config.mode}")

    if errors:
        msg = "实盘配置校验失败(fail-fast):\n  - " + "\n  - ".join(errors)
        raise ValueError(msg)
