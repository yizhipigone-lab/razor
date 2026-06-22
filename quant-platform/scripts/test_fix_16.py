"""验证 #16 修复: 顶层无"风险"键, "risk" 键仍存在, 关键参数未被误改"""
import json
import sys
sys.stdout.reconfigure(encoding='utf-8')

def test_no_chinese_risk_key():
    """顶层不应有"风险"键"""
    with open('config/app_setting.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert '风险' not in config, "顶层仍存在 '风险' 键"
    print("✅ 顶层无 '风险' 键")

def test_english_risk_key_still_exists():
    """'risk' 键应仍存在且包含关键参数"""
    with open('config/app_setting.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert 'risk' in config, "'risk' 键消失"
    assert 'hard_stop_loss_pct' in config['risk'], "risk 缺 hard_stop_loss_pct"
    print(f"✅ 'risk' 键仍存在,hard_stop_loss_pct = {config['risk']['hard_stop_loss_pct']}")

def test_risk_params_not_changed():
    """关键 risk 参数未被本修复误改(#16 不应改变实盘参数)"""
    with open('config/app_setting.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    r = config['risk']
    assert r['hard_stop_loss_pct'] == -6.0, f"hard_stop_loss_pct 误改: {r['hard_stop_loss_pct']}"
    assert r['trailing_stop_activate_pct'] == 5.0, f"trailing_stop_activate_pct 误改: {r['trailing_stop_activate_pct']}"
    assert r['trailing_stop_drawdown_pct'] == 2.0, f"trailing_stop_drawdown_pct 误改: {r['trailing_stop_drawdown_pct']}"
    assert r['time_exit_min_profit_pct'] == 3.0, f"time_exit_min_profit_pct 误改: {r['time_exit_min_profit_pct']}"
    # take_profit_tiers 第一档
    assert r['take_profit_tiers'][0]['profit_pct'] == 0.03, f"TP1 profit_pct 误改: {r['take_profit_tiers'][0]['profit_pct']}"
    assert r['take_profit_tiers'][0]['sell_ratio'] == 0.3, f"TP1 sell_ratio 误改: {r['take_profit_tiers'][0]['sell_ratio']}"
    print("✅ risk 段所有关键参数未误改")

def test_json_valid():
    """JSON 仍合法"""
    try:
        with open('config/app_setting.json', 'r', encoding='utf-8') as f:
            json.load(f)
        print("✅ JSON 格式合法")
    except json.JSONDecodeError as e:
        raise AssertionError(f"JSON 解析失败: {e}")

if __name__ == '__main__':
    test_no_chinese_risk_key()
    test_english_risk_key_still_exists()
    test_risk_params_not_changed()
    test_json_valid()
    print("\n🎉 #16 修复验证通过")
