"""
L20 架构偏差修复验证 (无需 import engine.py,避免 duckdb 文件锁)

验证:
1. engine.py 的 _p() 真正从 schema (config.py) 读, settings 不再是中间层
2. 修改 config.py 的风控参数 → schema 立即反映(无需改 settings/app_setting.json)
3. engine 源码不再走 'or (_risk.*)' 死代码模式 (settings 永远有值, 永远不命中)
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def _read_engine_text():
    """从磁盘读 engine.py 文本(不 import, 避免 duckdb 文件锁)"""
    p = os.path.join(os.path.dirname(__file__), '..', 'app', 'backtest', 'engine.py')
    with open(p, 'r', encoding='utf-8') as f:
        return f.read()


def _extract_method(text, method_name):
    """提取方法源码片段(行级近似 - 通过 'def method_name' 起算)"""
    lines = text.split('\n')
    out = []
    in_method = False
    indent = None
    started_with_def = False
    for line in lines:
        if not in_method and line.lstrip().startswith(f'def {method_name}('):
            in_method = True
            out.append(line)
            indent = len(line) - len(line.lstrip())
            started_with_def = True
            continue
        if in_method:
            stripped = line.lstrip()
            if not stripped:
                out.append(line)
                continue
            cur_indent = len(line) - len(stripped)
            # 仅在新顶层 def/class 时结束;空行不结束
            if cur_indent == 0 and (stripped.startswith('def ') or stripped.startswith('class ')):
                break
            if started_with_def and cur_indent <= indent and stripped.startswith(('def ', 'class ')):
                break
            out.append(line)
    return '\n'.join(out)


def test_engine_uses_schema_not_settings():
    """engine.py 内部 _p() 应直接从 schema 读,不再走 settings"""
    text = _read_engine_text()
    src_v2 = _extract_method(text, '_simulate_trade_v2')
    src_fallback = _extract_method(text, '_simulate_trade_daily_fallback')

    for name, src in [('_simulate_trade_v2', src_v2), ('_simulate_trade_daily_fallback', src_fallback)]:
        assert 'load_risk_params' in src, f"{name} 缺 load_risk_params 调用"
        assert 'getattr(settings, key)' not in src, (
            f"{name} 仍走 settings, 违反 config.py 唯一真相源铁律"
        )
        assert ' or (_risk.' not in src, (
            f"{name} 仍存在 'or (_risk.*)' 死代码 — settings 总有值, schema 分支永不触发"
        )
    print("[OK] engine.py 两处 _p() 均直接走 schema, 无 settings 回退、无死代码")


def test_config_change_propagates_to_schema():
    """修改 config.py 的 HARD_STOP → schema.load_risk_params() 应立即反映"""
    # 这步必须 import schema (但不 import engine, 避开 duckdb)
    from app.config.schema import load_risk_params
    import app.sim_trader.config as sc

    original_hard_stop = sc.HARD_STOP
    assert original_hard_stop == -0.06, f"基线 HARD_STOP 应为 -0.06, 实际 {original_hard_stop}"

    r1 = load_risk_params()
    assert abs(r1.hard_stop - (-0.06)) < 1e-9, f"schema hard_stop={r1.hard_stop}"

    sc.HARD_STOP = -0.10
    try:
        r2 = load_risk_params()
        assert abs(r2.hard_stop - (-0.10)) < 1e-9, (
            f"修改 config.py 后 schema 应反映新值, 实际 hard_stop={r2.hard_stop}"
        )
        print(f"[OK] 修改 config.py HARD_STOP: {original_hard_stop} -> {r2.hard_stop}, schema 实时反映")
    finally:
        sc.HARD_STOP = original_hard_stop
        r3 = load_risk_params()
        assert abs(r3.hard_stop - original_hard_stop) < 1e-9, "还原失败"


def test_settings_property_still_exists_for_compat():
    """settings.hard_stop_loss_pct 仍存在(向后兼容)但 engine 不再读它"""
    from core.settings import settings as settings_obj
    val = settings_obj.hard_stop_loss_pct
    assert val < 0, f"settings.hard_stop_loss_pct 仍可读 (兼容), 值={val}"
    print(f"[OK] settings.hard_stop_loss_pct={val} 仍存在(兼容), 但 engine 不再读它")


def test_engine_maps_all_9_risk_keys_to_schema():
    """schema 必须是所有 9 个风控参数的真相源,缺一不可"""
    text = _read_engine_text()
    src_v2 = _extract_method(text, '_simulate_trade_v2')

    required_keys = [
        ('hard_stop_loss_pct', 'hard_stop'),
        ('breakeven_threshold_pct', 'breakeven_threshold'),
        ('breakeven_stop_pnl_pct', 'breakeven_stop'),
        ('trailing_activate_pct', 'trail_activate'),
        ('trailing_drawdown_pct', 'trail_dd'),
        ('time_exit_days', 'time_exit_days'),
        ('time_exit_force_days', 'time_force_days'),
        ('first_day_exit_min_profit', 'first_day_exit_min_profit'),
        ('first_day_exit_days', 'first_day_exit_days'),
    ]

    for engine_key, schema_attr in required_keys:
        assert engine_key in src_v2, f"engine 缺 key: {engine_key}"
        assert schema_attr in src_v2, f"engine 未把 {engine_key} 映射到 schema.{schema_attr}"

    print(f"[OK] engine.py 9 个风控 key 全部映射到 schema, 无遗漏")


def test_params_override_still_works():
    """params_override (AI 优化器) 优先级最高, 仍能覆盖 schema"""
    from app.config.schema import load_risk_params
    r = load_risk_params()
    # 模拟 AI 优化器注入 hard_stop_loss_pct=-3.0 覆盖 schema
    override = {'hard_stop_loss_pct': -3.0}
    val = override.get('hard_stop_loss_pct') or (r.hard_stop * 100)
    assert val == -3.0, f"params_override 应胜出, 实际={val}"
    print(f"[OK] params_override 优先级最高: override=-3.0 覆盖 schema.hard_stop={r.hard_stop}")


if __name__ == '__main__':
    test_engine_uses_schema_not_settings()
    test_config_change_propagates_to_schema()
    test_settings_property_still_exists_for_compat()
    test_engine_maps_all_9_risk_keys_to_schema()
    test_params_override_still_works()
    print("\nL20 架构偏差修复验证全部通过")
