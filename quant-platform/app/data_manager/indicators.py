import pandas as pd
import numpy as np
try:
    import talib
except ImportError:
    talib = None
    from loguru import logger
    logger.error("TA-Lib is not installed. Technical indicators will be disabled. Please install TA-Lib manually.")

def enrich_with_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    接收一只股票的历史全量 K 线 DataFrame，
    使用 TA-Lib 计算一系列基础与核心技术指标，丰富数据字典。
    前提：df 必须按时间正序排列，且包含 open, high, low, close, volume 列。

    ⚠️ 数据-C3 注意: 本函数在 batch_save_bars 写入时调用, 基于【写入时价格】算指标。
    日线前复权(adj_factor)在读取层(load_bars/_apply_qfq_by_code)进行, 因此存储到 parquet 的
    指标列(ma*/macd*/kdj*/rsi*/boll*/wr*)是基于【原始/写入价】, 可能与读取出的复权价不一致。
    现状: 全链路无人消费这些存储列——前端K线MA(calculateMA)、回测/选股策略(ma5_angle等)
    都用读取出的(已复权)close【自行重算】指标。这些存储列目前是冗余。
    若未来要直接消费存储指标列, 必须改为读取层复权后重算, 否则会与复权价错位。
    """
    if talib is None:
        return df
    if df.empty or len(df) < 5:
        return df

    # 兼容有时列名带有大写的情况
    cols = {c.lower(): c for c in df.columns}
    close = df[cols['close']].values
    high = df[cols['high']].values
    low = df[cols['low']].values
    
    # 1. 基础均线组 (MA)
    for period in [5, 10, 20, 30, 60, 120, 250]:
        df[f'ma{period}'] = talib.SMA(close, timeperiod=period)
        
    # 2. 动能共振 (MACD)
    macd, macdsignal, macdhist = talib.MACD(close, fastperiod=12, slowperiod=26, signalperiod=9)
    df['macd_dif'] = macd
    df['macd_dea'] = macdsignal
    # 按照中国股市习惯，MACD 柱子通常为 hist * 2
    df['macd_hist'] = macdhist * 2 if macdhist is not None else np.nan
    
    # 3. 摆动极值 (KDJ)
    # TA-Lib 的 STOCH 函数默认：fastk_period=5, slowk_period=3, slowd_period=3 (最常用的是 9,3,3)
    k, d = talib.STOCH(high, low, close, fastk_period=9, slowk_period=3, slowk_matype=0, slowd_period=3, slowd_matype=0)
    df['kdj_k'] = k
    df['kdj_d'] = d
    # J = 3*K - 2*D
    df['kdj_j'] = 3 * k - 2 * d
    
    # 4. 强弱博弈 (RSI)
    df['rsi_6'] = talib.RSI(close, timeperiod=6)
    df['rsi_12'] = talib.RSI(close, timeperiod=12)
    df['rsi_24'] = talib.RSI(close, timeperiod=24)
    
    # 5. 波动率禁区 (Bollinger Bands)
    upperband, middleband, lowerband = talib.BBANDS(close, timeperiod=20, nbdevup=2, nbdevdn=2, matype=0)
    df['boll_upper'] = upperband
    df['boll_mid'] = middleband
    df['boll_lower'] = lowerband
    
    # 6. 威廉指标 (Williams %R)
    # TA-Lib 的 WILLR 返回值是 -100 到 0 之间。
    # 传统国内炒股软件往往显示 0 到 100。为了对齐国内习惯，做个绝对值映射或加100等。
    # 标准公测：我们直接使用 talib 原理：(H_n - C)/(H_n - L_n) * -100
    # 为使用方便，将其转正，变成 100 到 0：
    wr = talib.WILLR(high, low, close, timeperiod=14)
    df['wr_14'] = wr * -1  # 转成 0~100 的范围（或者就直接保留原值，看策略需要）
    
    return df
