"""
通达信/同花顺 公式转 Python 指标库
基于 MyTT (https://github.com/mpquant/MyTT) v3.3

使用方法:
    from app.indicators import MACD, KDJ, RSI, MA, EMA, CROSS, HHV, LLV

0级核心函数: REF DIFF MA EMA SMA WMA HHV LLV STD SUM DMA ...
1级应用函数: COUNT EVERY EXIST CROSS BARSLAST FILTER ...
2级技术指标: MACD KDJ RSI BOLL WR BIAS CCI ATR DMI ...
"""

from app.indicators.MyTT import (
    # 0级核心工具函数
    RD, RET, ABS, LN, POW, SQRT, SIN, COS, TAN,
    MAX, MIN, IF, REF, DIFF, STD, SUM, CONST,
    HHV, LLV, HHVBARS, LLVBARS,
    MA, EMA, SMA, WMA, DMA,
    AVEDEV, SLOPE, FORCAST, LAST,
    # 1级应用层函数
    COUNT, EVERY, EXIST, FILTER,
    BARSLAST, BARSLASTCOUNT, BARSSINCEN,
    CROSS, LONGCROSS, VALUEWHEN, BETWEEN,
    TOPRANGE, LOWRANGE,
    # 2级技术指标
    MACD, KDJ, RSI, WR, BIAS, BOLL, PSY, CCI, ATR, BBI,
    DMI, TAQ, KTN, TRIX, VR, CR, EMV, DPO, BRAR, DFMA,
    MTM, MASS, ROC, EXPMA, OBV, MFI, ASI, XSII,
)

from app.indicators.MyTT_plus import (
    HHV as HHV_seq,
    LLV as LLV_seq,
    DSMA,
    SUMBARSFAST,
    SAR,
    TDX_SAR,
)

__version__ = "3.3"
