import pandas as pd
import numpy as np

def generate_signals(df: pd.DataFrame, market_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    【双级趋势突破策略 V2.0】
    
    逻辑优化自投研中心：
    - 大趋势：要求股价站稳 60 日线，且 60 日趋势向上 (确认趋势反转/筑底完成)。
    - 小信号：要求股价放量突破 20 日阻力位 (寻找主升浪切入点)。
    """
    if df is None or len(df) < 70: # 需要 60日均线，数据量需更足
        return pd.DataFrame()
        
    df = df.copy()
    g = df.groupby('code', group_keys=False)
    
    # ─── 1. 大趋势过滤：60 日生命线 ───────────────────────────────
    df['ma60'] = g['close'].transform(lambda x: x.rolling(60).mean())
    df['ma60_prev'] = g['ma60'].transform(lambda x: x.shift(1))
    
    # 判定大势：股价在60日线上方，且60日线向上走平或抬升
    cond_ma60_up = (df['close'] > df['ma60']) & (df['ma60'] >= df['ma60_prev'])
    
    # ─── 2. 小阻力突破：20 日高点 ───────────────────────────────
    # 近 20 日阻力位：取过去 20 日(不含今日)的最高收盘价
    df['res_20'] = g['close'].transform(lambda x: x.shift(1).rolling(20).max())
    
    # ─── 3. 成交量配合判定 ─────────────────────────────────────
    # 今日成交量 > 过去 20 日均量的 1.5 倍
    df['avg_vol_20'] = g['volume'].transform(lambda x: x.shift(1).rolling(20).mean())
    df['vol_ratio'] = df['volume'] / (df['avg_vol_20'] + 1e-6)
    
    # ─── 4. 综合信号组合 ──────────────────────────────────────
    # A. 处于大趋势向上期 (大周期安全)
    # B. 股价突破最近一个月的阻力位 (小周期反转)
    # C. 今日放量 50% 以上 (主力进场确认)
    # D. 收阳线
    
    df['buy'] = (
        cond_ma60_up & 
        (df['close'] > df['res_20']) & 
        (df['vol_ratio'] >= 1.5) & 
        (df['close'] > df['open'])
    )
    
    # 仅保留首个信号点，避免连续提示
    df['buy_signal'] = df['buy'] & (~g['buy'].transform(lambda x: x.shift(1)).fillna(False))
    
    # 日期标准化
    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.date

    result = df[df['buy_signal'] == True].copy()

    # ─── 5. 板块评分排序（非阻塞，增强维度） ─────────────────
    # 注意：screener/engine.py 会在 merge 后统一注入 sector_score/concept_score/total_score
    # 此处仅保留排序逻辑，评分由引擎统一处理
    result['sector_score'] = 0.0

    return result

PARAMS = {
    "description": "【双向突破选股】：站稳 60 日生命线确认中长线大势开启，放量突破 20 日阻力位捕捉短期爆发点。这是机构级右侧交易的高胜率组合。",
    "use_sector_ranking": True,
}
