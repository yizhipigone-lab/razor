"""
模拟盘逐日回放重建脚本 (P3, 方案C)
=====================================
目的: 用真实 SimEngine 把 2026-03-01 ~ 2026-07-01 一天天重新跑一遍,
      重建被 TDX 回测数据污染前的真实净值曲线。

核心设计 (与 populate 灌数的本质区别):
  - TDX 只当"选股参谋": 每个交易日调 execute_screen 取 QUANTQQ 历史信号
  - SimEngine 当"账房先生": 买卖/扣现金/记账/算净值全走真实引擎逐日演进
  - 引擎尊重 100万本金 + 单票上限, 故净值真实, 不会虚高 2.5 倍

选股参数 (由 Claude 定, 经用户授权):
  - 用 execute_screen(end_time=当日, lookback_days=500), 与生产 cron 完全一致
  - 差异来源: 历史回算 vs 当时 QMT 实时盘口(不可消除), 6/26 锚点允许 ±5% 偏差

用法:
  python scripts/replay_sim_trader.py                    # 正式回放
  python scripts/replay_sim_trader.py --dry-run          # 只跑不写文件
  python scripts/replay_sim_trader.py --fast             # 无延迟
  python scripts/replay_sim_trader.py --end-date 2026-06-30

避坑 (P3-3):
  - 不写运行态 state.json, 只写 output/sim_trader/imports/
  - 单 engine 跑全程, _prev_day_snap 累积不重置
  - 交易日历从 parquet 反推, 不调 baostock
  - 选股需通达信客户端在线
"""
import argparse
import sys
import time
import traceback
from datetime import date, datetime
from pathlib import Path

# Windows 控制台 GBK 编码兜底: 强制 stdout/stderr 用 UTF-8, 避免 emoji/中文 print 崩溃
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.logger import get_logger

log = get_logger("Replay")

# 输出目录 (P3-3: 与运行态隔离)
IMPORTS_DIR = ROOT / "output" / "sim_trader" / "imports"
LIVE_STATE_PATH = (ROOT / "output" / "sim_trader" / "state.json").resolve()

# 6/26 真实日志锚点 (P3-4 验收)
ANCHOR_20260626 = {"equity": 1_486_078, "cash": 1_350_782, "pos": 3}


def parse_args():
    p = argparse.ArgumentParser(description="模拟盘逐日回放重建 (方案C)")
    p.add_argument("--start-date", default="2026-03-01")
    p.add_argument("--end-date", default="2026-07-01")
    p.add_argument("--initial-capital", type=float, default=1_000_000)
    p.add_argument("--lookback-days", type=int, default=500,
                   help="选股回溯K线数(与生产cron一致, 默认500)")
    p.add_argument("--max-failures", type=int, default=3,
                   help="连续失败N次中断(默认3)")
    p.add_argument("--dry-run", action="store_true", help="只跑不写文件")
    p.add_argument("--fast", action="store_true", help="无延迟(本脚本本就无sleep, 占位)")
    return p.parse_args()


def derive_trading_dates(bars, start: date, end: date):
    """P3-2: 从 parquet bars 反推交易日历(不依赖 baostock)。"""
    import pandas as pd
    all_dates = sorted({d for d in bars["date"].unique()})
    return [d for d in all_dates if start <= d <= end]


def load_bars_direct(start: date, end: date):
    """直接读 parquet 文件构建全市场 bars(绕开 DuckDB 锁; 主服务独占 meta.db 时仍可跑)。
    只保留引擎所需列 + 日期窗口, 控制内存。
    注意: parquet 的 'code' 列仅少数行有值(多为NaN), 必须用文件名作为可靠代码。"""
    import pandas as pd
    daily_dir = ROOT / "data" / "parquet" / "daily"
    files = sorted(daily_dir.glob("*.parquet"))
    cols = ["date", "open", "high", "low", "close", "volume", "adj_factor"]
    frames = []
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    for f in files:
        code = f.stem  # 文件名即股票代码(可靠), 不用 NaN 居多的 code 列
        try:
            df = pd.read_parquet(str(f), columns=cols)
        except Exception:
            continue
        df["date"] = pd.to_datetime(df["date"])
        df = df[(df["date"] >= start_ts) & (df["date"] <= end_ts)]
        if df.empty:
            continue
        df = df.copy()
        df["code"] = code
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    bars = pd.concat(frames, ignore_index=True)
    for c in ["open", "high", "low", "close", "volume"]:
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["close"])
    bars["date"] = bars["date"].dt.date
    return bars.sort_values(["code", "date"])


def build_snapshots_by_date(bars):
    """预构建 {date: {code: {open,high,low,close}}}, 避免逐日重复过滤大表。
    内联 data_loader.get_daily_snapshot 逻辑, 不导入 data_loader(避开 DuckDB 锁)。"""
    snaps = {}
    for row in bars.itertuples(index=False):
        d = row.date
        snaps.setdefault(d, {})[row.code] = {
            "open": float(row.open), "high": float(row.high),
            "low": float(row.low), "close": float(row.close),
        }
    return snaps


