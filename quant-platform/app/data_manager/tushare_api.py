import os
import tushare as ts
import pandas as pd
from datetime import datetime, timedelta
from core.logger import get_logger

log = get_logger("TushareAPI")

class TushareContextFetcher:
    def __init__(self):
        self.token = os.environ.get("TUSHARE_KEY", "")
        if self.token:
            ts.set_token(self.token)
            self.pro = ts.pro_api()
        else:
            self.pro = None
            log.warning("TUSHARE_KEY is not set in environment.")

    def format_code(self, code: str) -> str:
        """转换代码格式 for Tushare, e.g. 000001.SZ or 600000.SH if not already"""
        if "." in code:
            return code
        if code.startswith("6"):
            return f"{code}.SH"
        return f"{code}.SZ"

    def get_stock_basic(self, code: str) -> dict:
        if not self.pro: return {}
        try:
            formatted_code = self.format_code(code)
            df = self.pro.stock_basic(ts_code=formatted_code, fields='ts_code,symbol,name,area,industry,market,list_date')
            if not df.empty:
                return df.iloc[0].to_dict()
        except Exception as e:
            log.error(f"Error fetching stock basic for {code}: {e}")
        return {}

    def get_recent_klines(self, code: str, days: int = 30) -> pd.DataFrame:
        if not self.pro: return pd.DataFrame()
        try:
            formatted_code = self.format_code(code)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days * 2) # Factor in weekends
            
            df = self.pro.daily(
                ts_code=formatted_code, 
                start_date=start_date.strftime("%Y%m%d"), 
                end_date=end_date.strftime("%Y%m%d")
            )
            if not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.sort_values('trade_date').tail(days)
                return df
        except Exception as e:
            log.error(f"Error fetching klines for {code}: {e}")
        return pd.DataFrame()

    def get_daily_basic(self, code: str) -> dict:
        """获取每日指标（市盈率，市净率，换手率等）"""
        if not self.pro: return {}
        try:
            formatted_code = self.format_code(code)
            # 尽可能获取最近一次交易日的数据
            df = self.pro.daily_basic(ts_code=formatted_code, limit=1)
            if not df.empty:
                return df.iloc[0].to_dict()
        except Exception as e:
            log.error(f"Error fetching daily basic for {code}: {e}")
        return {}

    def get_ai_context(self, code: str) -> str:
        """Assemble a context string for the AI Analyst"""
        if not self.pro:
            return "Tushare API is not configured."

        basic_info = self.get_stock_basic(code)
        daily_basic = self.get_daily_basic(code)
        klines = self.get_recent_klines(code, days=20)

        context_parts = []
        
        # 1. 基本信息
        if basic_info:
            context_parts.append("### 股票基本信息")
            context_parts.append(f"- 股票代码: {basic_info.get('ts_code')}")
            context_parts.append(f"- 股票名称: {basic_info.get('name')}")
            context_parts.append(f"- 所属行业: {basic_info.get('industry')}")
            context_parts.append(f"- 所在地域: {basic_info.get('area')}")
            context_parts.append(f"- 市场板块: {basic_info.get('market')}")

        # 2. 估值和市值
        if daily_basic:
            context_parts.append("\n### 日度基本指标 (估值/市值)")
            context_parts.append(f"- 收盘价: {daily_basic.get('close', 'N/A')}")
            context_parts.append(f"- 换手率 (%): {daily_basic.get('turnover_rate', 'N/A')}")
            context_parts.append(f"- 市盈率 (PE TTM): {daily_basic.get('pe_ttm', 'N/A')}")
            context_parts.append(f"- 市净率 (PB): {daily_basic.get('pb', 'N/A')}")
            context_parts.append(f"- 总市值 (万元): {daily_basic.get('total_mv', 'N/A')}")
            context_parts.append(f"- 流通市值 (万元): {daily_basic.get('circ_mv', 'N/A')}")

        # 3. 近期 K 线
        if not klines.empty:
            context_parts.append("\n### 近期K线数据 (最近 20 个交易日)")
            # 简化输出格式给 AI
            kline_str = klines[['trade_date', 'open', 'high', 'low', 'close', 'vol', 'pct_chg']].to_string(index=False)
            context_parts.append("```\n" + kline_str + "\n```")

        if not context_parts:
            return f"无法获取股票 {code} 的相关信息。"

        return "\n".join(context_parts)

tushare_fetcher = TushareContextFetcher()
