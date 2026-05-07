"""
LLM 顾问层 (llm_advisor.py)
使用 DeepSeek API（OpenAI 兼容接口）作为量化研究员，
负责三次关键 LLM 调用：
  Call ① 冷启动：读策略代码 → 设计搜索空间
  Call ② 探索后：解读初步结果 → 精化搜索空间
  Call ③ 最终：生成中文报告 + 实盘推荐
"""
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from core.logger import get_logger

log = get_logger("LLMAdvisor")

# 最优宽默认搜索空间（LLM 失败时的降级方案）
FALLBACK_SEARCH_SPACE = {
    "hard_stop_loss_pct":      {"min":  -8.0, "max":  -2.0},
    "breakeven_threshold_pct": {"min":   3.0, "max":   6.0},
    "breakeven_stop_pnl_pct":  {"min":   0.0, "max":   2.0},
    "trailing_activate_pct":   {"min":   5.0, "max":  15.0},
    "trailing_drawdown_pct":   {"min":   2.0, "max":   5.0},
    "time_exit_days":          {"min":   7,   "max":  25  },
    "tp1_profit":              {"min":   6.0, "max":  12.0},
    "tp1_ratio":               {"min":   0.2, "max":   0.4},
    "tp2_profit":              {"min":  12.0, "max":  20.0},
    "tp2_ratio":               {"min":   0.2, "max":   0.4},
    "tp3_profit":              {"min":  18.0, "max":  30.0},
    "tp3_ratio":               {"min":   0.2, "max":   0.4},
}

# 参数绝对边界（防止 LLM 设计出不合理的范围）
PARAM_HARD_BOUNDS = {
    "hard_stop_loss_pct":      (-10.0, -2.0),
    "breakeven_threshold_pct": (  1.0,  8.0),
    "breakeven_stop_pnl_pct":  (  0.0,  3.0),
    "trailing_activate_pct":   (  3.0, 20.0),
    "trailing_drawdown_pct":   (  1.0,  8.0),
    "time_exit_days":          (  3,   30  ),
    "tp1_profit":              (  3.0, 15.0),
    "tp1_ratio":               (  0.1,  0.5),
    "tp2_profit":              (  8.0, 25.0),
    "tp2_ratio":               (  0.1,  0.5),
    "tp3_profit":              ( 15.0, 35.0),
    "tp3_ratio":               (  0.1,  0.5),
}


def _normalize_space(space: dict) -> dict:
    """
    规范化 LLM 返回的搜索空间：
    - 将 low/high/minimum/maximum 等别名统一为 min/max
    - 过滤掉非字典类型的参数值
    """
    result = {}
    alias_min = ("min", "low", "minimum", "lower", "from", "start")
    alias_max = ("max", "high", "maximum", "upper", "to", "end")
    for key, bounds in space.items():
        if not isinstance(bounds, dict):
            log.warning(f"LLMAdvisor | 忽略非字典参数 {key}: {bounds}")
            continue
        mn = next((bounds[k] for k in alias_min if k in bounds), None)
        mx = next((bounds[k] for k in alias_max if k in bounds), None)
        if mn is None or mx is None:
            log.warning(f"LLMAdvisor | 参数 {key} 缺少 min/max，跳过: {bounds}")
            continue
        result[key] = {"min": mn, "max": mx}
    return result


