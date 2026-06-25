"""验证 L25 修复: 统一成交执行层"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_get_limit_up_pct():
    """A 股各板块涨停幅度"""
    from app.backtest.execution import get_limit_up_pct
    assert abs(get_limit_up_pct('300750') - 0.20) < 1e-6  # 创业 ±20%
    assert abs(get_limit_up_pct('688981') - 0.20) < 1e-6  # 科创 ±20%
    assert abs(get_limit_up_pct('830123') - 0.30) < 1e-6  # 北证 ±30%
    assert abs(get_limit_up_pct('600519') - 0.10) < 1e-6  # 主板 ±10%
    print("✅ 涨停幅度表正确")


def test_can_buy_normal():
    """非涨停: 可买"""
    from app.backtest.execution import can_buy
    ok, msg = can_buy('600519', prev_close=100.0, today_high=103.0)
    assert ok, f"3% 涨幅应可买,实际: {msg}"
    print(f"✅ 3% 涨幅可买: {msg}")


def test_can_buy_limit_up():
    """一字涨停: 不可买"""
    from app.backtest.execution import can_buy
    ok, msg = can_buy('300750', prev_close=100.0, today_high=120.0)  # 20% 涨停
    assert not ok, "20% 涨停应不可买"
    assert '涨停' in msg
    print(f"✅ 20% 涨停不可买: {msg}")


def test_can_sell_today():
    """T+1: 买入当天不能卖"""
    from app.backtest.execution import can_sell_today
    from datetime import date
    d1 = date(2026, 1, 5)
    assert not can_sell_today(d1, d1), "当天买入应不能卖"
    assert can_sell_today(d1, date(2026, 1, 6)), "次日可卖"
    print("✅ T+1 约束正确")


def test_calc_buy_cost():
    """买入成本含佣金 + 滑点"""
    from app.backtest.execution import calc_buy_cost
    result = calc_buy_cost(price=10.0, shares=1000)
    # 1000 * 10 = 10000
    # 佣金: max(10000 * 0.00025, 5) = 5.0 (最低)
    # 滑点: 10000 * 0.001 = 10
    # 总成本: 10000 + 5 + 10 = 10015
    assert result['total'] > 10000, "买入成本应 > 10000"
    assert result['commission'] >= 5.0
    assert result['slippage'] == 10.0
    print(f"✅ 买入成本: {result}")


def test_calc_sell_revenue():
    """卖出收入扣佣金 + 印花 + 滑点"""
    from app.backtest.execution import calc_sell_revenue
    result = calc_sell_revenue(price=11.0, shares=1000)
    # 1000 * 11 = 11000
    # 扣: 佣金(>=5) + 印花(11000*0.0005=5.5) + 滑点(11)
    # 净收入: 11000 - 5 - 5.5 - 11 = 10778.5
    assert result['total'] < 11000, "卖出净收入应 < 11000"
    assert result['stamp_tax'] == 5.5
    print(f"✅ 卖出收入: {result}")


def test_engine_uses_execution():
    """engine.py 应引用 execution.py"""
    with open('app/backtest/engine.py', 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'from app.backtest.execution import' in content, "engine.py 未引用 execution.py"
    print("✅ engine.py 已引用 execution.py")


if __name__ == '__main__':
    test_get_limit_up_pct()
    test_can_buy_normal()
    test_can_buy_limit_up()
    test_can_sell_today()
    test_calc_buy_cost()
    test_calc_sell_revenue()
    test_engine_uses_execution()
    print("\n🎉 L25 修复验证通过")
