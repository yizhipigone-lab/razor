import pandas as pd
import numpy as np

def generate_signals(df: pd.DataFrame, market_df: pd.DataFrame = None, all_stock_df: pd.DataFrame = None) -> pd.DataFrame:
    """
    终极版【小步慢跑 + 长波波长 + 板块共振】策略
    
    核心进阶逻辑：
    1. 短趋势：MA5/10/20 多头排列且发散 3 天，金叉在 3 日内。
    2. 长趋势：所有均线均在 MA60 之上，且 MA60 呈现“阶梯式”稳步抬升。
    3. K线力量：收红盘，且高位抛压极小（上影线极短）。
    4. 量能与板块：保持 3日均量 > 5日均量的活跃度，并通过板块共振过滤。
    """
    # 基础数据校验：由于增加 MA60，至少需要 70 条数据
    if df is None or len(df) < 70:
        return pd.DataFrame()
        
    df = df.copy()
    date_col = "date" if "date" in df.columns else "datetime"
    df[date_col] = pd.to_datetime(df[date_col]).dt.normalize()
    
    # ─── 0. 板块共振过滤 ──────────────────────────────────────
    if all_stock_df is not None and 'sector' in all_stock_df.columns:
        stock_meta = all_stock_df.set_index('code')['sector'].to_dict()
        df['sector_name'] = df['code'].map(stock_meta)
        if market_df is not None and not market_df.empty:
            market_df['sector_name'] = market_df['code'].map(stock_meta)
            sector_perf = market_df.groupby('sector_name')['pct_chg'].mean()
            df['sector_pct'] = df['sector_name'].map(sector_perf).fillna(0)
            df['cond_sector_strong'] = df['sector_pct'] > -0.5 
        else:
            df['cond_sector_strong'] = True
    else:
        df['cond_sector_strong'] = True
    
    g = df.groupby('code', group_keys=False)

    # ─── 1. 均线库设计 ──────────────────────────────────────────
    df['ma5'] = g['close'].transform(lambda x: x.rolling(5).mean())
    df['ma10'] = g['close'].transform(lambda x: x.rolling(10).mean())
    df['ma20'] = g['close'].transform(lambda x: x.rolling(20).mean())
    df['ma60'] = g['close'].transform(lambda x: x.rolling(60).mean())
    
    # 多头排列判定
    df['long_arrangement'] = (df['ma5'] > df['ma10']) & (df['ma10'] > df['ma20'])
    df['ma5_up'] = df['ma5'] > g['ma5'].transform(lambda x: x.shift(1))
    df['ma10_up'] = df['ma10'] > g['ma10'].transform(lambda x: x.shift(1))
    df['ma20_up'] = df['ma20'] > g['ma20'].transform(lambda x: x.shift(1))
    df['diverge_up'] = df['ma5_up'] & df['ma10_up'] & df['ma20_up']
    
    # ─── 2. MA60 趋势逻辑 (阶梯式抬升) ──────────────────────────
    # 用户逻辑：(T + T-1) > (T-1 + T-2) 意味着整体趋势在加力
    df['ma60_shift1'] = g['ma60'].transform(lambda x: x.shift(1))
    df['ma60_shift2'] = g['ma60'].transform(lambda x: x.shift(2))
    df['ma60_sum_prev'] = df['ma60_shift1'] + df['ma60_shift2']
    df['ma60_sum_curr'] = df['ma60'] + df['ma60_shift1']
    # 这种“两日合力”法能有效过滤单日震荡，捕捉真实趋势
    df['ma60_is_lifting'] = df['ma60_sum_curr'] > df['ma60_sum_prev']
    # 要求最近 10 天中有 80% 以上的时间满足这种抬升趋势
    df['cond_ma60_trend'] = g['ma60_is_lifting'].transform(lambda x: x.rolling(10).sum() >= 8)
    
    # 指标均在 MA60 之上
    df['cond_above_60'] = (df['ma5'] > df['ma60']) & (df['ma10'] > df['ma60']) & (df['ma20'] > df['ma60'])
    
    # ─── 3. K 线力量与形态 ────────────────────────────────────
    # 1. 红盘 (收盘价 > 开盘价)
    df['is_red'] = df['close'] > df['open']
    
    # 2. 上影线限制 (最高价不能过高于收盘价)
    # 算法：上影线长度 / 实体长度 <= 0.3 (实体占主导，抛压轻)
    df['body_height'] = (df['close'] - df['open']).abs()
    df['upper_shadow'] = df['high'] - df['close']
    # 避免分母为 0，给个极小值
    df['cond_no_upper_shadow'] = df['upper_shadow'] / (df['body_height'] + 1e-6) <= 0.3
    
    # ─── 4. 其它原有优势逻辑 ──────────────────────────────────
    # [金叉] 近 3 日发生金叉
    df['ma5_prev'] = g['ma5'].transform(lambda x: x.shift(1))
    df['ma10_prev'] = g['ma10'].transform(lambda x: x.shift(1))
    df['cross_up'] = (df['ma5'] > df['ma10']) & (df['ma5_prev'] <= df['ma10_prev'])
    df['had_cross_recently'] = g['cross_up'].transform(lambda x: x.rolling(3).sum() > 0)
    
    # [量能] 3日均量 > 5日均量
    df['cond_vol_growing'] = g['volume'].transform(lambda x: x.rolling(3).mean()) > g['volume'].transform(lambda x: x.rolling(5).mean())
    
    # [爬坡] 近 3 天涨幅 0-5%，连续红盘
    df['close_prev'] = g['close'].transform(lambda x: x.shift(1))
    df['pct_chg'] = (df['close'] / df['close_prev'] - 1) * 100
    df['is_climbing'] = (df['pct_chg'] > 0) & (df['pct_chg'] <= 5)
    df['cond_climb_3d'] = g['is_climbing'].transform(lambda x: x.rolling(3).sum() == 3)
    
    # [破位] 15日新高
    df['cond_new_high'] = df['close'] >= g['close'].transform(lambda x: x.rolling(15).max())
    
    # ─── 5. 最终总攻信号 ──────────────────────────────────────
    df['buy'] = (
        g['long_arrangement'].transform(lambda x: x.rolling(3).sum() == 3) & # 短趋势齐整
        g['diverge_up'].transform(lambda x: x.rolling(3).sum() == 3) &        # 短发散齐整
        df['had_cross_recently'] &
        df['cond_vol_growing'] &
        df['cond_climb_3d'] &
        df['cond_new_high'] &
        df['cond_sector_strong'] &
        df['cond_above_60'] &      # 位于生命线之上
        df['cond_ma60_trend'] &    # 生命线稳步抬升
        df['is_red'] &             # 今日红盘
        df['cond_no_upper_shadow'] # 无显著抛压
    )
    
    df['buy_signal'] = df['buy'] & (~g['buy'].transform(lambda x: x.shift(1)).fillna(False))
    
    df[date_col] = df[date_col].dt.date
    return df[df['buy_signal'] == True].copy()

PARAMS = {
    "description": "终极版【小步慢跑+生命线共振】：结合 MA60 阶梯式抬升背景，寻找在强势板块中温和放量、无上影线干扰、强势突破 15 日高点的短长趋势共振个股。"
}
