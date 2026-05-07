import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy

class SmoothFlowStrategyV2(BaseStrategy):
    """
    均线唯美平滑发散策略 Pro (Smooth Flow v2.0)
    - 改进点：强化平滑度约束，收紧乖离率，增加量能确认，过滤高位脉冲。
    - 追求如“高速公路匀速并入主道”般的优美起爆。
    """
    name = "SmoothFlowStrategyV2"
    description = "均线唯美平滑发散 v2.0：通过加速度控制和乖离率优化，过滤高位巨震，锁定健康升浪。"

    def generate_signals(self, df: pd.DataFrame, market_df: pd.DataFrame = None, all_stock_df: pd.DataFrame = None) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=['code', 'date'])
            
        df = df.sort_values(['code', 'date']).copy()
        
        # ================= 模块1：均线引擎与高阶斜率 =================
        gb_close = df.groupby('code')['close']
        df['ma5'] = gb_close.transform(lambda x: x.rolling(5).mean())
        df['ma10'] = gb_close.transform(lambda x: x.rolling(10).mean())
        df['ma20'] = gb_close.transform(lambda x: x.rolling(20).mean())
        df['ma60'] = gb_close.transform(lambda x: x.rolling(60).mean())
        
        # 偏移量计算
        df['ma5_prev'] = df.groupby('code')['ma5'].shift(1)
        df['ma10_prev'] = df.groupby('code')['ma10'].shift(1)
        df['ma20_prev'] = df.groupby('code')['ma20'].shift(1)
        
        # 斜率（变动额）
        df['s5'] = df['ma5'] - df['ma5_prev']
        df['s10'] = df['ma10'] - df['ma10_prev']
        df['s20'] = df['ma20'] - df['ma20_prev']
        
        # 这里的 prev_s5 用于检测“加速度”
        df['s5_prev'] = df.groupby('code')['s5'].shift(1)
        
        # ================= 模块2：唯美平滑发散约束 =================
        # 特征A：多头排列稳定性 (核心优化：要求必须持续排列 5 天以上)
        # 彻底过滤图一这种“刚交叉就想发散”的伪信号
        order_cond_raw = (df['ma5'] > df['ma10']) & (df['ma10'] > df['ma20']) & (df['ma20'] > df['ma60'])
        df['order_ok'] = order_cond_raw.groupby(df['code']).transform(lambda x: x.rolling(5).sum() == 5)
        
        # 特征B：持续性发散与稳步张口 (Steady Opening)
        # 计算 5日线与 20日线的间距 (Gap)
        df['gap_5_20'] = df['ma5'] - df['ma20']
        df['gap_prev'] = df.groupby('code')['gap_5_20'].shift(1)
        
        # 唯美要求：间距必须是连续 3 天在扩张，且扩张力度不能是突变
        diverge_logic = (df['gap_5_20'] > df['gap_prev']) & (df['s5'] > df['s10']) & (df['s10'] > df['s20']) & (df['s20'] > 0)
        df['diverge_stable'] = diverge_logic.groupby(df['code']).transform(lambda x: x.rolling(3).sum() == 3)

        # 特征C：加速度控制 (核心优化：防止图二中的垂直起飞)
        # 要求 MA5 的斜率增长不能超过昨天的 2.5 倍，确保“平滑”
        df['accel_ok'] = (df['s5'] > 0) & ((df['s5_prev'] <= 0) | (df['s5'] / (df['s5_prev'] + 0.0001) < 2.5))
        
        # 特征D：美学间距 (不能离得太开，也不能挤在一起)
        # MA5/MA20 的差距在 1% 到 6% 之间属于“唯美初探”
        gap_ratio = df['ma5'] / df['ma20']
        df['aesthetic_gap'] = (gap_ratio > 1.01) & (gap_ratio < 1.06)

        # ================= 模块3：安全护盾 (针对图二痛点) =================
        # 护盾A：严格乖离率 (核心优化：收紧到 8%)
        # 股价距离 20日均线太远坚决不买
        df['bias_safety'] = ((df['close'] / df['ma20'] - 1) * 100) < 8.0
        
        # 护盾B：量能确认 (温和放量)
        df['v_ma20'] = df.groupby('code')['volume'].transform(lambda x: x.rolling(20).mean())
        # 要求成交量是均量的 1.1x ~ 2.8x 之间，排除缩量诱多和巨量力竭
        df['volume_ok'] = (df['volume'] > df['v_ma20'] * 1.1) & (df['volume'] < df['v_ma20'] * 2.8)
        
        # 护盾C：实体强度
        df['close_prev'] = gb_close.shift(1)
        change_pct = (df['close'] / df['close_prev'] - 1) * 100
        # 拒绝一字变态涨停，追求 2%~7% 的实体阳线
        df['attack_ok'] = (df['close'] > df['open']) & (change_pct > 2.0) & (change_pct < 9.5)

        # ================= 模块4：信号融合 =================
        final_cond = (
            df['order_ok'] & 
            df['diverge_stable'] & 
            df['accel_ok'] & 
            df['aesthetic_gap'] & 
            df['bias_safety'] & 
            df['volume_ok'] & 
            df['attack_ok']
        )
        
        # 确保是首次触发
        df['ZA'] = np.where(final_cond, 100, 0)
        df['signal_count_10d'] = df.groupby('code')['ZA'].transform(lambda x: x.rolling(10, min_periods=1).sum() / 100)
        df['ZP'] = (df['ZA'] == 100) & (df['signal_count_10d'] == 1)
        
        final_buy = df[df['ZP']]
        return final_buy[['code', 'date', 'close']].copy()
