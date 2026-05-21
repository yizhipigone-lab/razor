"""
通达信公式 → Python (严格使用 MyTT 能力转换)

原公式：MA5角度金叉选股信号

转换说明：
- PLOYLINE 是通达信绘图函数，无法在 Python 中实现，返回底层数据 X_3_val / X_4_val
- CODELIKE / NAMELIKE 依赖通达信内置股票数据库，由外部筛选层处理
- 涨停幅度 LIMIT_PCT 根据板块不同：
    科创板(688) / 创业板(300,301): 19.5%
    北交所(8/4开头): 29%
    主板: 9.5%
"""

import numpy as np
from app.indicators import MA, REF, CROSS, COUNT, IF, MAX, MIN, ABS


def ma5_angle_signal(close, high=None, low=None, code_prefix=""):
    """
    MA5角度金叉选股信号

    Args:
        close: np.ndarray, 收盘价序列
        high: 未使用（此公式仅依赖收盘价）
        low: 未使用
        code_prefix: str, 股票代码前缀，用于判断涨跌幅限制
            '688' → 科创板 19.5%
            '300'/'301' → 创业板 19.5%
            '8'/'4' → 北交所 29%
            其他 → 主板 9.5%

    Returns:
        dict with keys:
            x1: MA5角度值序列
            x2: x1的5日均线
            xg: 原始金叉信号(bool)
            zt: 综合打分(0或100)
            zp: 最终信号(bool，综合信号且非涨停)
            not_limit: 非涨停(bool)
    """
    # ── X_1: MA5角度值 ──
    # ATAN((MA(C,5)/REF(MA(C,5),1)-1)*100)*180/π
    ma5 = MA(close, 5)
    ma5_ref = REF(ma5, 1)
    rate = (ma5 / ma5_ref - 1) * 100          # MA5变化率(%)
    x1 = np.arctan(rate) * 180 / np.pi         # 反正切转角度  (MyTT无ATAN,用np.arctan)

    # ── X_2: X_1的5日均线 ──
    x2 = MA(x1, 5)

    # ── X_3 / X_4: PLOYLINE绘图 → 返回底层条件数据 ──
    # PLOYLINE(CROSS(X_1,X_2), X_2) → 金叉时取X_2的值
    # PLOYLINE(CROSS(X_2,X_1), X_1) → 死叉时取X_1的值
    golden_cross = CROSS(x1, x2)               # 金叉：X_1上穿X_2
    death_cross = CROSS(x2, x1)                # 死叉：X_2上穿X_1
    x3_val = IF(golden_cross, x2, 0)           # 金叉位置标记X_2值
    x4_val = IF(death_cross, x1, 0)            # 死叉位置标记X_1值

    # ── XG: COUNT(CROSS(X_1,X_2), N) ──
    # 原公式 N = (X_3<REF(X_3,5) AND X_4>REF(X_4,5))
    # 当条件为True(N=1): COUNT返回最近1周期金叉次数(0或1)
    # 当条件为False(N=0): COUNT返回0
    # 等价于: CROSS(X_1,X_2) AND (X_3<REF(X_3,5)) AND (X_4>REF(X_4,5))
    cond_x3 = x3_val < REF(x3_val, 5)
    cond_x4 = x4_val > REF(x4_val, 5)
    xg = golden_cross & cond_x3 & cond_x4

    # ── XA: 股票筛选条件 ──
    # CODELIKE/NAMELIKE 依赖通达信内部数据库，Python 中由外部实现
    # 此处返回占位，调用方需自行过滤：
    #   NOT 688开头的科创板(排除300687)
    #   NOT 920/430/873开头
    #   NOT ST/*ST 股票
    # xa 始终为 True，由调用层处理

    # ── ZT: 信号打分 ──
    # ZT:=IF(XG AND XA AND COUNT(XG=1,20)=1, 100, 0)
    # 条件：金叉成立 + 股票合格 + 20日内首次出现信号
    xg_count_20 = COUNT(xg.astype(int), 20)
    first_in_20 = (xg_count_20 == 1)
    zt = IF(xg & first_in_20, 100, 0)

    # ── LIMIT_PCT: 根据板块判断涨跌幅限制 ──
    limit_pct = _get_limit_pct(code_prefix)

    # ── NOT_LIMIT: 非涨停过滤 ──
    chg_pct = close / REF(close, 1) - 1
    not_limit = chg_pct < limit_pct

    # ── ZP: 最终综合信号 ──
    zp = (zt > 0) & not_limit

    return {
        "x1": x1,
        "x2": x2,
        "x3_val": x3_val,
        "x4_val": x4_val,
        "xg": xg,
        "zt": zt,
        "not_limit": not_limit,
        "zp": zp,
    }


def _get_limit_pct(code_prefix: str) -> float:
    """根据股票代码前缀返回涨跌幅限制比例"""
    prefix = str(code_prefix).lstrip("0") or "0"
    if prefix.startswith(("688",)):
        return 0.195   # 科创板
    if prefix.startswith(("300", "301")):
        return 0.195   # 创业板
    if prefix.startswith(("8", "4")):
        return 0.29    # 北交所
    return 0.095       # 主板
