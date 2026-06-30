"""
P3-5 安全迁移闸门: 把回放产物迁移为生产基线 state.json
========================================================
前置依赖: P1-1 原子写已实现(本脚本用 JsonSimStore._save 的 tmp+os.replace)。

流程(计划书 P3-5):
  1. 校验回放产物的 6/26 锚点(±5%) — 不过则拒绝迁移
  2. 备份当前运行态 state.json 到 snapshots/(带时间戳)
  3. 原子拷贝 imports/replay_*.json -> output/sim_trader/state.json
  4. 重载引擎确认 P0-4 校验不报警(首条≈100万)
  5. (人工)让 14:52 cron 接管前做连续性校验

用法:
  python scripts/migrate_replay_to_live.py --replay output/sim_trader/imports/replay_20260301_20260701.json
  python scripts/migrate_replay_to_live.py --replay <file> --dry-run   # 只校验不迁移
"""
import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LIVE_STATE_PATH = ROOT / "output" / "sim_trader" / "state.json"
SNAPSHOT_DIR = ROOT / "output" / "sim_trader" / "snapshots"
ANCHOR_20260626 = {"equity": 1_486_078, "cash": 1_350_782, "pos": 3}
ANCHOR_TOLERANCE = 0.05


def parse_args():
    p = argparse.ArgumentParser(description="P3-5 回放产物迁移为生产基线")
    p.add_argument("--replay", required=True, help="回放产物 JSON 路径")
    p.add_argument("--dry-run", action="store_true", help="只校验不迁移")
    p.add_argument("--force", action="store_true", help="锚点不过仍强制迁移(危险)")
    return p.parse_args()


def validate_anchor(data):
    """校验 6/26 锚点(±5%)。返回 (ok, 详情)。"""
    ec = data.get("equity_curve", [])
    anchor = next((e for e in ec if str(e.get("date")) == "2026-06-26"), None)
    if anchor is None:
        return False, "回放曲线无 6/26 记录"
    dev = abs(anchor["equity"] - ANCHOR_20260626["equity"]) / ANCHOR_20260626["equity"]
    detail = (f"回放 6/26 equity={anchor['equity']:,.0f} cash={anchor.get('cash',0):,.0f} "
              f"pos={anchor.get('pos','?')} | 日志基准 equity={ANCHOR_20260626['equity']:,.0f} "
              f"| 偏差={dev:.1%} (容忍±{ANCHOR_TOLERANCE:.0%})")
    return dev <= ANCHOR_TOLERANCE, detail


def main():
    args = parse_args()
    replay_path = Path(args.replay)
    if not replay_path.exists():
        print(f"❌ 回放产物不存在: {replay_path}"); sys.exit(1)

    data = json.loads(replay_path.read_text(encoding="utf-8"))
    ec = data.get("equity_curve", [])
    trades = data.get("trades", [])

    print("=" * 64)
    print("  P3-5 回放产物迁移闸门")
    print(f"  回放产物: {replay_path}")
    print(f"  equity_curve: {len(ec)} 条 | trades: {len(trades)} 笔")
    if ec:
        print(f"  首条: {ec[0].get('date')} equity={ec[0].get('equity'):,.0f}")
        print(f"  末条: {ec[-1].get('date')} equity={ec[-1].get('equity'):,.0f}")
    print("=" * 64)

    # ── 步骤1: 锚点校验 ──
    print("\n[1] 6/26 锚点校验 ...")
    ok, detail = validate_anchor(data)
    print(f"    {detail}")
    if not ok and not args.force:
        print("    ❌ 锚点未通过, 拒绝迁移(用 --force 可强制, 不推荐)")
        print("    建议: 回 P3 排查选股/记账差异, 或确认偏差是否来自'历史回算vs实时盘口'")
        sys.exit(2)
    print("    ✅ 通过" if ok else "    ⚠️ 未通过但 --force 强制继续")

    # 首条≈本金校验(确保不会被 P0-4 拦截)
    if ec and ec[0].get("equity", 0) > 1_000_000 * 1.10:
        print(f"    ⚠️ 警告: 首条 equity={ec[0]['equity']:,.0f} >110万, 迁移后会被P0-4拦截!")
        if not args.force:
            sys.exit(2)

    if args.dry_run:
        print("\n[dry-run] 仅校验, 不迁移。")
        return

    # ── 步骤2: 备份当前运行态 ──
    print("\n[2] 备份当前运行态 state.json ...")
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if LIVE_STATE_PATH.exists():
        backup = SNAPSHOT_DIR / f"state_before_migrate_{ts}.json"
        shutil.copy2(str(LIVE_STATE_PATH), str(backup))
        print(f"    已备份: {backup}")
    else:
        print("    (当前无 state.json, 跳过备份)")

    # ── 步骤3: 原子拷贝(用 JsonSimStore 原子写) ──
    print("\n[3] 原子迁移回放产物 -> 运行态 state.json ...")
    from app.sim_trader.store import JsonSimStore
    live_store = JsonSimStore(path=str(LIVE_STATE_PATH))
    live_store._data = data            # 整盘载入回放产物
    live_store._save()                 # P1-1 原子写(tmp+os.replace)
    print(f"    已写入: {LIVE_STATE_PATH}")

    # ── 步骤4: 重载引擎确认 P0-4 不报警 ──
    print("\n[4] 重载引擎验证 P0-4 校验 ...")
    import app.sim_trader.engine as eng_mod
    eng_mod._BAD_EQUITY_CURVE_DETECTED = False
    from app.sim_trader.engine import SimTraderEngine
    engine = SimTraderEngine(store=JsonSimStore(path=str(LIVE_STATE_PATH)))
    if eng_mod._BAD_EQUITY_CURVE_DETECTED:
        print("    ❌ P0-4 报警! 回放数据被判定可疑, 请检查")
        sys.exit(3)
    print(f"    ✅ P0-4 通过 | 加载 cash={engine.cash:,.0f} "
          f"持仓={engine.position_count} 曲线={len(engine.equity_curve)}条")

    print("\n" + "=" * 64)
    print("  ✅ 迁移完成。运行态 state.json 已是回放基线。")
    print("  下一步(人工 P3-5 步骤5): 让今天 14:52 cron 跑一次,")
    print("  核对新增净值与回放末值连续(突变>5%需排查)。")
    print("=" * 64)


if __name__ == "__main__":
    main()
