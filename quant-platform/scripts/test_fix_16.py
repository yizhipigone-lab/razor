"""验证 #16 修复: 顶层无"风险"键, "risk" 键仍存在"""
import json


def test_no_chinese_risk_key():
    """顶层不应有"风险"键"""
    with open('config/app_setting.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert '风险' not in config, "顶层仍存在 '风险' 键"
    print("OK 顶层无 '风险' 键")


def test_english_risk_key_still_exists():
    """'risk' 键应仍存在且包含关键参数"""
    with open('config/app_setting.json', 'r', encoding='utf-8') as f:
        config = json.load(f)
    assert 'risk' in config, "'risk' 键消失"
    assert 'hard_stop_loss_pct' in config['risk'], "risk 缺 hard_stop_loss_pct"
    print(f"OK 'risk' 键仍存在,hard_stop_loss_pct = {config['risk']['hard_stop_loss_pct']}")


def test_json_valid():
    """JSON 仍合法"""
    try:
        with open('config/app_setting.json', 'r', encoding='utf-8') as f:
            json.load(f)
        print("OK JSON 格式合法")
    except json.JSONDecodeError as e:
        raise AssertionError(f"JSON 解析失败: {e}")


if __name__ == '__main__':
    test_no_chinese_risk_key()
    test_english_risk_key_still_exists()
    test_json_valid()
    print("\n#16 修复验证通过")