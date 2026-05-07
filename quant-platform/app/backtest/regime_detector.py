"""
市场状态（Regime）检测器
根据已有的本地日线数据，为历史每个交易日打上量化的市场状态标签。
LLM 无需猜测历史，由此模块计算结果后再提供给 LLM 做统计解读。

四象限分类：
  牛市-低波 / 牛市-高波 / 熊市-低波 / 熊市-高波
"""
import os
import sys
from pathlib import Path
from datetime import date, timedelta
from typing import Dict, Optional

import pandas as pd
import numpy as np

ROOT_DIR = Path(__file__).parent.parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from core.logger import get_logger

log = get_logger("RegimeDetector")

# ── Regime 基准指数配置 ──────────────────────────────────────
# 主基准：中证A500（全市场代表性最强，兼顾大中小盘和行业均衡）
# 降级顺序：中证A500 → 沪深300 → 上证综指 → 深证成指 → 中证500
REGIME_INDEX_PRIORITY = [
    ("index_000510", "中证A500"),  # ★ 主基准（全市场/行业均衡）
    ("index_000300", "沪深300"),   # 第一降级（蓝筹/大盘）
    ("index_000001", "上证综指"),  # 第二降级（全市场情绪）
    ("index_399001", "深证成指"),  # 第三降级
    ("index_000905", "中证500"),   # 第四降级（中盘）
    ("index_000852", "中证1000"),  # 第五降级（小盘）
    ("index_399006", "创业板指"),  # 第六降级（成长/高波）
    ("index_000016", "上证50"),    # 第七降级（超大盘）
    ("index_000688", "科创50"),    # 第八降级（科技）
    ("index_000985", "中证全指"),  # 第九降级（全市场）
]
PARQUET_DIR = ROOT_DIR / "data" / "parquet" / "daily"

# 指数额定价格下界（用于识别误读股票的情况）
# 沪深300 价格应在 2000+ 点，上证综指 2000+ 点，均远大于此阈值
INDEX_PRICE_MIN = 300.0



