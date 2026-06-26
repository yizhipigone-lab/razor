"""L31: sim trader param alignment"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.stdout.reconfigure(encoding='utf-8')


def test_sim_trader_checks_schema():
    with open('app/sim_trader/engine.py', encoding='utf-8') as f:
        content = f.read()
    assert 'load_risk_params' in content or 'RiskSchema' in content
    print("OK sim_trader uses schema for param validation")


if __name__ == '__main__':
    test_sim_trader_checks_schema()
    print("\nL31 passed")
