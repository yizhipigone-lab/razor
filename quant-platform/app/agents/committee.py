"""
AI 投研委员会 (多智能体红蓝对抗分析系统)
"""
import os
import asyncio
from typing import TypedDict, Annotated, List
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import StateGraph, END

from app.data_manager.tushare_api import tushare_fetcher
from core.logger import get_logger

log = get_logger("AgentCommittee")

# 定义 LangGraph 状态传递
class CommitteeState(TypedDict):
    ticker: str        # 股票代码
    context: str       # 从数据库或 Tushare 提取的客观干事实
    bull_report: str   # 多头报告
    bear_report: str   # 空头报告
    final_report: str  # 融合五支柱模板的最终投研报告
    messages: List[BaseMessage]

# 获取底层大模型
def get_llm(model="gpt-4o"):
    # 为了兼容 OpenAI 或兼容接口 (如 DeepSeek, DashScope)，可以读环境变量
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
        # 修正 #10: 实际模型名不是 v4-pro
        if model == "gpt-4o" or model == "deepseek-chat":
            model = "deepseek-chat"

    if not base_url:
        base_url = "https://api.openai.com/v1"

    return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, temperature=0.2)

# --- 节点函数定义 ---

def context_retriever(state: CommitteeState) -> dict:
    ticker = state.get("ticker", "")
    log.info(f"[Committee] Fetching data context for {ticker}...")
    # 从 Tushare 或 DuckDB 提取全面客观的描述字符串
    context = tushare_fetcher.get_ai_context(ticker)
    return {"context": context}

def bull_researcher(state: CommitteeState) -> dict:
    log.info(f"[Committee] Dispatching Bull Researcher for {state['ticker']}...")
    try:
        llm = get_llm()
        system_prompt = "你是顶级券商的看多研究员(Bull Researcher)。从提供的标的数据中，挖掘一切有利的基本面、资金面和技术面信息。只说支撑买入的逻辑，不要写任何风险。"
        human_prompt = f"以下是标的客观数据:\n\n{state['context']}\n\n请给出强有力的看多论证报告。"

        resp = llm.invoke([
            {"role": "system", "content": system_prompt},
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
        system_prompt = "你是极其谨慎的看空研究员(Bear Researcher)。从数据中挖掘所有的估值泡沫、债务隐患、技术顶背离。只找做空或规避的理由，不要看好它。"
        human_prompt = f"以下是标的客观数据:\n\n{state['context']}\n\n请给出强有力的看空排雷报告。"

        resp = llm.invoke([
            {"role": "system", "content": system_prompt},
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

        system_prompt = """
你是首席投资组合经理。你需要平衡过于乐观的多头报告和过于悲观的空头报告，综合你掌握的数据事实，输出一份最终的机构级投研简报。
请严格按照以下【五支柱结构】输出 Markdown：

### 1. 公司基本面分析
(概括护城河、利润与估值质量)

### 2. 技术面分析
(当前动能、信号及支撑阻力)

### 3. 行业与资金面
(热点关注度、北向/主力买行为)

### 4. 核心风险提示
(宏观风向与内部隐患)

### 5. 最终投资建议
(评分 0-100，并给出明确的 观望 / 定投 / 重仓 判定及风控点)
"""
        human_prompt = f"""
股票代码: {state['ticker']}
【多头研究员报告】:
{state['bull_report']}

【空头研究员报告】:
{state['bear_report']}

请综合仲裁，输出最终报告。
"""
        resp = llm.invoke([
            {"role": "system", "content": system_prompt.strip()},
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

def generate_ai_report(ticker: str) -> str:
    """被 API 层调用的入口函数"""
    initial_state = {"ticker": ticker}
    result = committee_app.invoke(initial_state)
    return result.get("final_report", "分析失败，请检查数据源及 LLM 配置。")
