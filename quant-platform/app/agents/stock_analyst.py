import os
from openai import OpenAI
from app.data_manager.tushare_api import tushare_fetcher
from core.logger import get_logger

log = get_logger("StockAnalyst")

class StockAnalystAgent:
    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com")
        else:
            self.client = None
            log.warning("DEEPSEEK_API_KEY is not set in environment.")

    def generate_report(self, stock_code: str) -> str:
        if not self.client:
            return "❌ DeepSeek API 尚未配置，请在 .env 中设置 DEEPSEEK_API_KEY"
            
        # 1. Fetch context
        log.info(f"Fetching context data for {stock_code} ...")
        context = tushare_fetcher.get_ai_context(stock_code)

        # 2. Call DeepSeek
        messages = [
            {
                "role": "system", 
                "content": "你是一位拥有20年经验的A股资深量化/基本面分析师。请结合提供的客观市场数据（近期K线、市盈率、市占比等），输出一份专业、简明的个股深度体检报告。\n\n"
                           "要求采用以下结构（Markdown 格式）：\n"
                           "## 📊 股票概览\n"
                           "## 📈 技术面分析 (K线形态/趋势判断)\n"
                           "## 💼 基本面与估值分析\n"
                           "## ⚠️ 风险提示\n"
                           "## 💡 综合操作建议\n\n"
                           "注意：语气客观中立，分析具备实战指导意义，不带废话。"
            },
            {
                "role": "user", 
                "content": f"请为 {stock_code} 生成体检报告。以下是最近的市场数据：\n{context}"
            }
        ]
        
        try:
            log.info(f"Calling DeepSeek API for {stock_code} ...")
            response = self.client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                stream=False
            )
            report = response.choices[0].message.content
            log.info(f"Successfully generated report for {stock_code}.")
            return report
        except Exception as e:
            log.error(f"Error calling DeepSeek API: {e}")
            return f"❌ 生成 AI 体检报告失败: {str(e)}"

stock_analyst = StockAnalystAgent()
