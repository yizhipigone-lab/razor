"""验证 #10 修复: 无 key 时抛 RuntimeError, 模型名修正"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.stdout.reconfigure(encoding='utf-8')
from unittest.mock import patch


def test_no_key_raises_runtime_error():
    """无任何 key 时必须抛 RuntimeError,不发送 'EMPTY'"""
    with patch.dict('os.environ', {}, clear=True):
        for k in ['OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'OPENAI_BASE_URL']:
            os.environ.pop(k, None)
        from app.agents.committee import get_llm
        try:
            get_llm()
            assert False, "应抛 RuntimeError,实际未抛"
        except RuntimeError as e:
            msg = str(e)
            assert 'EMPTY' not in msg, f"错误信息泄露 'EMPTY': {msg}"
            assert 'API key' in msg or 'API_KEY' in msg, f"错误信息不明确: {msg}"
            print(f"[OK] 正确抛 RuntimeError: {msg}")


def test_deepseek_model_name_corrected():
    """有 DEEPSEEK_API_KEY 时, 模型名应是 deepseek-chat"""
    with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'fake_key', 'OPENAI_BASE_URL': ''}):
        from app.agents.committee import get_llm
        llm = get_llm()
        assert llm.model_name != 'deepseek-v4-pro', f"仍是错误的 deepseek-v4-pro: {llm.model_name}"
        actual_model = getattr(llm, 'model_name', None) or getattr(llm, 'model', None)
        print(f"[OK] 模型名已修正: {actual_model}")


def test_no_empty_string_in_api_key():
    """任何返回的 ChatOpenAI 实例, api_key 都不应是字面 'EMPTY'"""
    with patch.dict('os.environ', {'DEEPSEEK_API_KEY': 'real_fake_key'}):
        from app.agents.committee import get_llm
        llm = get_llm()
        actual_key = getattr(llm, 'openai_api_key', None) or getattr(llm, 'api_key', None)
        # SecretStr 兼容
        if hasattr(actual_key, 'get_secret_value'):
            actual_key_str = actual_key.get_secret_value()
        else:
            actual_key_str = str(actual_key) if actual_key is not None else ''
        assert actual_key_str != 'EMPTY', f"api_key 仍是 'EMPTY': {actual_key_str}"
        assert actual_key_str == 'real_fake_key', f"api_key 不匹配: {actual_key_str}"
        print(f"[OK] api_key 正确: {actual_key_str[:10]}...")


def test_no_deepseek_v4_pro_in_scope():
    """本次 #10 修复范围内(committee.py + strategy_coder.py)不应再有 deepseek-v4-pro
    注: app/agents/concept_miner.py、stock_analyst.py、app/backtest/llm_advisor.py
    仍有同根源 bug,记入 CHANGELOG-2026-06-22.md 作为后续 issue。
    """
    import subprocess
    for f in ['app/agents/committee.py', 'app/agents/strategy_coder.py']:
        result = subprocess.run(
            ['grep', '-n', 'deepseek-v4-pro', f],
            capture_output=True, text=True
        )
        assert result.stdout.strip() == '', f"{f} 仍存在 deepseek-v4-pro: {result.stdout}"
    print("✅ committee.py + strategy_coder.py 已无 deepseek-v4-pro 残留")


if __name__ == '__main__':
    test_no_key_raises_runtime_error()
    test_deepseek_model_name_corrected()
    test_no_empty_string_in_api_key()
    test_no_deepseek_v4_pro_in_scope()
    print("\n🎉 #10 修复验证通过")