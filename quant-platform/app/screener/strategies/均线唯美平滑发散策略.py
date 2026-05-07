import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy

class SmoothFlowStrategy(BaseStrategy):
    """
    均线唯美平滑发散策略 (Smooth Flow Strategy)
    - 核心思想：抛弃所有指标交叉点，纯粹追求物理线条的美感。
    - 特征：拒绝所有深V反弹和假横盘。专门猎杀 5/10/20日均线如车道般“互不穿插、平滑向上、稳步敞开”的起爆信号。
    """
    name = "SmoothFlowStrategy"
    description = "均线唯美平滑发散策略：追求5/10/20日均线平滑向上、稳步敞开的起爆信号"

    def generate_signals(self, df: pd.DataFrame, market_df: pd.DataFrame = None, all_stock_df: pd.DataFrame = None) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=['code', 'date'])
            
        # 确保数据包含必要的列
        required_cols = ['code', 'date', 'open', 'high', 'low', 'close', 'volume']
        if not all(col in df.columns for col in required_cols):
            raise ValueError(f"DataFrame must contain columns: {required_cols}")
        
        # 为全量并发矩阵运算，一次性对齐所有行（按股票和时间流顺序）
        df = df.sort_values(['code', 'date']).copy()
        
        # ================= 模块1：主体均线与斜率引擎 (底层 C 并发) =================
        gb_close = df.groupby('code')['close']
        df['ma5'] = gb_close.transform(lambda x: x.rolling(5).mean())
        df['ma10'] = gb_close.transform(lambda x: x.rolling(10).mean())
        df['ma20'] = gb_close.transform(lambda x: x.rolling(20).mean())
        df['ma60'] = gb_close.transform(lambda x: x.rolling(60).mean())
        
        # 对齐时序的边界防溢出（跨股票 shift 安全处理）
        df['ma5_prev'] = df.groupby('code')['ma5'].shift(1)
        df['ma10_prev'] = df.groupby('code')['ma10'].shift(1)
        df['ma20_prev'] = df.groupby('code')['ma20'].shift(1)
        df['ma60_prev'] = df.groupby('code')['ma60'].shift(1)
        
        # 计算单日增量(绝对斜率)
        df['s5'] = df['ma5'] - df['ma5_prev']
        df['s10'] = df['ma10'] - df['ma10_prev']
        df['s20'] = df['ma20'] - df['ma20_prev']
        
        # ================= 模块2：形体美学的量化映射 =================
        # 特征A：秩序井然 (绝对的老幼尊卑多头排列)
        order_cond = (df['ma5'] > df['ma10']) & (df['ma10'] > df['ma20']) & (df['ma20'] > df['ma60'])
        df['order_ok'] = order_cond.groupby(df['code']).transform(lambda x: x.rolling(3).sum() == 3)
        
        # 特征B：平滑无转折 (三大均线全在朝右上方走)
        smooth_cond = (df['ma5'] > df['ma5_prev']) & (df['ma10'] > df['ma10_prev']) & (df['ma20'] > df['ma20_prev'])
        df['smooth_ok'] = smooth_cond.groupby(df['code']).transform(lambda x: x.rolling(3).sum() == 3)
        
        # 特征C：平滑扩散 (维持喇叭口扩大的完美加速度)
        diverge_cond = (df['s5'] > df['s10']) & (df['s10'] > df['s20']) & (df['s20'] > 0)
        df['diverge_ok'] = diverge_cond.groupby(df['code']).transform(lambda x: x.rolling(3).sum() == 3)
        
        
        # ================= 模块3：强力护盾与禁区控制 =================
        # 防护A：大底不崩。MA60不能是断崖式下跌
        ma60_change = (df['ma60_prev'] - df['ma60']) / df['ma60']
        df['ma60_flat'] = (df['ma60'] >= df['ma60_prev']) | (ma60_change < 0.001)
        
        # 防护B：拒绝追高买套。起爆点绝不位于高空
        df['no_chase'] = ((df['close'] / df['ma20'] - 1) * 100) < 15
        
        # 防护C：点火阳线。坚决上攻的实体大阳线
        df['close_prev'] = gb_close.shift(1)
        price_change = (df['close'] / df['close_prev'] - 1) * 100
        df['attack'] = (df['close'] > df['ma5']) & (df['close'] > df['open']) & (price_change > 2.0)

        low1 = df.groupby('code')['low'].shift(1)  # 昨天最低价
        low2 = df.groupby('code')['low'].shift(2)  # 前天最低价
        ma5_1 = df.groupby('code')['ma5'].shift(1)
        ma5_2 = df.groupby('code')['ma5'].shift(2)
        df['touch_ma5_2d'] = (low2 < ma5_2)

        # ================= 模块4：终极神圣信号熔铸 =================
        # 叠加六大维度的特征封锁线
        za_cond = (df['order_ok'] & df['smooth_ok'] & df['touch_ma5_2d']  & df['diverge_ok'] & 
                   df['ma60_flat'] & df['no_chase'] &  df['attack'])
        
        df['ZA'] = np.where(za_cond, 100, 0)
        
        # 排爆系统：采用纯 C 级 sum 累加
        df['signal_count_20d'] = df.groupby('code')['ZA'].transform(lambda x: x.rolling(20, min_periods=1).sum() / 100)
        df['ZP'] = (df['ZA'] == 100) & (df['signal_count_20d'] == 1)
        
 

        # 生成标准响应并剔除噪音列 (修正逻辑)
        final_buy = df[df['ZP'] ]
        return final_buy[['code', 'date', 'close']].copy()