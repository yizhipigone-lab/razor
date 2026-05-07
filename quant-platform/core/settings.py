"""
全局配置管理模块 - 读取/写入 config/app_setting.json
所有可配置参数均从此模块统一获取，禁止在代码中使用硬编码数值。
"""
import json
import os
import threading
from pathlib import Path
from typing import Any

# 配置文件路径
ROOT_DIR = Path(__file__).parent.parent
CONFIG_FILE = ROOT_DIR / "config" / "app_setting.json"


class Settings:
    """全局设置单例，通过 settings.get() / settings.set() 访问"""

    _instance = None
    _data: dict = {}
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        """从文件加载配置"""
        with self._lock:
            if CONFIG_FILE.exists():
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
            else:
                self._data = {}

    def reload(self):
        """手动重新加载配置（设置面板保存后调用）"""
        self._load()

    def save(self):
        """将当前配置写入文件"""
        with self._lock:
            CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)

    def get(self, *keys: str, default: Any = None) -> Any:
        """
        递归读取嵌套键值，如 settings.get("trading", "max_buy_amount")
        """
        val = self._data
        for key in keys:
            if isinstance(val, dict):
                val = val.get(key)
            else:
                return default
            if val is None:
                return default
        return val

    def set(self, *keys_and_value, save: bool = True) -> None:
        """
        递归写入嵌套键值并可选持久化。
        用法: settings.set("trading", "max_buy_amount", 10000)
        save=False 可在批量更新时推迟持久化，最后手动调用 save()
        """
        *keys, value = keys_and_value
        with self._lock:
            d = self._data
            for key in keys[:-1]:
                d = d.setdefault(key, {})
            d[keys[-1]] = value
        if save:
            self.save()

    def get_all(self) -> dict:
        """返回完整配置字典（用于设置面板展示）"""
        return self._data.copy()

    # --- 便捷属性（常用参数直接访问）---

    @property
    def max_buy_amount(self) -> float:
        return float(self.get("trading", "max_buy_amount", default=8000))

    @property
    def order_lot_size(self) -> int:
        return int(self.get("trading", "order_lot_size", default=100))

    @property
    def trailing_activate_pct(self) -> float:
        """触发移动止盈的最低利润率（%）"""
        return float(self.get("risk", "trailing_stop_activate_pct", default=5.0))

    @property
    def trailing_drawdown_pct(self) -> float:
        """从历史最高价回落多少触发清仓（%）"""
        return float(self.get("risk", "trailing_stop_drawdown_pct", default=2.0))

    @property
    def hard_stop_loss_pct(self) -> float:
        """硬止损跌幅（%，负数）"""
        return float(self.get("risk", "hard_stop_loss_pct", default=-5.0))

    @property
    def breakeven_threshold_pct(self) -> float:
        """利润保护激活阈值（%）"""
        return float(self.get("risk", "breakeven_threshold_pct", default=2.0))

    @property
    def breakeven_stop_pnl_pct(self) -> float:
        """保本止损位（%，如+0.5%）"""
        return float(self.get("risk", "breakeven_stop_pnl_pct", default=0.5))

    @property
    def staged_take_profit(self) -> list:
        """分阶止盈配置列表"""
        return self.get("risk", "staged_take_profit", default=[])

    @property
    def time_exit_days(self) -> int:
        """持仓超过几天触发时间止损"""
        return int(self.get("risk", "time_exit_days", default=6))

    @property
    def time_exit_min_profit_pct(self) -> float:
        """时间止损：只要不低于此收益率则清仓（%）"""
        return float(self.get("risk", "time_exit_min_profit_pct", default=-3.0))

    @property
    def time_exit_force_days(self) -> int:
        """强制时间止损：超过此天数无条件清仓"""
        return int(self.get("risk", "time_exit_force_days", default=10))

    @property
    def poll_interval_seconds(self) -> int:
        """盘中行情轮询间隔（秒）"""
        return int(self.get("monitor", "poll_interval_seconds", default=180))

    @property
    def active_gateway(self) -> str:
        return self.get("gateway", "active_gateway", default="easytrader")

    @property
    def log_level(self) -> str:
        return self.get("log", "level", default="INFO")

    @property
    def auto_trade_enabled(self) -> bool:
        return bool(self.get("auto_trade", "enabled", default=False))
        
    @property
    def auto_trade_delay_seconds(self) -> int:
        return int(self.get("auto_trade", "delay_seconds", default=300))
        
    @property
    def auto_trade_max_amount(self) -> float:
        return float(self.get("auto_trade", "max_amount_per_stock", default=8000))
        
    @property
    def cron_enabled(self) -> bool:
        return bool(self.get("cron", "enabled", default=True))
        
    @property
    def cron_sync_times(self) -> list:
        val = self.get("cron", "sync_times")
        if isinstance(val, list):
            return val
        # 兼容旧版的单字符串配置
        legacy = self.get("cron", "sync_time")
        if legacy:
            return [str(legacy)]
        return ["17:00"]

    # ─── 热点板块配置 ────────────────────────────────────────

    @property
    def hot_sector_enabled(self) -> bool:
        return bool(self.get("hot_sector", "enabled", default=True))

    @property
    def hot_sector_refresh_minutes(self) -> int:
        return int(self.get("hot_sector", "refresh_minutes", default=5))

    @property
    def hot_sector_sector_weight(self) -> float:
        return float(self.get("hot_sector", "sector_weight", default=0.6))

    @property
    def hot_sector_concept_weight(self) -> float:
        return float(self.get("hot_sector", "concept_weight", default=0.4))

    @property
    def hot_sector_min_stocks(self) -> int:
        return int(self.get("hot_sector", "min_constituent_stocks", default=3))

    @property
    def hot_sector_redis_ttl(self) -> int:
        return int(self.get("hot_sector", "redis_ttl_seconds", default=600))

    @property
    def hot_sector_enable_ths(self) -> bool:
        return bool(self.get("hot_sector", "enable_ths_concepts", default=True))

    @property
    def hot_sector_enable_tushare(self) -> bool:
        return bool(self.get("hot_sector", "enable_tushare_concepts", default=True))

    # ── Optimizer ──────────────────────────────────────────

    @property
    def optimizer_search_space(self) -> dict:
        """AI 优化器搜索空间配置"""
        return self.get("optimizer", "search_space", default={})

    # ── Backtest ──────────────────────────────────────────

    @property
    def backtest_initial_capital(self) -> float:
        """回测初始资金（元）"""
        return float(self.get("backtest", "initial_capital", default=1_000_000))

    @property
    def backtest_position_size(self) -> float:
        """回测单票仓位上限（元）"""
        return float(self.get("backtest", "position_size", default=50_000))

    @property
    def backtest_use_portfolio(self) -> bool:
        """回测是否启用资金管理组合仿真"""
        return bool(self.get("backtest", "use_portfolio", default=True))

    @property
    def backtest_streak_pause(self) -> int:
        """连败保护：连续N笔亏损后暂停"""
        return int(self.get("backtest", "streak_pause", default=3))

    @property
    def backtest_pause_days(self) -> int:
        """连败暂停天数"""
        return int(self.get("backtest", "pause_days", default=2))

    @property
    def backtest_intraday_freq(self) -> str:
        """日内数据频率: min1 或 min5"""
        return str(self.get("backtest", "intraday_freq", default="min1"))

    @property
    def backtest_apply_costs(self) -> bool:
        """回测是否扣除交易成本"""
        return bool(self.get("backtest", "apply_costs", default=True))


# 全局单例
settings = Settings()


def calc_buy_volume(price: float, override_max_amount: float = None) -> int:
    """
    根据当前价格和系统金额上限，计算最大可买手数（100股整数倍）。
    如果单股价格超过金额上限，返回 0。
    """
    if price <= 0:
        return 0
    max_amount = override_max_amount if override_max_amount is not None else settings.max_buy_amount
    lot = settings.order_lot_size
    lots = int(max_amount / price / lot)
    return lots * lot