def make_deferred_store(out_path):
    """回放专用 store: 缓冲 _save(), 由脚本显式 flush(), 避免引擎每笔买卖都全量写盘
    (execute_buy/sell 内部各调一次 _save, 一天数十次全量JSON写 -> I/O风暴 + Windows锁竞态)。
    继承 JsonSimStore, 仅拦截 _save; flush() 时才真正落盘一次。"""
    from app.sim_trader.store import JsonSimStore

    class DeferredStore(JsonSimStore):
        def __init__(self, path):
            super().__init__(path=path)
            self._defer = True

        def _save(self):
            if getattr(self, "_defer", False):
                return  # 缓冲: 不落盘
            super()._save()

        def flush(self):
            self._defer = False
            try:
                super()._save()
            finally:
                self._defer = True

    store = DeferredStore(str(out_path))
    store._data = {}
    store.flush()
    return store


def select_signals(bridge, d: date, snapshot: dict, lookback_days: int):
    """调 TDX execute_screen 取当日 QUANTQQ 信号, 返回 [(code, close_price), ...]。
    与 cron_jobs.py:430 选股逻辑一致: matched 代码 + snapshot 收盘价。"""
    sig_result = bridge.execute_screen(
        end_time=d.strftime("%Y%m%d"), lookback_days=lookback_days)
    if sig_result.get("status") != "ok":
        raise RuntimeError(f"TDX选股失败: {sig_result.get('message', sig_result)}")
    matched = sig_result.get("matched", [])
    signals = []
    for code in matched:
        code_num = code.split(".")[0] if "." in code else code
        px = snapshot.get(code_num, {}).get("close", 0)
        if px > 0:
            signals.append((code_num, px))
    return signals, len(matched)


def replay_one_day(engine, d, trading_dates, snapshot, signals, sc):
    """模拟一天的完整流程, 与 cron_jobs.py:430-509 对齐。
    注意: 不调 engine.sell_phase (它有 09:25-15:05 时段护栏, 回放任意时刻跑会被拒),
          改为直接调 check_stops + execute_sell, 等价于 sell_phase 的核心。"""
    import copy
    from app.backtest.execution import calc_buy_cost  # noqa: 触发与引擎一致的成本口径

    sell_count = 0
    buy_count = 0

    # ── 卖出阶段(绕开时段护栏, 复刻 sell_phase 核心) ──
    if sc.AUTO_SELL:
        sells = engine.check_stops(d, snapshot, trading_dates,
                                   prev_snap=engine._prev_day_snap)
        for pos, exit_price, reason, partial in sells:
            trade = engine.execute_sell(pos, exit_price, reason, partial,
                                        exit_date=d, exit_timing="close")
            if trade:
                trade.hold_days = sum(1 for td in trading_dates
                                      if pos.entry_date <= td <= d)
                sell_count += 1
        # 先落盘现金(P1-4 顺序), 再清理持仓
        if engine._store:
            engine._store.save_state(engine.cash, engine.consecutive_losses,
                                     engine.pause_until, engine._trade_count)
        engine.positions = {k: v for k, v in engine.positions.items() if v.is_active}
        # 维护"昨日快照"供次日除权跳空保护(P3-2: 累积不重置), 仅内存, 不落盘
        # (回放是连续内存演进, prev_day_snap 只供次日 check_stops 用; 持久化它是冷启动才需要)
        engine._prev_snap = {k: dict(v) for k, v in snapshot.items()}
        engine._prev_day_snap = copy.deepcopy(engine._prev_snap)
        if engine._store:
            engine._store.save_positions(engine.positions)  # 缓冲, 不立即写盘

    # ── 买入阶段 ──
    if sc.AUTO_BUY and signals:
        paused = engine.pause_until is not None and d <= engine.pause_until
        if not paused:
            max_new = int(engine.cash / engine.max_buy_amount()) + 1
            for code, price in signals[:max_new]:
                # 同股冷却期(与 cron 一致)
                if any(t.code == code and (d - t.entry_date).days <= sc.SAME_STOCK_COOLDOWN
                       for t in engine.trades):
                    continue
                if engine.execute_buy(d, code, price, strategy_name="QUANTQQ-replay"):
                    buy_count += 1

    # ── 记录净值(始终执行, P1-2 单路径落盘, source=import) ──
    eq = engine.total_equity(snapshot)
    if engine._store:
        engine._store.save_equity_point(d, eq, engine.cash, engine.position_count,
                                        source="import")
        engine._store.save_state(engine.cash, engine.consecutive_losses,
                                 engine.pause_until, engine._trade_count)
        # 一天结束: 显式 flush 一次(把当日所有缓冲的买卖/净值落盘)
        engine._store.flush()
        engine.equity_curve = engine._store.load_equity_curve()
    return eq, buy_count, sell_count


