"""
AI 投研委员会 (多智能体红蓝对抗分析系统)

提示词设计要点（v2 — 量化档位锚定版）：
1. 评分档位锚定：每个维度给出明确打分区间（如 ROE≥15% 得10分），变主观为查表
2. 反幻觉约束：禁用"预计/有望/可能"等推测词，无数据项必须标注「数据缺失」
3. 论据有效性核验：投资经理第一步逐条核验多空论据，无效论据不纳入评分
4. 评级锚定：总分≥80重仓 / 60-80定投 / <60观望，评分与建议强绑定
5. 数学一致性硬约束：总分必须等于各分项之和
6. 结构化输出头：【总分】/【分项得分】/【摘要】三行，便于程序解析
"""
import os
import re
from typing import TypedDict, List, Optional, Tuple
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from app.data_manager.tushare_api import tushare_fetcher
from core.logger import get_logger

log = get_logger("AgentCommittee")

# ==================================================
# 0. 评分标准常量（主注入投资经理，多空研究员同步参考）
# ==================================================
SCORING_RUBRIC = """
评分维度（满分100，得分越高投资价值越高；风险维度得分越高代表风险越低）
所有分项打分必须严格按以下档位锚定，禁止主观随意给分。

1. 基本面（30分）
   - 盈利能力（10分）：ROE≥15%且毛利率≥30%得10分；ROE≥10%且毛利率≥20%得6分；其余0-4分
   - 成长性（10分）：净利润同比增速≥30%得10分；10%-30%得6分；0-10%得3分；负增长0分
   - 盈利质量（10分）：主营业务清晰、现金流匹配度高得8-10分；一般得4-7分；存疑得0-3分

2. 技术面（25分）
   - 趋势与均线（10分）：多头排列、上升趋势明确得8-10分；震荡整理得4-7分；空头排列得0-3分
   - 量价配合（8分）：上涨放量、下跌缩量得6-8分；量价紊乱得2-5分；量价背离得0-1分
   - 支撑阻力（7分）：价格处于关键支撑位上方得5-7分；区间中部得3-4分；跌破支撑得0-2分

3. 估值（20分）
   - PE相对分位（10分）：低于行业均值30%以上得8-10分；低于行业均值0-30%得5-7分；高于行业均值得0-4分
   - PB与市值合理性（10分）：PB低于行业均值且流动性充足得8-10分；基本合理得4-7分；显著高估得0-3分

4. 风险（15分，得分越高风险越低）
   - 财务风险（8分）：资产负债率<40%、无大额商誉得7-8分；负债适中得4-6分；高负债/高商誉得0-3分
   - 非财务风险（7分）：无解禁、无业绩变脸信号得6-7分；存在潜在风险点得3-5分；风险明确得0-2分

5. 行业景气（10分）
   - 行业处于上行周期/政策利好得8-10分；平稳周期得4-7分；下行周期得0-3分
"""

# ==================================================
# 1. 多头研究员 提示词
# ==================================================
BULL_SYSTEM_PROMPT = """
你是头部券商资深多头研究员，仅从给定【客观数据】中挖掘支撑买入的正面逻辑，绝对不提及任何风险、负面因素与外部未提供信息。

## 分析框架（严格按5项输出，与评分维度一一对应）
每项必须给出明确结论 + 对应数据依据，无对应数据的项标注「数据缺失」，严禁编造。
1. 基本面优势：ROE、毛利率、净利润增速、盈利质量等正面支撑
2. 技术面利多：趋势方向、均线排列、量价配合、支撑位等看涨信号
3. 估值安全边际：PE/PB与行业均值对比、市值合理性带来的估值优势
4. 风险抵御能力：债务、商誉、解禁等维度的低风险特征
5. 行业景气利好：所处行业的周期、政策等正面驱动

## 输出硬性要求
1. 每条论据前加序号，结尾标注「数据依据：xxx」，所有结论必须由【客观数据】直接推导
2. 严禁使用「预计、有望、可能、未来」等推测性表述，只陈述基于现有数据的客观事实
3. 严禁提及北向资金、机构研报、新闻事件等任何未在【客观数据】中出现的信息
4. 结尾单独列出「数据缺失项」，明确标注哪些维度无对应数据支撑
5. 全程只输出看多逻辑，不出现任何风险提示、负面评价、中性表述
"""