def _clip_space(space: dict) -> dict:
    """将 LLM 设计的搜索空间裁剪到硬边界内，防止越界"""
    # 先规范化键名（防止 low/high 等别名导致 KeyError）
    space = _normalize_space(space)
    result = {}
    for key, bounds in space.items():
        hard = PARAM_HARD_BOUNDS.get(key)
        if not hard:
            result[key] = bounds
            continue
        lo, hi = hard
        raw_min = float(bounds.get("min", lo))
        raw_max = float(bounds.get("max", hi))
        
        # 基础裁剪
        bm = max(lo, min(hi, raw_min))
        bx = max(lo, min(hi, raw_max))
        if bm > bx: bm, bx = bx, bm  # 纠正倒置
        
        result[key] = {"min": bm, "max": bx}

    # 逻辑联动：tp2 > tp1, tp3 > tp2
    if "tp1_profit" in result and "tp2_profit" in result:
        tp1_max = result["tp1_profit"]["max"]
        tp2_min = result["tp2_profit"]["min"]
        if tp2_min <= tp1_max:
            result["tp2_profit"]["min"] = tp1_max + 2.0
            if result["tp2_profit"]["min"] >= result["tp2_profit"]["max"]:
                result["tp2_profit"]["max"] = result["tp2_profit"]["min"] + 10.0

    if "tp2_profit" in result and "tp3_profit" in result:
        tp2_max = result["tp2_profit"]["max"]
        tp3_min = result["tp3_profit"]["min"]
        if tp3_min <= tp2_max:
            result["tp3_profit"]["min"] = tp2_max + 3.0
            if result["tp3_profit"]["min"] >= result["tp3_profit"]["max"]:
                result["tp3_profit"]["max"] = result["tp3_profit"]["min"] + 12.0
    
    return result


def _extract_json(text: str) -> Optional[dict]:
    """从 LLM 输出中鲁棒地提取第一个 JSON 对象"""
    # 优先尝试 ```json ... ``` 代码块
    block = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if block:
        try:
            return json.loads(block.group(1).strip())
        except Exception:
            pass
    # 退而求其次：寻找第一个 { ... }
    brace = re.search(r"\{[\s\S]*\}", text)
    if brace:
        try:
            return json.loads(brace.group())
        except Exception:
            pass
    return None