def main():
    args = parse_args()
    start = date.fromisoformat(args.start_date)
    end = date.fromisoformat(args.end_date)

    print("=" * 64)
    print("  模拟盘逐日回放重建 (方案C)")
    print(f"  区间:     {start} ~ {end}")
    print(f"  初始资金: {args.initial_capital:,.0f}")
    print(f"  选股:     execute_screen, lookback={args.lookback_days} (同生产cron)")
    print(f"  dry-run:  {args.dry_run}")
    print("=" * 64)

    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_json = IMPORTS_DIR / f"replay_{start:%Y%m%d}_{end:%Y%m%d}.json"
    out_csv = IMPORTS_DIR / "replay_daily_log.csv"
    err_log = IMPORTS_DIR / "replay_errors.log"

    # 安全: 输出路径绝不等于运行态(P3-3)
    if out_json.resolve() == LIVE_STATE_PATH:
        print("❌ 输出路径不能是运行态 state.json"); sys.exit(1)

    # ── 加载数据 + 引擎 ──
    print("\n[1/3] 加载 parquet 全市场日线(直读文件, 绕开DuckDB锁) ...")
    from app.sim_trader.engine import SimTraderEngine
    from app.sim_trader.store import JsonSimStore
    import app.sim_trader.config as sc

    bars = load_bars_direct(start, end)
    if bars is None or bars.empty:
        print("❌ parquet 无数据"); sys.exit(1)
    trading_dates = derive_trading_dates(bars, start, end)
    print(f"  交易日数(parquet反推): {len(trading_dates)}  ({trading_dates[0]} ~ {trading_dates[-1]})")
    snaps_by_date = build_snapshots_by_date(bars)

    print("\n[2/3] 初始化 TDX 桥接 + 单 engine ...")
    from app.tqsdk.bridge import TdxBridge
    bridge = TdxBridge()

    # 独立 store(指向 imports, 缓冲写盘), 不碰运行态
    store = None if args.dry_run else make_deferred_store(out_json)
    engine = SimTraderEngine(store=store)
    engine.cash = args.initial_capital

    # ── 逐日回放 ──
    print("\n[3/3] 逐日回放 ...")
    csv_lines = ["date,equity,cash,positions,buy_count,sell_count,signal_count"]
    err_lines = []
    consecutive_failures = 0
    total_failures = 0
    success_days = 0

    for d in trading_dates:
        try:
            snapshot = snaps_by_date.get(d, {})
            if not snapshot:
                raise RuntimeError("当日 snapshot 为空(parquet无当日数据)")
            signals, sig_count = select_signals(bridge, d, snapshot, args.lookback_days)
            eq, bc, scount = replay_one_day(engine, d, trading_dates, snapshot, signals, sc)
            csv_lines.append(
                f"{d},{eq:.0f},{engine.cash:.0f},{engine.position_count},{bc},{scount},{sig_count}")
            success_days += 1
            consecutive_failures = 0
            log.info(f"[回放] {d} 净值={eq:,.0f} 现金={engine.cash:,.0f} "
                     f"持仓={engine.position_count} 买{bc}卖{scount} 信号{sig_count}")
        except Exception as e:
            total_failures += 1
            consecutive_failures += 1
            tb = "".join(traceback.format_exc().splitlines(keepends=True)[:5])
            err_lines.append(f"{d} | {type(e).__name__}: {e}\n{tb}\n")
            log.error(f"[回放] {d} 失败({consecutive_failures}/{args.max_failures}): {e}")
            if consecutive_failures >= args.max_failures:
                log.error(f"[回放] 连续失败 {consecutive_failures} 次, 中断")
                break

    # ── 输出 ──
    if not args.dry_run:
        out_csv.write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
        if err_lines:
            err_log.write_text("".join(err_lines), encoding="utf-8")
        # store 已逐日 flush 落盘 equity_curve/trades/positions/state, 此处确保 state 终值落盘
        store.save_state(engine.cash, engine.consecutive_losses,
                         engine.pause_until, engine._trade_count)
        store.flush()

    # ── 摘要 + 锚点校验 ──
    print("\n" + "=" * 64)
    print(f"  回放完成: {success_days} 天成功, {total_failures} 天失败")
    if not args.dry_run:
        ec = store.load_equity_curve()
        print(f"  equity_curve 条数: {len(ec)}")
        if ec:
            last = ec[-1]
            print(f"  终值: {last['date']} equity={last['equity']:,.0f} "
                  f"cash={last['cash']:,.0f} pos={last['pos']}")
        # 6/26 锚点对账
        anchor = next((e for e in ec if str(e["date"]) == "2026-06-26"), None)
        if anchor:
            dev = abs(anchor["equity"] - ANCHOR_20260626["equity"]) / ANCHOR_20260626["equity"]
            ok = "✅ 通过(±5%)" if dev <= 0.05 else "⚠️ 偏差>5%, 需排查"
            print(f"  6/26 锚点: 回放equity={anchor['equity']:,.0f} "
                  f"vs 日志{ANCHOR_20260626['equity']:,.0f} 偏差={dev:.1%} {ok}")
        else:
            print("  6/26 锚点: 回放曲线无6/26记录(可能区间或失败)")
        print(f"\n  → 中间产物: {out_json}")
        print(f"  → 每日明细: {out_csv}")
        print(f"  → 运行态 state.json 未被触碰(P3-3)")
        print(f"  → 校验通过后, 走 P3-5 安全迁移闸门")
    print("=" * 64)


if __name__ == "__main__":
    main()