BULL_USER_TEMPLATE = """
标的：{ticker}

【客观数据】
{context}

请按上述框架输出完整看多论证报告。
"""

# ==================================================
# 2. 空头研究员 提示词
# ==================================================
BEAR_SYSTEM_PROMPT = """
你是头部券商资深风控/空头研究员，仅从给定【客观数据】中挖掘估值泡沫、基本面隐患、技术风险等看空逻辑，绝对不提及任何利好、正面因素与外部未提供信息。

## 排雷框架（严格按5项输出，与评分维度一一对应）
每项必须给出明确风险结论 + 对应数据依据，无对应数据的项标注「数据缺失」，严禁编造。
1. 基本面隐患：ROE下滑、毛利率压缩、盈利质量差等负面信号
2. 技术面风险：顶背离、破位、量价背离、跌破支撑等看空信号
3. 估值泡沫：PE/PB高于行业均值、市值虚高带来的估值风险
4. 核心风险点：债务高企、大额商誉、解禁压力、业绩变脸等风险
5. 行业景气逆风：所处行业的周期下行、政策利空等负面驱动

## 输出硬性要求
1. 每条风险前加序号，结尾标注「数据依据：xxx」，所有结论必须由【客观数据】直接推导
2. 严禁使用「预计、有望、可能、未来」等推测性表述，只陈述基于现有数据的客观事实
3. 严禁提及北向资金、机构研报、新闻事件等任何未在【客观数据】中出现的信息
4. 结尾单独列出「数据缺失项」，明确标注哪些维度无对应数据支撑
5. 全程只输出看空/风险逻辑，不出现任何利好评价、正面表述
"""

BEAR_USER_TEMPLATE = """
标的：{ticker}

【客观数据】
{context}

请按上述框架输出完整看空排雷报告。
"""

# ==================================================
# 3. 投资经理（仲裁） 提示词
# ==================================================
PM_SYSTEM_PROMPT = """
你是首席投资组合经理，职责是基于客观数据，核验多空研究员报告的论据真实性，平衡双方观点，输出标准化投研简报。

## 工作流程（必须严格按顺序执行，不得跳步）
1. 论据有效性核验：逐条核对多头、空头报告的每一条论据，是否有【客观数据】支撑；无数据支撑、编造、超纲的论据直接标记「无效」，不纳入后续评分与结论
2. 分项打分：严格参照【评分标准】的量化档位，对5个维度逐一打分，给出分项得分 + 简短打分理由
3. 综合研判：采信有效论据，形成最终投资逻辑与建议
4. 严格按指定格式输出，不得增减模块

## 输出格式（严格遵守，便于程序解析，禁止额外排版）
【总分】XX
【分项得分】基本面:XX / 技术面:XX / 估值:XX / 风险:XX / 行业景气:XX
【摘要】一句话总结核心投资逻辑，不超过50字

### 1. 公司基本面分析
（整合有效多空论据，客观陈述基本面优劣）

### 2. 技术面分析
（整合有效多空论据，客观陈述技术面多空信号）

### 3. 估值与行业分析
（合并估值、行业景气两个维度的结论）

### 4. 核心风险提示
（仅列经数据验证的真实风险，无效论据不得列入）

### 5. 最终投资建议
- 操作评级：重仓 / 定投 / 观望
  （评级锚定：总分≥80 重仓；60≤总分<80 定投；总分<60 观望）
- 核心风控触发点：明确列出1-2个必须离场/减仓的量化条件

【论据核验说明】
- 采信多头核心论据：xxx
- 采信空头核心论据：xxx
- 否决无效论据：xxx

## 硬性规则
1. 所有结论必须基于【客观数据】，严禁编造数据、引入外部信息
2. 评分必须严格匹配评分标准档位，总分必须等于各分项得分之和
3. 摘要必须精炼，不得超过50字
4. 操作评级必须严格按总分锚定，不得主观调整
"""

