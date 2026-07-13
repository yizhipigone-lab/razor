import os
import tushare as ts
import pandas as pd
import numpy as np
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

    def get_daily_basic_history(self, code: str, days: int = 30) -> pd.DataFrame:
        """获取近 N 天每日指标（用于估值分位：当前 PE/PB vs 自身历史）"""
        if not self.pro: return pd.DataFrame()
        try:
            formatted_code = self.format_code(code)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days * 2)
            df = self.pro.daily_basic(
                ts_code=formatted_code,
                start_date=start_date.strftime("%Y%m%d"),
                end_date=end_date.strftime("%Y%m%d"),
                fields='trade_date,pe_ttm,pb,total_mv,circ_mv,turnover_rate'
            )
            if not df.empty:
                df['trade_date'] = pd.to_datetime(df['trade_date'])
                df = df.sort_values('trade_date').tail(days)
            return df
        except Exception as e:
            log.error(f"Error fetching daily basic history for {code}: {e}")
        return pd.DataFrame()

    def get_financial_indicator(self, code: str) -> dict:
        """获取最新财务指标（ROE/毛利率/净利润同比/资产负债率）

        对应评分标准的基本面维度。依次尝试最近几个报告期，取首个有数据的。
        """
        if not self.pro: return {}
        try:
            formatted_code = self.format_code(code)
            # 依次尝试最近几个报告期，取首个有数据的
            now = datetime.now()
            candidate_periods = []
            y = now.year
            for offset in range(6):
                yy = y - offset
                candidate_periods.extend([f"{yy}1231", f"{yy}0930", f"{yy}0630", f"{yy}0331"])
            # 去重保序
            seen = set()
            candidate_periods = [p for p in candidate_periods if not (p in seen or seen.add(p))]
            candidate_periods = candidate_periods[:6]  # 只尝试最近 6 个

            for p in candidate_periods:
                try:
                    df = self.pro.fina_indicator(
                        ts_code=formatted_code, period=p,
                        fields='ts_code,roe,grossprofit_margin,netprofit_yoy,debt_to_assets'
                    )
                    if not df.empty:
                        return df.iloc[0].to_dict()
                except Exception:
                    continue
            return {}
        except Exception as e:
            log.error(f"Error fetching fina_indicator for {code}: {e}")
        return {}

    def _build_technical_signals(self, klines: pd.DataFrame, code: str) -> str:
        """把 K线用 MyTT 算成【确定性技术信号】，供 LLM 直接据以查表打分（替代"喂原始K线让LLM看图"）。
        按 趋势均线 / 量价配合 / 支撑阻力 三组组织，对齐评分标准的技术面子项。纯 Python，不依赖 TA-Lib。"""
        try:
            if klines is None or klines.empty or len(klines) < 60:
                return "### 技术面结构化信号\n（近120日K线不足60根，技术面信号数据缺失，请审慎评估技术面）"
            from app.indicators.MyTT import MA, MACD, RSI, KDJ, BOLL, HHV, LLV, SLOPE

            c = klines['close'].values.astype(float)
            h = klines['high'].values.astype(float)
            l = klines['low'].values.astype(float)
            v = klines['vol'].values.astype(float)

            ma5, ma10, ma20, ma60 = MA(c, 5), MA(c, 10), MA(c, 20), MA(c, 60)
            dif, dea, macd = MACD(c)
            kk, kd, kj = KDJ(c, h, l)
            rsi = RSI(c, 14)
            up, mid, lowr = BOLL(c, 20, 2)

            bull_align = bool(ma5[-1] > ma10[-1] > ma20[-1] > ma60[-1])
            bear_align = bool(ma5[-1] < ma10[-1] < ma20[-1] < ma60[-1])
            ma5_angle = float(np.degrees(np.arctan(SLOPE(ma5, 5)[-1])))
            golden = bool(dif[-1] > dea[-1])
            avg5v = float(np.mean(v[-6:-1]))
            vol_ratio = float(v[-1] / avg5v) if avg5v > 0 else 1.0
            hh60, ll60 = HHV(h, 60)[-1], LLV(l, 60)[-1]
            range_pos = float((c[-1] - ll60) / (hh60 - ll60) * 100) if (hh60 - ll60) > 0 else 50.0
            ret5 = float((c[-1] / c[-6] - 1) * 100) if len(c) > 6 and c[-6] > 0 else 0.0
            ret20 = float((c[-1] / c[-21] - 1) * 100) if len(c) > 21 and c[-21] > 0 else 0.0
            boll_pos = "上轨上方(超买区)" if c[-1] > up[-1] else ("下轨下方(超卖区)" if c[-1] < lowr[-1] else "布林带内")

            align_txt = ("多头排列(MA5>MA10>MA20>MA60，强势上行)" if bull_align
                         else "空头排列(MA5<MA10<MA20<MA60，弱势下行)" if bear_align
                         else "均线纠缠(无明确排列，震荡整理)")
            vp_txt = ("上涨放量(看涨)" if ret5 > 0 and vol_ratio > 1.2
                      else "下跌缩量(抗跌)" if ret5 < 0 and vol_ratio < 0.8
                      else "量价背离(价涨量缩，动能不足)" if ret5 > 0 and vol_ratio < 0.8
                      else "量价平稳")

            L = ["### 技术面结构化信号 (确定性计算 — 技术面评分请直接据此查表，勿自行看K线推断)",
                 "对齐评分标准技术面三子项：【趋势与均线10分】/【量价配合8分】/【支撑阻力7分】。",
                 "\n【趋势与均线】(满分10)",
                 f"- 均线排列: {align_txt}",
                 f"- MA5近5日斜率: {ma5_angle:+.1f}° (>0上行，<-10急跌)",
                 f"- 当前价 {c[-1]:.2f}；MA20={ma20[-1]:.2f} MA60={ma60[-1]:.2f} → "
                 f"{'价在MA20/MA60上方' if c[-1] > ma20[-1] and c[-1] > ma60[-1] else '价在关键均线下方'}",
                 "\n【量价配合】(满分8)",
                 f"- 量比(今/近5日均量): {vol_ratio:.2f} (>1.2放量，<0.8缩量)",
                 f"- 近5日涨跌 {ret5:+.1f}%，{vp_txt}",
                 "\n【支撑阻力】(满分7)",
                 f"- 距近60日区间位置: {range_pos:.0f}% (0%=区间最低≈近支撑，100%=区间最高≈近阻力)",
                 f"- 布林带: {boll_pos} (上轨{up[-1]:.2f}/中轨{mid[-1]:.2f}/下轨{lowr[-1]:.2f})",
                 "\n【辅助参考】(非直接评分项)",
                 f"- MACD: DIF={dif[-1]:.3f} DEA={dea[-1]:.3f} → {'金叉(多)' if golden else '死叉(空)'} 柱={macd[-1]:.3f}",
                 (f"- KDJ: K={kk[-1]:.1f} D={kd[-1]:.1f} J={kj[-1]:.1f} (J>100超买)" if kj[-1] > 100
                  else f"- KDJ: K={kk[-1]:.1f} D={kd[-1]:.1f} J={kj[-1]:.1f} (J<0超卖)" if kj[-1] < 0
                  else f"- KDJ: K={kk[-1]:.1f} D={kd[-1]:.1f} J={kj[-1]:.1f}")
                 + f"；RSI14={rsi[-1]:.1f}(>70超买/<30超卖)",
                 f"- 近20日涨跌: {ret20:+.1f}%"]
            return "\n".join(L)
        except Exception as e:
            log.error(f"技术面信号计算失败 {code}: {e}")
            return f"### 技术面结构化信号\n（计算异常: {e}；请审慎评估技术面）"

    def _build_market_sentiment(self, code: str) -> str:
        """用 akshare 取东财【综合评价 + 散户情绪 + 个股新闻】作为市场参考维度。
        纯 Python(akshare 已封装反爬)，不碰浏览器，零合规风险。
        标注'供投资经理参考，不替代客观评分'。各子项独立 try/except，任一失败不影响其余。"""
        clean = str(code).split('.')[0]
        lines = ["### 市场情绪与综合评价 (东财数据，供投资经理参考，不替代客观评分)"]
        try:
            import akshare as ak
        except ImportError:
            return "### 市场情绪与综合评价\n（akshare 未安装，跳过该维度）"

        # 1. 综合评分(东财30天评分趋势) —— 列序: [日期, 得分]
        try:
            df = ak.stock_comment_detail_zhpj_lspf_em(symbol=clean)
            if df is not None and not df.empty and df.shape[1] >= 2:
                cur = float(df.iloc[-1, 1])
                prev = float(df.iloc[-6, 1]) if len(df) >= 6 else float(df.iloc[0, 1])
                d = cur - prev
                trend = "上行" if d > 0 else ("下行" if d < 0 else "持平")
                lines.append(f"【综合评价】东财综合评分 {cur:.1f}（5日前 {prev:.1f}，{d:+.1f}，{trend}）— 东财机构级综合评价，含资金/情绪因子")
        except Exception:
            lines.append("【综合评价】数据缺失")

        # 2. 散户买入意愿(情绪温度计) —— 列序: [日期,代码,买入意愿,5日均,...]
        try:
            df2 = ak.stock_comment_detail_scrd_desire_em(symbol=clean)
            if df2 is not None and not df2.empty and df2.shape[1] >= 4:
                r = df2.iloc[-1]
                desire, avg = float(r.iloc[2]), float(r.iloc[3])
                tag = "偏积极" if desire > 50 else ("偏谨慎" if desire < 50 else "中性")
                lines.append(f"【散户情绪】买入意愿 {desire:.1f}（5日均 {avg:.1f}，{tag}；>50积极/<50谨慎）— 市场参与意愿温度计")
        except Exception:
            lines.append("【散户情绪】数据缺失")

        # 3. 近期事件新闻(事件驱动参考) —— 列序: [关键词,标题,内容,时间,来源,链接]
        try:
            df3 = ak.stock_news_em(symbol=clean)
            if df3 is not None and not df3.empty and df3.shape[1] >= 4:
                lines.append("【近期事件】(事件驱动参考，非评分依据)")
                for _, row in df3.head(5).iterrows():
                    t = str(row.iloc[3])[:10]
                    title = str(row.iloc[1])[:48]
                    lines.append(f"  - [{t}] {title}")
        except Exception:
            lines.append("【近期事件】数据缺失")

        return "\n".join(lines)

    def get_ai_context(self, code: str) -> str:
        """Assemble a context string for the AI Analyst"""
        if not self.pro:
            return "Tushare API is not configured."

        basic_info = self.get_stock_basic(code)
        daily_basic = self.get_daily_basic(code)
        klines = self.get_recent_klines(code, days=120)  # 120日：满足 MA60/MACD/KDJ 计算窗口
        fina = self.get_financial_indicator(code)           # 财务指标(基本面)
        val_hist = self.get_daily_basic_history(code, days=30)  # 估值历史(分位)

        context_parts = []

        # 1. 基本信息
        if basic_info:
            context_parts.append("### 股票基本信息")
            context_parts.append(f"- 股票代码: {basic_info.get('ts_code')}")
            context_parts.append(f"- 股票名称: {basic_info.get('name')}")
            context_parts.append(f"- 所属行业: {basic_info.get('industry')}")
            context_parts.append(f"- 所在地域: {basic_info.get('area')}")
            context_parts.append(f"- 市场板块: {basic_info.get('market')}")

        # 2. 估值和市值（当日）
        if daily_basic:
            context_parts.append("\n### 日度基本指标 (估值/市值)")
            context_parts.append(f"- 收盘价: {daily_basic.get('close', 'N/A')}")
            context_parts.append(f"- 换手率 (%): {daily_basic.get('turnover_rate', 'N/A')}")
            context_parts.append(f"- 市盈率 (PE TTM): {daily_basic.get('pe_ttm', 'N/A')}")
            context_parts.append(f"- 市净率 (PB): {daily_basic.get('pb', 'N/A')}")
            context_parts.append(f"- 总市值 (万元): {daily_basic.get('total_mv', 'N/A')}")
            context_parts.append(f"- 流通市值 (万元): {daily_basic.get('circ_mv', 'N/A')}")

        # 3. 估值历史分位（用于估值打分：当前 PE/PB vs 近 30 天自身分位）
        if not val_hist.empty:
            context_parts.append("\n### 估值历史分位 (近30交易日，用于估值打分)")
            pe_series = val_hist['pe_ttm'].dropna()
            pb_series = val_hist['pb'].dropna()
            cur_pe = daily_basic.get('pe_ttm') if daily_basic else None
            cur_pb = daily_basic.get('pb') if daily_basic else None
            if not pe_series.empty and cur_pe is not None:
                pe_pct = (pe_series < cur_pe).sum() / len(pe_series) * 100
                context_parts.append(f"- PE TTM 当前 {cur_pe:.2f}，近30日分位 {pe_pct:.0f}%（0%=最低，100%=最高）")
                context_parts.append(f"- PE TTM 近30日区间: {pe_series.min():.2f} ~ {pe_series.max():.2f}")
            if not pb_series.empty and cur_pb is not None:
                pb_pct = (pb_series < cur_pb).sum() / len(pb_series) * 100
                context_parts.append(f"- PB 当前 {cur_pb:.2f}，近30日分位 {pb_pct:.0f}%")

        # 4. 财务指标（基本面打分核心依据）
        if fina:
            context_parts.append("\n### 财务指标 (最新报告期，基本面打分依据)")
            context_parts.append(f"- ROE (%): {fina.get('roe', 'N/A')}")
            context_parts.append(f"- 毛利率 (%): {fina.get('grossprofit_margin', 'N/A')}")
            context_parts.append(f"- 净利润同比 (%): {fina.get('netprofit_yoy', 'N/A')}")
            context_parts.append(f"- 资产负债率 (%): {fina.get('debt_to_assets', 'N/A')}")
            context_parts.append(f"- 报告期: {fina.get('end_date', 'N/A')}")
        else:
            context_parts.append("\n### 财务指标: 数据缺失（fina_indicator 未返回）")

        # 5. 技术面：确定性结构化信号 (替代原始K线表 — 让 LLM 据公式结果打分，而非"看图")
        context_parts.append("\n" + self._build_technical_signals(klines, code))

        # 6. 市场情绪与综合评价 (akshare/东财，供参考不替代客观评分)
        context_parts.append("\n" + self._build_market_sentiment(code))

        if not context_parts:
            return f"无法获取股票 {code} 的相关信息。"

        return "\n".join(context_parts)

tushare_fetcher = TushareContextFetcher()