class RegimeDetector:
    """
    市场状态检测器（单例缓存）
    """

    def __init__(self):
        self._regime_cache: Dict[str, str] = {}  # date_str -> regime_label
        self._loaded = False

    def _load_index_data(self, start: date, end: date) -> pd.DataFrame:
        """
        按优先级读取基准指数数据：沪深300 > 上证综指 > 深证成指 > 全市场均值
        同时进行价格合理性校验，防止误读股票 Parquet。
        """
        for file_stem, name in REGIME_INDEX_PRIORITY:
            index_path = PARQUET_DIR / f"{file_stem}.parquet"
            if not index_path.exists():
                log.debug(f"Regime | {name}（{file_stem}.parquet）不存在，尝试下一个")
                continue
            try:
                df = pd.read_parquet(index_path)
                df["date"] = pd.to_datetime(df["date"]).dt.date
                df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
                if df.empty:
                    continue
                # 价格合理性校验：指数应远大于 INDEX_PRICE_MIN（300点）
                median_close = df["close"].median()
                if median_close < INDEX_PRICE_MIN:
                    log.warning(
                        f"Regime | {name} 中位收盘价 {median_close:.1f} < {INDEX_PRICE_MIN}，"
                        f"可能是股票数据而非指数，跳过"
                    )
                    continue
                log.info(f"Regime | 使用 {name}（{len(df)} 条，中位值 {median_close:.0f} 点）")
                return df
            except Exception as e:
                log.warning(f"Regime | 读取 {name} 失败: {e}")
                continue

        # 全部失败：用全量股票均值代理
        log.warning("Regime | 无可用指数数据，改用全市场均值代理（精度较低）...")
        try:
            proxy_files = list(PARQUET_DIR.glob("*.parquet"))[:100]
            frames = []
            for f in proxy_files:
                try:
                    tmp = pd.read_parquet(f)[["date", "close"]]
                    tmp["date"] = pd.to_datetime(tmp["date"]).dt.date
                    tmp = tmp[(tmp["date"] >= start) & (tmp["date"] <= end)]
                    tmp = tmp.rename(columns={"close": f.stem})
                    frames.append(tmp.set_index("date"))
                except Exception:
                    continue
            if not frames:
                return pd.DataFrame()
            combined = pd.concat(frames, axis=1).mean(axis=1).reset_index()
            combined.columns = ["date", "close"]
            combined = combined.sort_values("date")
            return combined
        except Exception as e:
            log.error(f"Regime | 代理计算失败: {e}")
            return pd.DataFrame()

    def compute_regime_map(
        self,
        start: date,
        end: date,
        ma_window: int = 60,
        vol_window: int = 20,
        vol_threshold: float = 0.015,
    ) -> Dict[str, str]:
        """
        为 [start, end] 区间的每个交易日生成 Regime 标签。

        Returns:
            {date_str: regime_label}
            regime_label ∈ {'牛市-低波', '牛市-高波', '熊市-低波', '熊市-高波', '未知'}
        """
        # 向前多取 ma_window + vol_window 天的数据以保证指标计算稳定
        data_start = start - timedelta(days=(ma_window + vol_window) * 2)
        df = self._load_index_data(data_start, end)

        if df.empty:
            log.warning("Regime | 无法获取参考数据，所有 Regime 标记为「未知」")
            # 返回全 "未知"，LLM 会据此降低置信度
            result = {}
            cur = start
            while cur <= end:
                result[str(cur)] = "未知"
                cur += timedelta(days=1)
            return result

        df = df.sort_values("date").copy()
        df["ma60"] = df["close"].rolling(ma_window, min_periods=max(1, ma_window // 2)).mean()
        df["vol20"] = (
            df["close"].pct_change().rolling(vol_window, min_periods=max(1, vol_window // 2)).std()
        )

        result = {}
        for _, row in df.iterrows():
            d = row["date"]
            if d < start or d > end:
                continue
            close = row["close"]
            ma60 = row["ma60"]
            vol = row["vol20"]

            # 趋势判断
            if pd.isna(ma60) or pd.isna(close):
                trend = "未知"
            elif close > ma60:
                trend = "牛市"
            else:
                trend = "熊市"

            # 波动判断
            if pd.isna(vol):
                vol_label = "低波"
            elif vol > vol_threshold:
                vol_label = "高波"
            else:
                vol_label = "低波"

            label = "未知" if trend == "未知" else f"{trend}-{vol_label}"
            result[str(d)] = label

        log.info(
            f"Regime | 生成完毕 {len(result)} 条标签 | "
            f"分布: { {k: list(result.values()).count(k) for k in set(result.values())} }"
        )
        self._regime_cache.update(result)
        return result

    def get_regime(self, d: date) -> str:
        """快捷查询单日 Regime，依赖 compute_regime_map 已被调用过。"""
        return self._regime_cache.get(str(d), "未知")

    def summarize_trades_by_regime(self, trades: list, regime_map: Dict[str, str]) -> dict:
        """
        将回测交易按入场 Regime 分类统计，返回给 LLM 的结构化数据。
        """
        from collections import defaultdict

        grouped = defaultdict(list)
        for t in trades:
            buy_date = str(t.get("buy_date", ""))[:10]  # 只取日期部分
            regime = regime_map.get(buy_date, "未知")
            grouped[regime].append(t["pnl_pct"])

        summary = {}
        for regime, pnls in grouped.items():
            if not pnls:
                continue
            wins = [p for p in pnls if p > 0]
            summary[regime] = {
                "count": len(pnls),
                "avg_pnl": round(float(np.mean(pnls)), 3),
                "win_rate": round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
                "max_pnl": round(float(max(pnls)), 3),
                "min_pnl": round(float(min(pnls)), 3),
            }
        return summary


# 全局单例
regime_detector = RegimeDetector()