PM_USER_TEMPLATE = """
股票代码: {ticker}

【客观数据】
{context}

【多头研究员报告】
{bull_report}

【空头研究员报告】
{bear_report}

【评分标准】
{scoring_rubric}

请严格按上述流程和格式输出最终投研简报。
"""

# ==================================================
# 状态定义与工作流
# ==================================================

class CommitteeState(TypedDict):
    ticker: str        # 股票代码
    context: str       # 从数据库或 Tushare 提取的客观干事实
    bull_report: str   # 多头报告
    bear_report: str   # 空头报告
    final_report: str  # 融合五支柱模板的最终投研报告

# 获取底层大模型
def get_llm(model="gpt-4o"):
    api_key = os.environ.get("OPENAI_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
    base_url = os.environ.get("OPENAI_BASE_URL")

    if not api_key:
        log.error("LLM API key 未配置: 请设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 环境变量")
        raise RuntimeError(
            "LLM API key 未配置: 请设置 OPENAI_API_KEY 或 DEEPSEEK_API_KEY 环境变量"
        )

    # 自动识别 deepseek key
    if not base_url and os.environ.get("DEEPSEEK_API_KEY"):
        base_url = "https://api.deepseek.com/v1"
        if model == "gpt-4o" or model == "deepseek-chat":
            model = "deepseek-chat"

    if not base_url:
        base_url = "https://api.openai.com/v1"

    # temperature=0.3：兼顾稳定与表达力，降低幻觉
    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0.3)

# --- 节点函数定义 ---

def context_retriever(state: CommitteeState) -> dict:
    ticker = state.get("ticker", "")
    log.info(f"[Committee] Fetching data context for {ticker}...")
    context = tushare_fetcher.get_ai_context(ticker)
    # M3 防御：context 过短说明数据源异常，提前给出明确标记
    if not context or len(context) < 100:
        context = f"【数据警告】标的 {ticker} 的客观数据获取不足（可能 Tushare 未配置或代码无效）。\n请基于此限制给出审慎判断，不得臆测任何具体财务指标。"
    return {"context": context}

def bull_researcher(state: CommitteeState) -> dict:
    log.info(f"[Committee] Dispatching Bull Researcher for {state['ticker']}...")
    try:
        llm = get_llm()
        human_prompt = BULL_USER_TEMPLATE.format(
            ticker=state['ticker'],
            context=state['context']
        )
        resp = llm.invoke([
            {"role": "system", "content": BULL_SYSTEM_PROMPT.strip()},
            {"role": "user", "content": human_prompt}
        ])
        return {"bull_report": resp.content}
    except Exception as e:
        log.error(f"Bull researcher failed: {e}")
        return {"bull_report": f"❌ 看多分析失败: {e}"}

def bear_researcher(state: CommitteeState) -> dict:
    log.info(f"[Committee] Dispatching Bear Researcher for {state['ticker']}...")
    try:
        llm = get_llm()
        human_prompt = BEAR_USER_TEMPLATE.format(
            ticker=state['ticker'],
            context=state['context']
        )
        resp = llm.invoke([
            {"role": "system", "content": BEAR_SYSTEM_PROMPT.strip()},
            {"role": "user", "content": human_prompt}
        ])
        return {"bear_report": resp.content}
    except Exception as e:
        log.error(f"Bear researcher failed: {e}")
        return {"bear_report": f"❌ 看空分析失败: {e}"}

