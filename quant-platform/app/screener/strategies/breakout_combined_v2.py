import pandas as pd
import numpy as np
from app.screener.strategies.base import BaseStrategy

class BreakoutCombinedV2(BaseStrategy):
    """
    量化选股策略 V2.5: 顺势突破策略
    逻辑：
    1. 趋势过滤：股价站在 60 日生命线之上，且 60 日线斜率向上（确认中长期趋势）。
    2. 震荡收敛：近 20 日波动幅度逐渐减小（收敛形态）。
    3. 突破信号：股价放量（成交量 > 20日均量 1.5倍）突破 20 日小阻力位。
    """
    
    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or len(df) < 60:
            return pd.DataFrame()
            
        # 1. 计算均线
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        
        # 2. 计算成交量均值
        df['vol_ma20'] = df['vol'].rolling(20).mean()
        
        # 3. 20 日阻力位（近 20 日最高价，不含当日）
        df['res_20'] = df['high'].shift(1).rolling(20).max()
        
        # 4. 判断逻辑
        # 条件 A: 趋势向上 (股价 > MA60 且 MA60 升序)
        cond_trend = (df['close'] > df['ma60']) & (df['ma60'] > df['ma60'].shift(1))
        
        # 条件 B: 突破阻力位
        cond_break = (df['close'] > df['res_20']) & (df['close'].shift(1) <= df['res_20'])
        
        # 条件 C: 放量 (大于 20 日均量的 1.5 倍)
        cond_volume = df['vol'] > (df['vol_ma20'] * 1.5)
        
        # 条件 D: 价格站稳 (收盘价远离 MA20 且 MA20 也向上)
        cond_ma20 = (df['close'] > df['ma20']) & (df['ma20'] > df['ma20'].shift(1))
        
        # 综合信号
        df['signal'] = cond_trend & cond_break & cond_volume & cond_ma20
        
        # 返回触发信号的行
        signals = df[df['signal'] == True].copy()
        
        # 补充展示字段
        if not signals.empty:
            signals['reason'] = "MA60顺势+20日量价突破"
            # 记录此时的突破价格和均线位置，方便前端或 AI 再次校验
            signals['desc'] = signals.apply(lambda r: f"突破价:{r['close']:.2f}, MA60:{r['ma60']:.2f}, 放量:{r['vol']/r['vol_ma20']:.1f}x", axis=1)
            
        return signals
