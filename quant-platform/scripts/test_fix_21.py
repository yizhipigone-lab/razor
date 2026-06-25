"""验证 L21 修复: settings.py / backtest.py 假默认值清除"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_settings_no_property_defaults():
    """core/settings.py 的 property 不应有硬编码 default"""
    with open('core/settings.py', 'r', encoding='utf-8') as f:
        content = f.read()
    bad_defaults = [
        "-5.0",   # 原 hard_stop_loss_pct 默认
        " 2.0)",  # breakeven_threshold_pct / trailing_drawdown_pct 默认
        " 6)",    # time_exit_days 默认
        "-3.0",   # time_exit_min_profit_pct 默认(符号反)
        " 10)",   # time_exit_force_days 默认
        " 5.0)",  # trailing_activate_pct 默认
        " 0.5)",  # breakeven_stop_pnl_pct 默认
    ]
    import re
    # 提取所有 @property 块(精确范围)
    prop_blocks = re.findall(
        r'@property\s*\n\s*def\s+(\w+)\(self\)[^:]*:\s*\n((?:[ \t]+[^\n]*\n)+)',
        content
    )
    for prop_name, body in prop_blocks:
        for bad in bad_defaults:
            # 跳过非风控 property (first_day_exit_min_profit default=0.0 不在坏列表)
            if bad in body:
                raise AssertionError(
                    f"settings.{prop_name} 仍含假默认值 '{bad.strip()}':\n{body[:200]}"
                )
    print("✅ settings.py 假默认值已清除 (8 个风控 property)")


def test_api_backtest_no_minus_6_default():
    """app/api/backtest.py 不应有 -6.0 假默认"""
    with open('app/api/backtest.py', 'r', encoding='utf-8') as f:
        content = f.read()
    # 实际行是: "hard_stop_loss_pct": params.get("hard_stop_loss_pct", -6.0)
    assert '"hard_stop_loss_pct", -6.0' not in content, "app/api/backtest.py 仍硬编码 -6.0 fake default"
    print("✅ app/api/backtest.py -6.0 假默认已清除")


def test_settings_property_sign_for_time_exit_min_profit():
    """time_exit_min_profit_pct 应来自 schema 的 time_exit_profit (正数,+0.03)"""
    """settings 不应再硬编码 -3.0(原错误 sign inversion)"""
    from core.settings import settings as s
    val = s.time_exit_min_profit_pct
    # schema TIME_EXIT_PROFIT = +0.03, *100 = +3.0(正数表示"只要不低于+3%就清仓")
    # 原 default=-3.0 是 sign 反了: 语义变成"只要不低于-3%" = 几乎永远触发
    assert val > 0, f"time_exit_min_profit_pct 应为正(只高于阈值才清仓), 实际 {val}"
    # JSON 中可能已有用户配置的值,只要不为负数即可(schema 兜底为正)
    print(f"✅ time_exit_min_profit_pct={val} (正数,无 sign 反)")


def test_settings_falls_back_to_schema_when_json_missing():
    """删掉 JSON 中的 risk.X 后, settings.X 应回退到 schema(config.py 唯一真相源)"""
    from core.settings import Settings, settings as s
    from app.config.schema import load_risk_params
    r = load_risk_params()

    # 备份原始 _data
    original_data = dict(s._data)
    try:
        # 模拟删掉所有 8 个风控键(JSON 中无该键 → 触发 schema fallback)
        risk = s._data.get("risk", {})
        # 记录原值以便恢复
        saved = {k: risk.get(k) for k in [
            "hard_stop_loss_pct", "trailing_stop_activate_pct",
            "trailing_stop_drawdown_pct", "breakeven_threshold_pct",
            "breakeven_stop_pnl_pct", "time_exit_days",
            "time_exit_min_profit_pct", "time_exit_force_days",
        ]}
        # 删除 keys (使用 pop with default 防止 KeyError)
        for k in saved:
            risk.pop(k, None)

        # 此时所有 property 应回退到 schema
        checks = [
            (s.hard_stop_loss_pct, r.hard_stop * 100, "hard_stop_loss_pct"),
            (s.trailing_activate_pct, r.trail_activate * 100, "trailing_activate_pct"),
            (s.trailing_drawdown_pct, r.trail_dd * 100, "trailing_drawdown_pct"),
            (s.breakeven_threshold_pct, r.breakeven_threshold * 100, "breakeven_threshold_pct"),
            (s.breakeven_stop_pnl_pct, r.breakeven_stop * 100, "breakeven_stop_pnl_pct"),
            (s.time_exit_days, r.time_exit_days, "time_exit_days"),
            (s.time_exit_min_profit_pct, r.time_exit_profit * 100, "time_exit_min_profit_pct"),
            (s.time_exit_force_days, r.time_force_days, "time_exit_force_days"),
        ]
        for actual, expected, name in checks:
            assert abs(actual - expected) < 1e-6, (
                f"JSON 缺键时 settings.{name}={actual} 应回退到 schema {expected}"
            )
        print(f"✅ JSON 缺键时, settings 8 个风控 property 全部正确回退到 schema")
    finally:
        # 恢复原始 _data(避免污染后续测试)
        s._data = original_data


if __name__ == '__main__':
    test_settings_no_property_defaults()
    test_api_backtest_no_minus_6_default()
    test_settings_property_sign_for_time_exit_min_profit()
    test_settings_falls_back_to_schema_when_json_missing()
    print("\n🎉 L21 修复验证通过")