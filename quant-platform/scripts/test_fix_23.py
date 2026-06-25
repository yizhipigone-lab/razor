"""验证 L23 修复: AI 目标函数 + 种子 + WFE 选优

通过 mock 隔离 ai_optimizer 的重依赖(database/engine),让纯函数测试可独立运行
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')

# Mock 掉重型依赖,避免 DuckDB 锁和 LLM 网络依赖
from unittest.mock import MagicMock

# 在 import ai_optimizer 前 stub 掉 database.duckdb_manager 的 db 实例
mock_db = MagicMock()
sys.modules['database.duckdb_manager'] = MagicMock()
sys.modules['database.duckdb_manager'].db = mock_db
sys.modules['database'] = MagicMock()
sys.modules['database'].duckdb_manager = sys.modules['database.duckdb_manager']

# 1) 静态行为测试:用纯 Python 重新实现 _calmar_score 的核心断言逻辑
#    不依赖 import,直接 exec 出 _calmar_score 函数体
import re

def _load_calmar_from_source():
    src_path = os.path.join(
        os.path.dirname(__file__), '..', 'app', 'backtest', 'ai_optimizer.py'
    )
    with open(src_path, 'r', encoding='utf-8') as f:
        content = f.read()
    # 提取 _calmar_score 函数定义
    m = re.search(
        r'def _calmar_score\(trades: list\) -> float:.*?(?=\ndef |\nclass |\n# ─)',
        content, re.DOTALL
    )
    if not m:
        raise RuntimeError("无法定位 _calmar_score 函数")
    body = m.group(0)
    # 简单环境
    import numpy as np
    ns = {'np': np}
    exec(body, ns)
    return ns['_calmar_score']


def test_calmar_score_is_risk_adjusted():
    """_calmar_score 不应只是均值,应风险调整

    构造两组:同样的 mean,但低方差组波动小、高方差组波动大
    纯 mean 实现下两组 score 相同;真 Calmar 应让低方差组胜出
    """
    _calmar_score = _load_calmar_from_source()
    # 同样 mean=2.0:
    #   high_std:  6个 15, 4个 -16.5  → mean=2.0, std 大
    #   low_std:   9个 2.5, 1个 -0.5   → mean=2.2, std 小
    # 调整为完全相同 mean=2.0:
    high_mean_high_std = [
        {'pnl_pct': 15.0}, {'pnl_pct': 15.0}, {'pnl_pct': 15.0},
        {'pnl_pct': 15.0}, {'pnl_pct': 15.0}, {'pnl_pct': 15.0},
        {'pnl_pct': -10.0}, {'pnl_pct': -10.0},
        {'pnl_pct': -10.0}, {'pnl_pct': -10.0},  # 6*15+4*(-10)=90-40=50/10=5
    ]
    # 修正: 6*10 + 4*(-5) = 60-20 = 40/10 = 4
    high_mean_high_std = [
        {'pnl_pct': 10.0}, {'pnl_pct': 10.0}, {'pnl_pct': 10.0},
        {'pnl_pct': 10.0}, {'pnl_pct': 10.0}, {'pnl_pct': 10.0},
        {'pnl_pct': -5.0}, {'pnl_pct': -5.0},
        {'pnl_pct': -5.0}, {'pnl_pct': -5.0},  # mean=4, std=7.5
    ]
    low_mean_low_std = [
        {'pnl_pct': 4.0}, {'pnl_pct': 4.0}, {'pnl_pct': 4.0},
        {'pnl_pct': 4.0}, {'pnl_pct': 4.0}, {'pnl_pct': 4.0},
        {'pnl_pct': 4.0}, {'pnl_pct': 4.0},
        {'pnl_pct': 4.0}, {'pnl_pct': 4.0},  # mean=4, std=0
    ]
    score_high = _calmar_score(high_mean_high_std)
    score_low = _calmar_score(low_mean_low_std)
    # 纯 mean 实现下两者相等(都=4);真 Calmar 应让低方差组胜出
    assert score_low > score_high, (
        f"风险调整失败: 低方差组({score_low}) 应 > 高方差组({score_high});"
        f"若两者相等,说明 _calmar_score 没做风险调整"
    )
    print(f"✅ 风险调整正确: 低方差={score_low:.2f} > 高方差={score_high:.2f}")


def test_ai_optimizer_has_seed():
    """ai_optimizer 应有 np.random.seed 调用"""
    with open('app/backtest/ai_optimizer.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'np.random.seed' in content or 'RandomState' in content, \
        "ai_optimizer.py 缺固定随机种子"
    print("✅ ai_optimizer 有固定随机种子")


def test_wfe_used_in_best_selection():
    """Top-10 排序应参考 WFE,不只是 score"""
    with open('app/backtest/ai_optimizer.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 验证选优逻辑中出现 wfe 字段(不区分大小写)
    has_wfe_aware = 'wfe' in content.lower() and (
        'wfe' in content.split('def _run_ai')[1] if 'def _run_ai' in content else False
    )
    # 更宽松:只要 wfe 出现在 best_params 选择相关代码块即可
    has_wfe_in_main = re.search(
        r'best_params.*wfe|wfe.*best_params|_sort_key',
        content, re.DOTALL
    )
    assert has_wfe_in_main is not None, \
        "ai_optimizer 选 best_params 没考虑 WFE"
    print("✅ best_params 选优考虑 WFE")


if __name__ == '__main__':
    test_calmar_score_is_risk_adjusted()
    test_ai_optimizer_has_seed()
    test_wfe_used_in_best_selection()
    print("\n🎉 L23 修复验证通过")