# -*- coding: utf-8 -*-
import os
import json
from datetime import datetime, timedelta
from typing import Optional

import tushare as ts
from openai import OpenAI
from pydantic import BaseModel

from core.logger import get_logger
from database.duckdb_manager import db

log = get_logger("ConceptMiner")

class ConceptMinerAgent:
    def __init__(self):
        self.ts_key = os.getenv("TUSHARE_KEY")
        self.ds_key = os.getenv("DEEPSEEK_API_KEY")
        
        if self.ts_key:
            ts.set_token(self.ts_key)
            self.pro = ts.pro_api()
        else:
            self.pro = None
            log.warning("ConceptMiner: TUSHARE_KEY 未配置")
            
        if self.ds_key:
            self.client = OpenAI(
                api_key=self.ds_key,
                base_url="https://api.deepseek.com/v1"
            )
        else:
            self.client = None
            log.warning("ConceptMiner: DEEPSEEK_API_KEY 未配置")

    def fetch_recent_news(self, hours: int = 4) -> list:
        """从 Tushare 获取最近几小时的快讯"""
        if not self.pro:
            raise ValueError("未配置 TUSHARE_KEY，无法获取快讯")
            
        now = datetime.now()
        start = now - timedelta(hours=hours)
        
        start_str = start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            # 尝试获取同花顺快讯或其它快讯接口
            # Tushare 的 news 接口：src='sina' 或其它
            df = self.pro.news(src='sina', start_date=start_str, end_date=end_str)
            if df is None or df.empty:
                # 兜底查询
                date_str = now.strftime("%Y%m%d")
                df = self.pro.major_news(src='', start_date=date_str, end_date=date_str)
                
            if df is None or df.empty:
                return []
                
            news_list = []
            # 获取最新的100条新闻组合
            for _, row in df.head(100).iterrows():
                title = row.get('title', '')
                content = row.get('content', '')
                time_str = row.get('datetime', '')
                if title or content:
                    news_list.append(f"[{time_str}] 【{title}】 {content}")
                    
            return news_list
        except Exception as e:
            log.error(f"提取快讯失败: {e}")
            raise e

    def analyze_and_extract(self, news_list: list, target_count: int = 10) -> dict:
        """调用 DeepSeek 深度解构新闻，提取核心概念板块"""
        if not self.client:
            raise ValueError("未配置 DEEPSEEK_API_KEY")
        if not news_list:
            raise ValueError("新闻列表为空，无法分析")
            
        # 截取适量文本，防止超长
        news_text = "\n".join(news_list[:60])
        
        sys_prompt = f"""你是一个顶级的A股短线游资操盘手和市场情绪分析专家。
你的任务是阅读给定的最近几小时的全网财经快讯，并敏锐地捕捉当前资金正在攻击或即将攻击的【核心概念板块】。
请深度解析目前的宏观与微观情绪，总结市场主线题材，并严格以JSON格式输出。
不要生成 3-5 个，请尽可能多且精准地提取出大约 {target_count} 个最核心的发酵概念板块（必须是A股标准的板块概念名称，如：低空经济、AI语料、液冷服务器、铜缆高速连接等）。

严格按照以下JSON结构返回（不要包含任何 markdown 代码块符号，直接输出 JSON 纯文本）：
{{
    "analysis": "这里写几段深度复盘和情绪解析，写明为什么看好这些主线，逻辑清晰详实。",
    "concepts": [
        "板块名称1",
        "板块名称2"
    ]
}}
"""
        try:
            resp = self.client.chat.completions.create(
                model="deepseek-v4-pro",
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": f"以下是最新的焦点快讯：\n{news_text}"}
                ],
                temperature=0.3
            )
            
            content = resp.choices[0].message.content.strip()
            # 移除可能的大模型 Markdown JSON 包裹
            if content.startswith("```json"):
                content = content[7:]
            elif content.startswith("```"):
                content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
            content = content.strip()
            
            parsed = json.loads(content)
            
            ai_analysis = parsed.get("analysis", "")
            extracted_concepts = parsed.get("concepts", [])
            
            # 入库保存
            db.save_sentiment_analysis(
                raw_news=news_list,
                ai_analysis=ai_analysis,
                extracted_concepts=extracted_concepts
            )
            
            # 从 DuckDB 返回刚存的组合记录
            return db.get_latest_sentiment()
            
        except Exception as e:
            log.error(f"大模型解析热点失败: {e}")
            raise e

# 单例实例
concept_miner = ConceptMinerAgent()
