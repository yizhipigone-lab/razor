# -*- coding: utf-8 -*-
"""Critical fixes regression tests.

每个 test_* 函数对应一个已知风险点的护栏,失败 = 防线被破。
"""
from pathlib import Path


def test_pytest_does_not_collect_scripts_directory():
    """pytest.ini 必须排除 scripts/ 下的 test_*.py,即使外部跑 pytest scripts/

    scripts/test_fix_*.py 共 28 个,命名匹配 python_files = test_*.py,
    当前 testpaths = tests 保护 pytest 不主动收, 但 `pytest scripts/` 显式跑会触发误收集。
    必须 addopts = --ignore=scripts 才安全。
    """
    cfg = Path("pytest.ini").read_text(encoding="utf-8")
    assert "addopts" in cfg, "pytest.ini 缺 [pytest].addopts 段"
    assert "--ignore=scripts" in cfg, "缺 --ignore=scripts 防御 scripts/test_fix_*.py 误收集"