"""
指数代码统一映射表 — 全平台唯一真相来源 (Single Source of Truth)

覆盖中证、上证、深证三大系列的主流指数。
QQT sync INDEX_QMT_SECTOR 映射获取成分股。
"""
from typing import Optional

# 内部 Key → Tushare 代码
INDEX_MAP: dict[str, str] = {
    # 上证系列
    "SH_COMP": "000001.SH",   # 上证指数 (全市场)
    "SH50":    "000016.SH",   # 上证50
    "SH180":   "000010.SH",   # 上证180
    "SH380":   "000009.SH",   # 上证380
    # 中证系列
    "HS300":   "000300.SH",   # 沪深300
    "ZZ500":   "000905.SH",   # 中证500
    "ZZ1000":  "000852.SH",   # 中证1000
    "ZZA500":  "000510.SH",   # 中证A500
    # 深证系列
    "SZ_COMP": "399001.SZ",   # 深证成指
    "SZ100":   "399004.SZ",   # 深证100
    "SZ200":   "399009.SZ",   # 深证200
    "SZ300":   "399007.SZ",   # 深证300
    # 主题指数
    "KCB50":   "000688.SH",   # 科创50
    "CYB":     "399006.SZ",   # 创业板指
}

# 反向映射：Tushare 代码 → 内部 Key
INDEX_MAP_REVERSE: dict[str, str] = {v: k for k, v in INDEX_MAP.items()}

# 显示名称映射
INDEX_DISPLAY: dict[str, str] = {
    "SH_COMP": "上证指数",
    "SH50":    "上证50",
    "SH180":   "上证180",
    "SH380":   "上证380",
    "HS300":   "沪深300",
    "ZZ500":   "中证500",
    "ZZ1000":  "中证1000",
    "ZZA500":  "中证A500",
    "SZ_COMP": "深证成指",
    "SZ100":   "深证100",
    "SZ200":   "深证200",
    "SZ300":   "深证300",
    "KCB50":   "科创50",
    "CYB":     "创业板指",
}

# QMT xtdata.get_stock_list_in_sector 接受的板块名称
INDEX_QMT_SECTOR: dict[str, str] = {
    "SH_COMP": "上证指数",
    "SH50":    "上证50",
    "SH180":   "上证180",
    "SH380":   "上证380",
    "HS300":   "沪深300",
    "ZZ500":   "中证500",
    "ZZ1000":  "中证1000",
    "ZZA500":  "中证A500",
    "SZ_COMP": "深证成指",
    "SZ100":   "深证100",
    "SZ200":   "深证200",
    "SZ300":   "深证300",
    "KCB50":   "科创50",
    "CYB":     "创业板指",
}

# 静态备份 CSV 的路径（相对于项目根目录）
INDEX_BACKUP_DIR = "data/meta/index_backup"


def get_display_name(key: str) -> str:
    return INDEX_DISPLAY.get(key, key)


def get_qmt_sector_name(key: str) -> Optional[str]:
    return INDEX_QMT_SECTOR.get(key)


def get_ts_code(key: str) -> Optional[str]:
    return INDEX_MAP.get(key)
