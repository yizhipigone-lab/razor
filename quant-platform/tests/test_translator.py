"""
TDX 翻译器 V2 专项测试
- 验证递归下降解析器正确处理嵌套函数/逗号
- 验证 MyTT 函数映射
- 验证生成的代码可执行
"""
import sys
from pathlib import Path

import pytest

# 确保仓库根在 sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.strategy_factory.translator import translator


# ─── 基础翻译 ───────────────────────────────────────────────────

def test_basic_ma():
    """MA(C,5) → MA(c, 5)"""
    code = translator.translate("X:=MA(C,5);", "Test")
    assert "MA(c, 5)" in code
    assert "from app.indicators import MA" in code


def test_var_mapping():
    """C/O/H/L/V 变量映射"""
    code = translator.translate("X:=C+O+H+L+V;", "Test")
    # 左结合加法，所有变量应映射为小写
    for var in ['c', 'o', 'h', 'l', 'v']:
        assert var in code
    # 不应残留大写价量关键字
    assert 'C + O' not in code


def test_cross_function():
    """CROSS(MA(C,5),MA(C,10)) 金叉"""
    code = translator.translate("X:=CROSS(MA(C,5),MA(C,10));", "Test")
    assert "CROSS(MA(c, 5), MA(c, 10))" in code
    assert "CROSS" in code
    assert "MA" in code


def test_deeply_nested_functions():
    """深层嵌套: MA(CROSS(MA(C,5),MA(C,10)),3) — V1 正则会截断"""
    code = translator.translate("X:=MA(CROSS(MA(C,5),MA(C,10)),3);", "Test")
    # 应正确展开所有层
    assert "MA(c, 5)" in code
    assert "MA(c, 10)" in code
    assert "CROSS(MA(c, 5), MA(c, 10))" in code
    # 最外层 MA 应包裹 CROSS 结果
    assert "MA(CROSS(MA(c, 5), MA(c, 10)), 3)" in code


def test_if_with_nested_commas():
    """IF(COUNT(C>O,5)>3, 1, 0) — V1 的 [^,]+ 在 COUNT 内部逗号处截断"""
    code = translator.translate("X:=IF(COUNT(C>O,5)>3,1,0);", "Test")
    # IF 应正确识别3个参数
    assert "IF(" in code
    # COUNT 应完整保留
    assert "COUNT((c > o), 5)" in code


def test_and_or_logic():
    """AND/OR → &/|，布尔运算加括号"""
    code = translator.translate("X:=C>O AND C>REF(C,1);", "Test")
    assert "&" in code
    assert "(c > o)" in code


def test_comments_stripped():
    """{注释} 被剥离"""
    code = translator.translate("{这是注释} X:=MA(C,5);", "Test")
    assert "注释" not in code
    assert "MA(c, 5)" in code


def test_assignment_output_vs_intermediate():
    """:= 中间变量 vs : 输出变量"""
    code = translator.translate("A:=MA(C,5); B:A+1;", "Test")
    assert "a = MA(c, 5)" in code
    assert "b = (a + 1)" in code


def test_code_like():
    """CODELIKE('688') → _code_like(codes, '688')"""
    code = translator.translate("X:=CODELIKE('688');", "Test")
    assert "_code_like(codes, '688')" in code
    assert "def _code_like" in code


def test_atan():
    """ATAN → np.arctan (MyTT 无 ATAN)"""
    code = translator.translate("X:=ATAN((MA(C,5)/REF(MA(C,5),1)-1)*100)*180/3.14159;", "Test")
    assert "np.arctan(" in code
    assert "MA(c, 5)" in code


def test_unknown_function_warning():
    """未知函数保留并附警告注释"""
    code = translator.translate("X:=SOMEUNKNOWNFUNC(C,5);", "Test")
    assert "WARN" in code or "someunknownfunc" in code.lower()


def test_class_name():
    """策略类名正确"""
    code = translator.translate("X:=MA(C,5);", "MyCustom")
    assert "class MyCustomStrategy(BaseStrategy):" in code


def test_signal_output():
    """末尾裸表达式作为信号输出"""
    code = translator.translate("MA(C,5)>MA(C,20);", "Test")
    assert "_signal_out" in code
    assert "MA(c, 5)" in code
    assert "MA(c, 20)" in code


# ─── 生成代码可执行性（端到端）──────────────────────────────────

def test_generated_code_runs(tmp_path, monkeypatch):
    """翻译生成的策略代码能在 Mock 数据上跑通"""
    import numpy as np
    import pandas as pd

    formula = """
    MA5 := MA(C,5);
    MA20 := MA(C,20);
    BUY := CROSS(MA5, MA20);
    BUY;
    """
    code = translator.translate(formula, "TestE2E")

    # 执行翻译后的代码
    local_vars = {}
    import app.indicators  # 确保 MyTT 可用
    exec(code, local_vars)

    strat_cls = local_vars.get("TestE2EStrategy")
    assert strat_cls is not None, "策略类未生成"

    # 构造 Mock 数据
    rng = np.random.RandomState(42)
    n = 100
    bars = pd.DataFrame({
        "code": "000001.SZ",
        "date": pd.date_range("2024-01-01", periods=n),
        "open": rng.randn(n).cumsum() + 10,
        "high": rng.randn(n).cumsum() + 11,
        "low": rng.randn(n).cumsum() + 9,
        "close": rng.randn(n).cumsum() + 10,
        "volume": rng.randint(100, 1000, n),
    })

    strat = strat_cls()
    result = strat.generate_signals(bars)
    # 应返回 DataFrame（可能为空，但不应抛异常）
    assert isinstance(result, pd.DataFrame)


# ─── 真实公式回归 ───────────────────────────────────────────────

def test_real_ma5_angle_formula():
    """真实 MA5 角度公式能翻译并可执行"""
    formula = """
    MA5 := MA(C,5);
    MA20 := MA(C,20);
    X1 := ATAN((MA5/REF(MA5,1)-1)*100)*180/3.14159;
    X2 := MA(X1,5);
    CROSS_UP := X1 > X2 AND REF(X1,1) <= REF(X2,1);
    COND := CROSS_UP AND X2 < REF(X2,5);
    COND;
    """
    code = translator.translate(formula, "MA5Angle")
    assert "np.arctan" in code
    assert "CROSS_UP" not in code  # 变量名应小写
    assert "cross_up" in code
    # 语法正确性：能编译
    compile(code, "<test>", "exec")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
