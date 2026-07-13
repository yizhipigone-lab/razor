"""复现：用 backtest_config params 构造 ctx，验证 rule_hard_stop 实际 fill 模式"""
import json, sys
sys.path.insert(0, '.')
from app.backtest.exit_rules import exit_rule_engine, RuleContext

params = json.load(open('output/backtest_config.json', encoding='utf-8'))
print('params.realistic_stop_fill =', repr(params.get('realistic_stop_fill', '<未设置,走default stop>')))
print('params keys:', list(params.keys()))
print()

# 模拟 688106 01-24: entry=30.45, low=28.75(<=stop=29.049), open=29.75(TDX无OHLC则=close), close=29.75, high=29.75
entry = 30.45
stop = entry * (1 + params['hard_stop'])
print(f'entry={entry} hard_stop={params["hard_stop"]} stop_price={stop:.4f}')
print()

# 构造一个假 pos
class P:
    def __init__(s):
        s.entry_price = entry
        s.peak_price = entry
        s.shares = 2000
        s.tp_triggered = set()
pos = P()

bar = {"close": 29.75, "high": 29.75, "low": 28.75, "open": 29.75}
ctx = exit_rule_engine.build_context(pos, bar, 2, params, use_high_for_tp=True)
print(f'ctx.realistic_stop_fill = {repr(ctx.realistic_stop_fill)}')
print(f'ctx.low={ctx.low} ctx.open={ctx.open} ctx.close={ctx.close}')
print()

# 调 check_all (trailing_first)
sigs = exit_rule_engine.check_all(ctx)
print(f'check_all 返回 {len(sigs)} 个 signal:')
for s in sigs:
    print(f'  reason={s.reason} sell_price={s.sell_price:.4f} ratio={s.sell_ratio} ret={(s.sell_price/entry-1)*100:.2f}%')
print()
print(f'若 fill=stop_price 则 ret 应=-4.60%; 若 fill=max(stop,open)=29.75 则 ret=-2.30%')
print(f'实际我方记录 exit=29.75 ret=-2.3% → 对应 max 模式')