class LLMAdvisor:
    """
    DeepSeek LLM 调用封装。
    所有调用都有超时 + 降级保护，LLM 不可用时系统继续运行。
    """

    def __init__(
        self,
        use_llm: bool = True,
        model: str = "deepseek-v4-pro",
        timeout: int = 30,
    ):
        self.use_llm = use_llm
        self.model = model
        self.timeout = timeout
        self._client = None

        if use_llm:
            self._init_client()

    def _init_client(self):
        """初始化 OpenAI 兼容的 DeepSeek 客户端"""
        try:
            from openai import OpenAI
            from dotenv import load_dotenv

            load_dotenv()
            api_key = os.getenv("DEEPSEEK_API_KEY", "")
            if not api_key:
                log.warning("LLMAdvisor | DEEPSEEK_API_KEY 未配置，LLM 功能将降级")
                self.use_llm = False
                return

            self._client = OpenAI(
                api_key=api_key,
                base_url="https://api.deepseek.com",
            )
            log.info(f"LLMAdvisor | 已连接 DeepSeek ({self.model})")
        except Exception as e:
            log.warning(f"LLMAdvisor | 初始化失败，降级运行: {e}")
            self.use_llm = False

    def _call(self, system_prompt: str, user_prompt: str, max_tokens: int = 2048) -> str:
        """核心 LLM 调用，带超时保护"""
        if not self.use_llm or not self._client:
            return ""
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=0.3,
                timeout=self.timeout,
            )
            return resp.choices[0].message.content or ""
        except Exception as e:
            err_type = type(e).__name__
            err_msg = str(e)[:200]
            log.warning(f"LLMAdvisor | API 调用异常 [{err_type}]: {err_msg}")
            return ""

    # ─────────────────────────────────────────────────────
    # Call ① 冷启动：读策略代码 → 设计初始搜索空间
    # ─────────────────────────────────────────────────────
    def design_search_space(self, strategy_code: str) -> dict:
        """
        让 LLM 阅读策略源码，推断合理的参数搜索范围。
        失败时降级为 FALLBACK_SEARCH_SPACE。
        """
        if not self.use_llm:
            log.info("LLMAdvisor | Call① 降级：使用默认搜索空间")
            return FALLBACK_SEARCH_SPACE.copy()

        system = (
            "你是一位资深量化策略分析师，专注于 A 股市场。"
            "你熟悉趋势跟踪、均线策略、止盈止损机制。"
            "请严格按照要求输出 JSON，不要输出任何其他内容。"
        )

        param_desc = "\n".join(
            f"- {k}: {v} (绝对范围: {PARAM_HARD_BOUNDS[k]})"
            for k, v in {
                "hard_stop_loss_pct":      "硬止损百分比（负数，如-7表示跌7%止损）",
                "breakeven_threshold_pct": "触发利润保卫的盈利阈值（%）",
                "breakeven_stop_pnl_pct":  "利润保卫后的止损位（%，如0表示保本止损）",
                "trailing_activate_pct":   "回落止盈激活的最低盈利（%）",
                "trailing_drawdown_pct":   "从最高点回落多少触发止盈（%）",
                "time_exit_days":          "强制持仓上限天数（整数，无条件离场）",
                "tp1_profit":              "第一档止盈触发线（%）",
                "tp1_ratio":               "第一档止盈卖出比例（0到1）",
                "tp2_profit":              "第二档止盈触发线（%，需大于tp1）",
                "tp2_ratio":               "第二档止盈卖出比例（0到1）",
                "tp3_profit":              "第三档止盈触发线（%，需大于tp2，触发后清仓）",
                "tp3_ratio":               "第三档止盈卖出比例（0到1，通常为剩余全部）",
            }.items()
        )

        user = f"""请分析以下策略代码，为每个参数设计合理的搜索范围。

【策略代码】
```python
{strategy_code[:3000]}
```

【需要设计范围的参数】
{param_desc}

请基于策略逻辑（均线类型、选股条件等），推断参数应该偏大还是偏小，输出如下 JSON：

```json
{{
  "hard_stop_loss_pct":      {{"min": -8.0, "max": -3.0}},
  "breakeven_threshold_pct": {{"min":  3.0, "max":  7.0}},
  "breakeven_stop_pnl_pct":  {{"min":  0.0, "max":  2.0}},
  "trailing_activate_pct":   {{"min": 10.0, "max": 20.0}},
  "trailing_drawdown_pct":   {{"min":  3.0, "max":  7.0}},
  "time_exit_days":          {{"min": 15,   "max": 40  }},
  "tp1_profit":              {{"min":  8.0, "max": 15.0}},
  "tp1_ratio":               {{"min":  0.2, "max":  0.4}},
  "tp2_profit":              {{"min": 15.0, "max": 25.0}},
  "tp2_ratio":               {{"min":  0.2, "max":  0.4}},
  "tp3_profit":              {{"min": 25.0, "max": 35.0}},
  "tp3_ratio":               {{"min":  0.3, "max":  0.4}}
}}
```

同时简述你的推理依据（200字以内）。"""

        log.info("LLMAdvisor | Call① 开始：冷启动搜索空间设计...")
        t0 = time.time()
        raw = self._call(system, user, max_tokens=1200)
        log.info(f"LLMAdvisor | Call① 完成 ({time.time()-t0:.1f}s)")

        if not raw:
            log.warning("LLMAdvisor | Call① 无响应，使用默认空间")
            return FALLBACK_SEARCH_SPACE.copy()

        space = _extract_json(raw)
        if not space:
            log.warning("LLMAdvisor | Call① JSON 解析失败，使用默认空间")
            return FALLBACK_SEARCH_SPACE.copy()

        # 裁剪到硬边界
        space = _clip_space(space)
        # 补充 LLM 遗漏的参数（以 fallback 填充）
        for key, val in FALLBACK_SEARCH_SPACE.items():
            if key not in space:
                space[key] = val

        log.info(f"LLMAdvisor | Call① 成功：{len(space)} 个参数范围已设计")
        return space

    # ─────────────────────────────────────────────────────
    # Call ② 探索结果分析 → 精化搜索空间
    # ─────────────────────────────────────────────────────
    def analyze_exploration(
        self,
        exploration_results: list,
        regime_summary: dict,
        current_space: dict,
    ) -> dict:
        """
        分析 12 组探索期回测结果，输出精化的搜索空间。
        失败时返回 current_space（不收窄，继续使用现有范围）。
        """
        if not self.use_llm or not exploration_results:
            return current_space

        # 构建结果摘要表格
        rows = []
        for r in sorted(exploration_results, key=lambda x: x.get("score", -999), reverse=True):
            rows.append(
                f"  score={r.get('score',0):.3f} | 平盈率={r.get('avg_pnl',0):.2f}% | "
                f"PF={r.get('profit_factor',0):.2f} | 胜率={r.get('win_rate',0):.1f}% | "
                f"最大回撤={r.get('max_dd',0):.2f}% | 笔数={r.get('n_trades',0)} | "
                f"止损={r.get('params',{}).get('hard_stop_loss_pct',0):.1f}% | "
                f"回落激活={r.get('params',{}).get('trailing_activate_pct',0):.1f}% | "
                f"TP1={r.get('params',{}).get('tp1_profit',0):.1f}%"
            )
        results_text = "\n".join(rows)

        regime_text = "\n".join(
            f"  {regime}: 平均盈亏={stats['avg_pnl']:.2f}% 胜率={stats['win_rate']:.1f}% 笔数={stats['count']}"
            for regime, stats in regime_summary.items()
        )

        system = "你是量化回测分析师。请基于数据给出精准、简洁的分析。严格输出 JSON。"
        user = f"""以下是探索阶段 {len(exploration_results)} 组参数回测结果（按 Calmar 得分排序）：

【回测结果】
{results_text}

【市场状态分析（按入场 Regime 分类）】
{regime_text}

【当前搜索空间】
{json.dumps(current_space, ensure_ascii=False, indent=2)}

请分析：
1. 哪些参数区域表现更好？
2. 哪些参数对得分影响最显著？
3. 基于数据，建议收窄哪些参数的搜索范围（收窄 30-50%）？

输出精化后的 JSON 搜索空间（同样格式，范围应比原来更窄）："""

        log.info("LLMAdvisor | Call② 开始：探索结果分析...")
        t0 = time.time()
        raw = self._call(system, user, max_tokens=1000)
        log.info(f"LLMAdvisor | Call② 完成 ({time.time()-t0:.1f}s)")

        if not raw:
            return current_space

        space = _extract_json(raw)
        if not space:
            return current_space

        space = _clip_space(space)
        # 补充缺漏参数
        for key, val in current_space.items():
            if key not in space:
                space[key] = val

        log.info("LLMAdvisor | Call② 成功：搜索空间已精化")
        return space

    # ─────────────────────────────────────────────────────
    # Call ③ 最终报告
    # ─────────────────────────────────────────────────────
    def generate_final_report(
        self,
        top_results: list,
        wfo_results: list,
        regime_summary: dict,
        n_trials: int,
    ) -> str:
        """
        生成完整的中文分析报告。
        失败时返回结构化数据摘要。
        """

        def _build_fallback():
            """LLM 不可用时的结构化降级报告"""
            lines = [f"## AI 参数优化报告（自动摘要）\n"]
            lines.append(f"- 探索次数：{n_trials} 组参数")
            if not top_results:
                lines.append("- 无有效结果")
                return "\n".join(lines)

            best = top_results[0]
            lines.append(f"- 最优平盈率：**{best.get('avg_pnl', 0):+.2f}%** / 笔")
            lines.append(f"- 胜率：{best.get('win_rate', 0):.1f}%")
            lines.append(f"- 最大回撤（累计）：{best.get('max_dd', 0):.2f}%")
            lines.append(f"- 交易笔数：{best.get('n_trades', 0)}")

            # WFO 摘要
            if wfo_results:
                wfo0 = wfo_results[0]
                wfe = wfo0.get("wfe", "N/A")
                wfe_status = wfo0.get("wfe_status", "")
                n_splits = wfo0.get("n_splits", "?")
                lines.append(f"- WFE：{wfe} {wfe_status}（{n_splits}折验证）")

            # Top 3 参数
            lines.append(f"\n### Top 3 参数组合")
            for i, r in enumerate(top_results[:3]):
                p = r.get("params", {})
                wfo = next((w for w in wfo_results if w.get("rank") == i + 1), {})
                lines.append(
                    f"{i+1}. 平盈={r.get('avg_pnl',0):+.2f}% "
                    f"胜率={r.get('win_rate',0):.1f}% "
                    f"DD={r.get('max_dd',0):.1f}% "
                    f"笔数={r.get('n_trades',0)} "
                    f"WFE={wfo.get('wfe','?')} | "
                    f"SL={p.get('hard_stop_loss_pct','?'):.1f}% "
                    f"TP={p.get('tp1_profit','?'):.1f}/{p.get('tp2_profit','?'):.1f}/{p.get('tp3_profit','?'):.1f}%"
                )

            # 市场状态
            if regime_summary:
                lines.append(f"\n### 市场状态差异")
                for regime, stats in sorted(regime_summary.items()):
                    lines.append(
                        f"- {regime}：平盈={stats.get('avg_pnl',0):+.2f}% "
                        f"胜率={stats.get('win_rate',0):.1f}% "
                        f"笔数={stats.get('count',0)}"
                    )

            lines.append(f"\n> ⚠️ LLM 报告未生成，以上为自动摘要。")
            return "\n".join(lines)

        if not self.use_llm:
            log.info("LLMAdvisor | Call③ 降级：LLM 已禁用，使用自动摘要")
            return _build_fallback()

        top3_rows = []
        for i, r in enumerate(top_results[:3]):
            p = r.get("params", {})
            wfo = next((w for w in wfo_results if w.get("rank") == i + 1), {})
            top3_rows.append(
                f"第{i+1}名：平盈率={r.get('avg_pnl',0):.2f}% | 胜率={r.get('win_rate',0):.1f}% | "
                f"PF={r.get('profit_factor',0):.2f} | 最大回撤={r.get('max_dd',0):.2f}% | WFE={wfo.get('wfe', '未验证')}\n"
                f"   参数: 止损={p.get('hard_stop_loss_pct',0):.1f}% | "
                f"回落={p.get('trailing_activate_pct',0):.1f}%激活/{p.get('trailing_drawdown_pct',0):.1f}%回撤 | "
                f"TP1={p.get('tp1_profit',0):.1f}%×{int(p.get('tp1_ratio',0)*100)}% | "
                f"TP2={p.get('tp2_profit',0):.1f}%×{int(p.get('tp2_ratio',0)*100)}% | "
                f"TP3={p.get('tp3_profit',0):.1f}% | "
                f"到期={p.get('time_exit_days',0)}天"
            )

        regime_text = "\n".join(
            f"  {regime}: 平均盈亏={stats['avg_pnl']:.2f}% 胜率={stats['win_rate']:.1f}% 笔数={stats['count']}"
            for regime, stats in regime_summary.items()
        )

        system = (
            "你是资深量化策略顾问，擅长解读 A 股量化回测结果。"
            "请用简洁专业的中文写报告，直接给出判断，避免废话。"
        )

        user = f"""请根据以下 AI 参数优化结果，生成完整分析报告：

【优化概况】
共探索了 {n_trials} 组参数组合，结果按 Calmar 风险调整收益排序。

【Top 3 参数组合】
{chr(10).join(top3_rows)}

【市场状态差异分析】
{regime_text}

请写一份约 400-600 字的分析报告，包含：
1. 最优参数组合的综合评价
2. 策略在不同市场状态下的表现差异及启示
3. 过拟合风险评估（结合 WFE 数值）
4. 实盘推荐使用哪组参数，理由是什么
5. 重要警示（如果有）

用 Markdown 格式输出。"""

        log.info("LLMAdvisor | Call③ 开始：生成最终分析报告...")
        t0 = time.time()
        raw = self._call(system, user, max_tokens=2000)
        log.info(f"LLMAdvisor | Call③ 完成 ({time.time()-t0:.1f}s)")

        if not raw:
            log.warning("LLMAdvisor | Call③ API 无响应，使用降级报告")
            return _build_fallback()

        return raw
