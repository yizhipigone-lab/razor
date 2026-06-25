"""验证 L20 修复: 真相源 schema 加载"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_schema_loads_from_config():
    """schema 应能从 app/sim_trader/config.py 加载所有风控参数"""
    from app.config.schema import load_risk_params
    params = load_risk_params()
    assert params.hard_stop < 0, f"hard_stop 应 < 0, 实际 {params.hard_stop}"
    assert params.trail_activate > 0, f"trail_activate 应 > 0"
    assert isinstance(params.take_profit_tiers, list)
    assert len(params.take_profit_tiers) > 0
    print(f"✅ schema 加载成功, hard_stop={params.hard_stop}")


def test_engine_no_fake_defaults():
    """engine.py 不应再硬编码 -7.0 / 15.0 / 30 等假默认值"""
    with open('app/backtest/engine.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_patterns = [
        "_p('hard_stop_loss_pct', -7.0)",
        "_p('trailing_activate_pct', 15.0)",
        "_p('trailing_drawdown_pct', 5.0)",
        "_p('time_exit_days', 30)",
        "_p('breakeven_threshold_pct', 5.0)",
        "_p('breakeven_stop_pnl_pct', 0.0)",
    ]
    for p in bad_patterns:
        assert p not in content, f"仍存在假默认值: {p}"
    print("✅ engine.py 假默认值已清除")


if __name__ == '__main__':
    test_schema_loads_from_config()
    test_engine_no_fake_defaults()
    print("\n🎉 L20 修复验证通过")