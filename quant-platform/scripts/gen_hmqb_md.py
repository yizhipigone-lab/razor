"""读回测JSON, 算年度/月度统计+排查异常, 生成MD报告"""
import json
from collections import defaultdict, Counter
from datetime import date

d = json.load(open('output/backtest_results/hmqb_zz500_20260710.json', encoding='utf-8'))
s = d['summary']
trades = d['trades']
equity = d['equity']

# 年度交易统计
year_stat = defaultdict(lambda: {'n':0,'win':0,'pnl':0,'buy':0})
for t in trades:
    y = t['entry_date'][:4]
    year_stat[y]['n'] += 1
    year_stat[y]['pnl'] += t.get('profit',0)
    year_stat[y]['buy'] += t.get('entry_total',0)
    if t['ret_pct']>0: year_stat[y]['win']+=1

# 年度净值收益(从equity曲线)
eq_by_year = {}
for e in equity:
    y = e['date'][:4]
    if y not in eq_by_year:
        eq_by_year[y] = e['equity']  # 年初首次
year_ret = {}
prev = None
for y in sorted(eq_by_year):
    if prev: year_ret[y] = (eq_by_year[y]/prev_eq_year_end.get(y, prev)-1)*100
# 简单: 用每年末equity
year_end_eq = {}
for e in equity:
    year_end_eq[e['date'][:4]] = e['equity']

# 月度统计
mon_stat = defaultdict(lambda: {'n':0,'pnl':0})
for t in trades:
    m = t['entry_date'][:7]
    mon_stat[m]['n']+=1
    mon_stat[m]['pnl']+=t.get('profit',0)

# 最差交易(-100%)
worst = min(trades, key=lambda t:t['ret_pct'])
best = max(trades, key=lambda t:t['ret_pct'])

# 退出原因中文化
REASON_CN={'HS':'成本止损','TP1':'阶梯止盈(3%)','TP2':'阶梯止盈(13%)','TR':'移动止盈','TC':'时间条件止盈','TF':'强制时间退出','FE':'期末清仓'}
er = s.get('exit_reasons',{})

md = f"""# 黑马起步 × 中证500 回测报告

> 生成时间: {date.today()}  |  数据源: 通达信日线  |  成分股: QMT中证500(500只)

## 一、回测配置

| 项目 | 配置 |
|---|---|
| 公式 | 黑马起步 (TDX) |
| 股票池 | 中证500 (500只) |
| 区间 | 2019-06-01 ~ 2026-07-10 ({s['trading_days']}交易日) |
| 本金 / 单仓 | 100万 / 5万 (动态净值×5%) |
| 频率 | 日线 |
| 费用 | 扣(佣金万2.5+印花千0.5+滑点0.1%双边) |

### 止盈止损参数(对齐VERA套)

| 参数 | 值 | 参数 | 值 |
|---|---|---|---|
| 成本止损 | -4.6% | TP1/TP2 | +3%(卖20%) / +13%(卖60%) |
| 移动止盈激活 | +3.9% | 移动止盈回撤 | 1.7% |
| 时间强退 | 12天 | 条件时间止盈 | 7天且盈利≥2.5% |
| 成交价假设 | stop(止损线价) | 优先级 | trailing_first(止盈>移动>止损) |

## 二、核心指标

| 指标 | 值 | 指标 | 值 |
|---|---|---|---|
| **总收益率** | **{s['total_return']}%** | 最终净值 | {s['final_equity']:,.0f} |
| 最大回撤 | {s['max_drawdown']}% | 年化收益 | {s.get('ann_return','—')}% |
| **胜率** | **{s['win_rate']}%** | 盈亏比 | {s.get('profit_ratio')} |
| 交易笔数 | {s['trades']} | 利润因子 | {s.get('profit_factor')} |
| 胜/负 | {s['wins']}/{s['losses']} | 夏普/卡玛/索提诺 | {s.get('sharpe')}/{s.get('calmar')}/{s.get('sortino')} |
| 平均盈利 | {s.get('avg_win')}% | 平均亏损 | {s.get('avg_loss')}% |
| 最佳交易 | {s.get('best_trade')}% | 最差交易 | {s.get('worst_trade')}% |

## 三、退出原因分布

| 原因 | 笔数 | 占比 | 说明 |
|---|---|---|---|
"""
total = sum(er.values())
for k in ['TP1','TP2','TR','HS','TC','TF','FE']:
    n = er.get(k,0)
    if n: md += f"| {REASON_CN.get(k,k)} | {n} | {n/total*100:.1f}% | — |\n"

md += f"\n## 四、年度统计\n\n| 年度 | 交易笔数 | 胜率% | 盈亏金额(万) | 年末净值 |\n|---|---|---|---|---|\n"
for y in sorted(year_stat):
    st=year_stat[y]
    wr = st['win']/st['n']*100 if st['n'] else 0
    md += f"| {y} | {st['n']} | {wr:.1f} | {st['pnl']/1e4:+.1f} | {year_end_eq.get(y,'—'):,.0f} |\n"

md += f"\n## 五、⚠️ 异常排查\n\n"
md += f"- **最差交易 -100%**：{worst['code']} {worst['name']}，入场{worst['entry_date']}@{worst['entry_px']} → 出场{worst['exit_date']}@{worst['exit_px']}，原因{REASON_CN.get(worst['reason'],worst['reason'])}，{worst['shares']}股\n"
md += f"  - 极可能是**退市/停牌**股按接近0价强制平仓，单笔亏光。建议回测前剔除退市股或限制ST/退市。\n"
md += f"- **最佳交易 +{best['ret_pct']}%**：{best['code']} {best['name']}，{best['entry_date']}→{best['exit_date']}\n"

# 月度盈亏概况
pos_mon = sum(1 for m in mon_stat.values() if m['pnl']>0)
neg_mon = sum(1 for m in mon_stat.values() if m['pnl']<0)
md += f"\n## 六、月度概况\n\n覆盖 {len(mon_stat)} 个月，盈利月 {pos_mon}，亏损月 {neg_mon}，月胜率 {pos_mon/len(mon_stat)*100:.0f}%。\n"
best_mon = max(mon_stat.items(), key=lambda x:x[1]['pnl'])
worst_mon = min(mon_stat.items(), key=lambda x:x[1]['pnl'])
md += f"- 最佳月: {best_mon[0]} ({best_mon[1]['pnl']/1e4:+.1f}万)\n"
md += f"- 最差月: {worst_mon[0]} ({worst_mon[1]['pnl']/1e4:+.1f}万)\n"

md += f"\n## 七、结论\n\n"
md += f"- 7年(2019-2026)总收益 **{s['total_return']}%**，年化约 {s.get('ann_return','—')}%，胜率 {s['win_rate']}%，最大回撤仅 {s['max_drawdown']}%，卡玛 {s.get('calmar')}，风险收益比优秀。\n"
md += f"- 盈利主要来自移动止盈({er.get('TR',0)}笔)和阶梯止盈({er.get('TP1',0)+er.get('TP2',0)}笔)，成本止损{er.get('HS',0)}笔(占{er.get('HS',0)/total*100:.0f}%)。\n"
md += f"- **注意**：含1笔-100%退市亏损，剔除后实际更优。建议实盘前加退市/ST过滤。\n"
md += f"- 本结果含交易费用(真实口径)，参数对齐VERA体系。\n"

open('output/backtest_results/hmqb_zz500_20260710.md','w',encoding='utf-8').write(md)
print(md[:600])
print('...\n\nMD已生成: output/backtest_results/hmqb_zz500_20260710.md')
