import os
import re
from openai import OpenAI
from core.logger import get_logger

log = get_logger("StrategyCoder")

class StrategyCoderAgent:
    def __init__(self):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if self.api_key:
            self.client = OpenAI(api_key=self.api_key, base_url="https://api.deepseek.com")
        else:
            self.client = None
            log.warning("DEEPSEEK_API_KEY is not set.")

    def _extract_code(self, text: str) -> str:
        # 尝试提取 python 代码块中的内容
        match = re.search(r"```python\s*(.*?)\s*```", text, re.DOTALL)
        if match:
            return match.group(1).strip()
        # 兜底：如果没写代码块但还是代码，就直接返回
        return text.strip()

    def generate_strategy(self, prompt: str) -> str:
        if not self.client:
            return "# ❌ DeepSeek API 未配置，请在 .env 中设置 DEEPSEEK_API_KEY"
            
        system_prompt = """你是一位精通 Pandas 的中国A股量化开发工程师。
请根据用户的自然语言需求，编写一段可运行的 Python 选股策略代码。

【重要约束】：
1. 必须使用 Python 导入并继承基类：`from app.screener.strategies.base import BaseStrategy`。
2. 定义一个类（名字自取），并提供类属性 `name` 和 `description`。
3. 实现 `def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:` 方法。
4. 输入 `df` 是多支股票的历史 K 线，列包含：`code`, `date`, `open`, `high`, `low`, `close`, `volume` (由于是多支股票，必须通过 `groupby('code')` 处理)。
5. `generate_signals` 方法的返回值必须是一个 `pd.DataFrame`，且只含有 `["code", "date"]` 这两列，代表在哪些日期的哪些股票触发了买入信号。
6. 【严格输出格式】：请直接输出纯 Python 代码，必须放在 ```python ``` 代码块中。不要有任何额外的解释和问候语。
"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"需求描述：{prompt}\n请生成上述策略代码："}
        ]
        
        try:
            log.info("Calling DeepSeek for strategy generation...")
            response = self.client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=messages,
                stream=False
            )
            raw_text = response.choices[0].message.content
            return self._extract_code(raw_text)
        except Exception as e:
            log.error(f"Strategy generation failed: {e}")
            return f"# ❌ 策略代码生成失败:\n# {str(e)}"

strategy_coder = StrategyCoderAgent()
