"""实盘交易模块数据模型(v5.4 §6)

Pydantic 模型用于 API 接口;内部用 dataclass 高性能。
字段对应 live_trader.duckdb 表结构。
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import List, Optional

from pydantic import BaseModel, Field


# ===== 内部 dataclass(高性能,模块间传递)=====

@dataclass
class OrderIntent:
    """下单意图(闸门前)"""
    code: str
    direction: str  # "buy" / "sell"
    volume: int
    price: float = 0.0
    price_type: int = 11  # FIX_PRICE
    strategy_name: str = ""
    terminal: str = "SYS"  # WEB / TDX / SYS
    client_order_id: str = ""  # C3 幂等键
    reason: str = ""  # 策略原因(写入 order_remark)


@dataclass
class LiveOrder:
    """委托记录(对应 live_orders 表)"""
    order_id: int
    client_order_id: str
    code: str
    direction: str  # buy/sell
    volume: int
    price: float
    price_type: int
    status: int  # 48-57+255
    status_msg: str = ""
    seq: int = 0
    mode: str = "dry-run"  # dry-run / live(v5.3)
    strategy_name: str = ""
    order_remark: str = ""
    terminal: str = "SYS"
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass
class LiveDeal:
    """成交流水(对应 live_deals 表)"""
    trade_id: int
    order_id: int
    code: str
    direction: str
    filled_volume: int
    filled_price: float
    filled_amount: float
    commission: float = 0.0
    mode: str = "dry-run"
    traded_at: Optional[datetime] = None


@dataclass
class LivePosition:
    """持仓(对应 live_positions 表)"""
    code: str
    volume: int = 0
    can_use_volume: int = 0
    frozen_volume: int = 0
    pending_buy_volume: int = 0  # C1 在途买入预扣
    avg_cost: float = 0.0
    last_price: float = 0.0
    market_value: float = 0.0
    float_profit: float = 0.0
    profit_rate: float = 0.0
    peak_price: float = 0.0
    sell_count: int = 0
    entry_date: Optional[date] = None
    managed: bool = True  # §3.3.1 false=保留持仓(ETF),true=策略持仓
    strategy_name: str = ""


@dataclass
class ExitAction:
    """离场动作(复用 exit_rules.ExitSignal,适配实盘)"""
    code: str
    trigger_type: str  # 硬止损/保本/...
    sell_pct: float  # 0-100
    priority: int
    note: str = ""
    reason: str = ""


# ===== Pydantic API 模型(对外接口)=====

class OrderRequest(BaseModel):
    """下单请求(API)"""
    code: str
    direction: str = Field(..., pattern="^(buy|sell)$")
    volume: int = Field(..., gt=0)
    price: float = Field(0.0, ge=0)
    price_type: int = 11
    strategy_name: str = ""
    terminal: str = "WEB"


class OrderResponse(BaseModel):
    """下单响应"""
    ok: bool
    order_id: Optional[int] = None
    client_order_id: str
    status: str = ""
    reason: str = ""


class PositionView(BaseModel):
    """持仓视图(前端)"""
    code: str
    name: str = ""
    volume: int
    can_use_volume: int
    avg_cost: float
    last_price: float
    market_value: float
    float_profit: float
    profit_rate: float
    managed: bool
    today_pnl: Optional[float] = None  # 今日盈亏(当日买入按买入价,过夜按昨收)


class AssetView(BaseModel):
    """资产视图"""
    cash: float
    frozen_cash: float
    market_value: float  # 仅 managed=true 部分
    total_asset: float
    live_capital: float
    managed_position_value: float  # 策略持仓市值


class GateStatus(BaseModel):
    """闸门状态"""
    gate: int
    name: str
    active: bool  # 是否检查
    passed: bool
    threshold: str = ""
    current: str = ""
    detail: str = ""


class KillSwitchStatus(BaseModel):
    """kill switch 状态"""
    activated: bool
    reason: str = ""
    activated_at: Optional[datetime] = None
    source: str = ""


class ReconcileResult(BaseModel):
    """对账结果"""
    timestamp: str
    code: str
    local_volume: int
    qmt_volume: int
    diff_volume: int
    diff_value: float
    level: str  # INFO / WARN / CRITICAL
    managed: bool


# ===== 数据同步请求模型(从 qmt_proxy 迁移) =====

class SyncIntraRequest(BaseModel):
    """分时数据同步请求"""
    freq: str = "5m"
    days: int = 30
    start_date: Optional[str] = None
    end_date: Optional[str] = None


class SyncIndexDailyRequest(BaseModel):
    """指数日线同步请求"""
    start_date: Optional[str] = None
    end_date: Optional[str] = None


# ===== 信号桥接请求模型(v1.2.2 §5.2) =====

class SignalItem(BaseModel):
    """单个买入信号"""
    code: str               # 股票代码(支持 600000 或 600000.SH)
    price: float = 0.0      # Docker 端快照价格(会被 Windows QMT 实时价覆盖)
    ts: str = ""            # 信号时间戳


class BuySignalRequest(BaseModel):
    """买入信号批量请求(Docker → Windows)"""
    signals: List[SignalItem]
    strategy: str = "QUANTQQ"
    source: str = "TDX"     # 信号来源


class SignalResult(BaseModel):
    """单个信号处理结果"""
    code: str
    ok: bool
    status: str = ""        # submitted / risk_rejected / locked / duplicate / error
    reason: str = ""
    order_id: Optional[int] = None


class BuySignalResult(BaseModel):
    """买入信号批量响应"""
    accepted: List[str] = []        # 接受的 code 列表
    rejected: List[str] = []        # 拒绝的 code 列表
    details: List[SignalResult] = []  # 每只信号详情
