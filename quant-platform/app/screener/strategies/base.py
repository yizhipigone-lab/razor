"""
策略基类 (Pandas 驱动) — 候选⑤
统一前置过滤流水线(ST/退市/北交所/停牌/涨停);子类 generate_signals 接收已过滤的 bars。
涨停阈值表 LIMIT_TABLE 默认 = panzheng 的完整版(688/300/301=0.199, 8/4=0.295);
ma5_angle/ma5_angle_tdx_v2 覆盖为 0.195 保留旧行为。
"""
from abc import ABC, abstractmethod
from typing import Optional

import pandas as pd


def preprocess_bars(
    bars: pd.DataFrame,
    params: dict,
    limit_table: Optional[dict] = None,
    limit_main_pct: float = 0.099,
) -> pd.DataFrame:
    """单一过滤流水线(自由函数,引擎/类方法共用)。

    params 开关:filter_st / filter_bj / filter_suspend / skip_limit_up(均默认 True);
    filter_bj_pattern(默认 r'^[84]\d{5}',ma5_angle_cross 覆盖为 r'^8' 保留旧行为)。
    涨停:按 limit_table 匹配 code 前缀(688/300/301/8/4),主板用 limit_main_pct;
    skip_limit_up=True 时直接丢弃涨停行(per-code groupby shift 计算日收益)。
    """
    if bars is None or bars.empty:
        return bars
    df = bars
    p = params or {}
    _limit_table = limit_table if limit_table is not None else {
        "688": 0.199, "300": 0.199, "301": 0.199, "8": 0.295, "4": 0.295,
    }
    bj_pattern = p.get("filter_bj_pattern", r"^[84]\d{5}")
    if p.get("filter_st", True) and "name" in df.columns:
        df = df[~df["name"].str.contains("ST|退", na=False, case=False)]
    if p.get("filter_bj", True) and "code" in df.columns:
        df = df[~df["code"].astype(str).str.match(bj_pattern)]
    if p.get("filter_suspend", True) and "volume" in df.columns:
        df = df[df["volume"].fillna(0) > 0]
    if p.get("skip_limit_up", True) and "close" in df.columns and "code" in df.columns:
        prev_close = df.groupby("code")["close"].shift(1)
        daily_ret = df["close"] / prev_close - 1
        limit_pct = pd.Series(float(limit_main_pct), index=df.index, dtype=float)
        codes = df["code"].astype(str)
        for pfx, lp in _limit_table.items():
            limit_pct[codes.str.startswith(pfx)] = lp
        limit_up = daily_ret >= limit_pct
        df = df[~limit_up.fillna(False)]
    return df


class BaseStrategy(ABC):
    """
    选股策略基类。子类实现 generate_signals(),接收全市场 K 线宽表(Pandas),返回信号结果。
    bars 已被引擎 preprocess 过(过滤 ST/退/北交/停牌/涨停)。
    """

    name: str = "BaseStrategy"
    description: str = ""
    # 涨停阈值表(子类可覆盖;ma5_angle/ma5_angle_tdx_v2 覆盖为 0.195 保留旧行为)
    LIMIT_TABLE: dict = {
        "688": 0.199, "300": 0.199, "301": 0.199, "8": 0.295, "4": 0.295,
    }
    LIMIT_MAIN_PCT: float = 0.099

    def __init__(self, params: dict = None):
        self.params = params or self.default_params()

    def default_params(self) -> dict:
        return {}

    def preprocess(self, bars: pd.DataFrame) -> pd.DataFrame:
        """统一前置过滤(ST/退市/北交所/停牌日/涨停) — 委托 preprocess_bars。"""
        return preprocess_bars(bars, self.params or {}, self.LIMIT_TABLE, self.LIMIT_MAIN_PCT)

    @abstractmethod
    def generate_signals(self, bars: pd.DataFrame) -> pd.DataFrame:
        """输入全市场 K 线(已 preprocess 过滤);返回信号结果。"""
        raise NotImplementedError

    def get_info(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "params": self.params,
        }
