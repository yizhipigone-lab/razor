"""验证 L2 修复: 仓库全 app/ 无 deepseek-v4-pro 残留"""
import subprocess
import sys
sys.stdout.reconfigure(encoding='utf-8')

def test_no_deepseek_v4_pro_in_app():
    """全 app/ 不应再有 deepseek-v4-pro"""
    result = subprocess.run(
        ['grep', '-rn', 'deepseek-v4-pro', 'app/', '--include=*.py'],
        capture_output=True, text=True
    )
    assert result.stdout.strip() == '', f"仍存在 deepseek-v4-pro:\n{result.stdout}"
    print("✅ 全 app/ 无 deepseek-v4-pro 残留")

if __name__ == '__main__':
    test_no_deepseek_v4_pro_in_app()
    print("\n🎉 L2 修复验证通过")
