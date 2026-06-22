"""验证 #8 修复: Position.market_value 用 current_price, profit_pct 是 property

Bug: Position.market_value 旧实现用 entry_price 计算,导致实时权益失真(只反映建仓成本)。
Fix: 新增 current_price 字段(record() 阶段从 snapshot 写入),market_value/profit_pct 改用 current_price。
"""
import sys, os
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from datetime import date


def test_market_value_uses_current_price():
    """market_value 必须用 current_price 计算,current_price=0 时 market_value=0"""
    from app.sim_trader.engine import Position
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    assert pos.market_value == 0.0, f"current_price=0 时 market_value 应为 0, 实际 {pos.market_value}"

    pos.current_price = 10.5
    assert pos.market_value == 100 * 10.5, f"market_value 错: {pos.market_value}"
    print(f"OK market_value = {pos.remaining_shares} * {pos.current_price} = {pos.market_value}")


def test_profit_pct_is_property():
    """profit_pct 必须是 property,不带参数,直接访问"""
    from app.sim_trader.engine import Position
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    pos.current_price = 11.0  # 涨 10%
    try:
        result = pos.profit_pct
        assert abs(result - 10.0) < 0.001, f"profit_pct 错: {result}"
        print(f"OK profit_pct = {result:.2f}% (property 可访问)")
    except TypeError as e:
        raise AssertionError(f"profit_pct 不是 property: {e}")


def test_market_value_not_uses_entry_price():
    """market_value 不能用 entry_price: 即使 current_price=0 也不该等于 shares*entry_price"""
    from app.sim_trader.engine import Position
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    pos.current_price = 0
    assert pos.market_value != pos.shares * pos.entry_price, \
        f"market_value 仍用 entry_price 计算: {pos.market_value} == {pos.shares * pos.entry_price}"
    print(f"OK market_value 不再用 entry_price: {pos.market_value}")


def test_profit_pct_zero_when_no_current_price():
    """边界: current_price=0 时 profit_pct=0(防除零)"""
    from app.sim_trader.engine import Position
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    assert pos.profit_pct == 0.0, f"current_price=0 时 profit_pct 应为 0, 实际 {pos.profit_pct}"
    print(f"OK profit_pct 边界: current_price=0 -> {pos.profit_pct}")


def test_profit_pct_negative_when_loss():
    """边界: 亏损时 profit_pct < 0"""
    from app.sim_trader.engine import Position
    pos = Position(
        code='000001', entry_date=date(2025, 1, 3),
        entry_price=10.0, shares=100, cost=1000.0
    )
    pos.current_price = 9.0
    result = pos.profit_pct
    assert abs(result - (-10.0)) < 0.001, f"亏损 profit_pct 错: {result}"
    print(f"OK profit_pct 亏损: current_price=9 -> {result:.2f}%")


if __name__ == '__main__':
    test_market_value_uses_current_price()
    test_profit_pct_is_property()
    test_market_value_not_uses_entry_price()
    test_profit_pct_zero_when_no_current_price()
    test_profit_pct_negative_when_loss()
    print("\n#8 修复验证全部通过")