def research_manager(state: CommitteeState) -> dict:
    log.info(f"[Committee] Research Manager synthesizing final report for {state['ticker']}...")
    try:
        llm = get_llm()
        human_prompt = PM_USER_TEMPLATE.format(
            ticker=state['ticker'],
            context=state['context'],
            bull_report=state['bull_report'],
            bear_report=state['bear_report'],
            scoring_rubric=SCORING_RUBRIC
        )
        resp = llm.invoke([
            {"role": "system", "content": PM_SYSTEM_PROMPT.strip()},
            {"role": "user", "content": human_prompt}
        ])
        return {"final_report": resp.content}
    except Exception as e:
        log.error(f"Research manager failed: {e}")
        return {"final_report": f"❌ 最终投研报告生成失败: {e}"}

# --- 构建工作流引擎 ---
def create_committee_graph():
    workflow = StateGraph(CommitteeState)

    workflow.add_node("context_retriever", context_retriever)
    workflow.add_node("bull_researcher", bull_researcher)
    workflow.add_node("bear_researcher", bear_researcher)
    workflow.add_node("research_manager", research_manager)

    # 路径：获取上下文 -> 同时分配给多头和空头 -> 汇总给投资经理
    workflow.set_entry_point("context_retriever")
    workflow.add_edge("context_retriever", "bull_researcher")
    workflow.add_edge("context_retriever", "bear_researcher")
    workflow.add_edge("bull_researcher", "research_manager")
    workflow.add_edge("bear_researcher", "research_manager")
    workflow.add_edge("research_manager", END)

    return workflow.compile()

# 全局复用这棵编译好的图
committee_app = create_committee_graph()


def parse_score_and_summary(report: str) -> Tuple[Optional[int], Optional[str]]:
    """从报告开头解析总分和摘要

    报告格式（v2）：
        【总分】85
        【分项得分】基本面:25 / 技术面:18 / 估值:15 / 风险:12 / 行业景气:8
        【摘要】一句话总结

    兼容旧格式 【评分】XX。

    Returns:
        (score, summary) — 解析失败则为 (None, None)
    """
    if not report:
        return None, None

    # 解析总分：优先 【总分】，兼容旧 【评分】
    score_match = re.search(r'【(?:总分|评分)】\s*(\d{1,3})', report)
    score = int(score_match.group(1)) if score_match else None
    if score is not None and (score < 0 or score > 100):
        score = max(0, min(100, score))  # 钳制到 0-100

    # 解析摘要：匹配 【摘要】后到换行
    summary_match = re.search(r'【摘要】\s*(.+?)(?:\n|$)', report)
    summary = summary_match.group(1).strip() if summary_match else None

    return score, summary


def parse_subscores(report: str) -> Optional[dict]:
    """解析分项得分（v2 新增）

    格式：【分项得分】基本面:25 / 技术面:18 / 估值:15 / 风险:12 / 行业景气:8

    Returns:
        {"基本面": 25, "技术面": 18, ...} 或 None
    """
    if not report:
        return None
    line_match = re.search(r'【分项得分】\s*(.+?)(?:\n|$)', report)
    if not line_match:
        return None
    line = line_match.group(1)
    subscores = {}
    for part in re.split(r'[/／、]', line):
        m = re.match(r'\s*([^:：]+)[:：]\s*(\d{1,3})', part)
        if m:
            subscores[m.group(1).strip()] = int(m.group(2))
    return subscores if subscores else None


def strip_header(report: str) -> str:
    """剥离报告开头的【总分】【分项得分】【摘要】三行，正文部分用于展示

    兼容旧格式 【评分】。
    """
    if not report:
        return report
    # 用 [^\n]* 贪婪匹配整行，确保把头部所在行完整移除
    cleaned = re.sub(r'^【(?:总分|评分)】[^\n]*\n?', '', report, count=1)
    cleaned = re.sub(r'^【分项得分】[^\n]*\n?', '', cleaned, count=1)
    cleaned = re.sub(r'^【摘要】[^\n]*\n?', '', cleaned, count=1)
    # 去掉开头多余空行
    return cleaned.lstrip('\n')


def generate_ai_report(ticker: str) -> str:
    """被 API 层调用的入口函数"""
    initial_state = {"ticker": ticker}
    result = committee_app.invoke(initial_state)
    return result.get("final_report", "分析失败，请检查数据源及 LLM 配置。")
