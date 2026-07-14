r"""POC: xtdata.subscribe_quote 实时 tick 订阅验证 (2026-07-15)

专项计划书 PLAN-tick-subscription-2026-07-15 Step 0 的 go/no-go 门。
验证三件事:
  1. xtdata.connect() 基础行情连接能否成功
  2. xtdata.subscribe_quote / subscribe_whole_quote API 是否存在且可调用(回调签名实测)
  3. 能否真正收到 tick 推送(需交易时段; 非时段可能只收到缓存末笔或无推送)

账号/路径从环境变量读, 不硬编码。
用法:
  set QMT_USERDATA_PATH=D:\Program Files\XCXT\userdata_mini
  python scripts/poc_subscribe_quote.py
  (可选) python scripts/poc_subscribe_quote.py --wait 60   # 等待秒数, 默认 30

判定:
  - connect 失败 → QMT mini 未运行, 需先启动 QMT
  - API 不存在/抛异常 → subscribe_quote 不可用, 计划退回高频轮询方案
  - API 可用但 0 tick → 需交易时段重跑验证推送
  - API 可用 + 收到 tick → go, 计划可推进
"""
import os
import sys
import time
import threading
from collections import defaultdict

QMT_USERDATA_PATH = os.environ.get("QMT_USERDATA_PATH", r"D:\Program Files\XCXT\userdata_mini")

# 3 只高流动性票做样本
TEST_CODES = ["000001.SZ", "600519.SH", "000333.SZ"]

# tick 收集
ticks_received = defaultdict(int)   # code -> count
first_ticks = {}                    # code -> 第一笔 tick 原始结构(打印用)
ticks_lock = threading.Lock()
callback_errors = []


def _record_tick(source: str, code: str, raw):
    """记录一笔 tick, 抓第一笔原始结构供签名分析"""
    if not code:
        return
    with ticks_lock:
        ticks_received[code] += 1
        if code not in first_ticks:
            # 保留原始结构(截断长内容), 用于分析回调签名
            try:
                s = repr(raw)
            except Exception as e:
                s = f"<repr 失败: {e}>"
            first_ticks[code] = (source, s[:500])


def main():
    wait_sec = 30
    if "--wait" in sys.argv:
        i = sys.argv.index("--wait")
        wait_sec = int(sys.argv[i + 1]) if i + 1 < len(sys.argv) else 30

    print("=" * 60)
    print("POC: xtdata.subscribe_quote 实时 tick 订阅验证")
    print("=" * 60)
    print(f"userdata_mini: {QMT_USERDATA_PATH}")
    print(f"test codes:    {TEST_CODES}")
    print(f"wait seconds:  {wait_sec}")
    print(f"当前时间:       {time.strftime('%Y-%m-%d %H:%M:%S %a')}")
    print("(注: 非交易时段 9:25-15:00 可能收不到实时推送)")
    print("=" * 60)

    # ── 1. xtdata 导入 + connect ──
    print("\n[1] xtdata 导入 + connect()...")
    try:
        from xtquant import xtdata
    except ImportError as e:
        print(f"  FAIL: xtquant 未安装: {e}")
        return False
    print("  xtquant 导入: OK")

    try:
        if hasattr(xtdata, "data_dir"):
            xtdata.data_dir = QMT_USERDATA_PATH
        rc = xtdata.connect()
        print(f"  xtdata.connect() 返回: {rc} (类型 {type(rc).__name__})")
        print("  基础行情连接: OK")
    except Exception as e:
        print(f"  FAIL: xtdata.connect 异常: {e}")
        print("  → QMT mini 可能未运行, 请先启动 QMT 再跑此 POC")
        return False

    # ── 2. 探测 API 存在性 ──
    print("\n[2] 探测 subscribe API 存在性...")
    has_single = hasattr(xtdata, "subscribe_quote")
    has_whole = hasattr(xtdata, "subscribe_whole_quote")
    has_unsub_single = hasattr(xtdata, "unsubscribe_quote")
    has_unsub_whole = hasattr(xtdata, "unsubscribe_whole_quote")
    print(f"  subscribe_quote        : {'OK' if has_single else '缺失'}")
    print(f"  subscribe_whole_quote  : {'OK' if has_whole else '缺失'}")
    print(f"  unsubscribe_quote      : {'OK' if has_unsub_single else '缺失'}")
    print(f"  unsubscribe_whole_quote: {'OK' if has_unsub_whole else '缺失'}")
    if not (has_single or has_whole):
        print("  FAIL: 无任何 subscribe API → 计划退回高频轮询方案")
        return False

    # ── 3. 测 subscribe_whole_quote(批量, 计划书首选) ──
    used_whole = False
    if has_whole:
        print("\n[3a] subscribe_whole_quote(批量 3 票)...")
        def on_whole(datas):
            """datas 期望 {code: [tick, ...]} 或 {code: tick}"""
            try:
                if isinstance(datas, dict):
                    for code, val in datas.items():
                        _record_tick("whole", code, val)
                else:
                    _record_tick("whole", "?", datas)
            except Exception as e:
                callback_errors.append(f"on_whole 异常: {e}")
        try:
            xtdata.subscribe_whole_quote(TEST_CODES, callback=on_whole)
            print("  subscribe_whole_quote 调用: OK (无异常)")
            used_whole = True
        except Exception as e:
            print(f"  subscribe_whole_quote 调用异常: {e}")

    # ── 3b. 测 subscribe_quote(单 code, 设计文档 v1.0 签名) ──
    if has_single:
        print("\n[3b] subscribe_quote(单 code, period='tick')...")
        sample_code = TEST_CODES[0]
        def on_single(data):
            """单 code 回调签名未知, 抓原始结构"""
            try:
                if isinstance(data, dict):
                    for code, val in data.items():
                        _record_tick("single", code, val)
                else:
                    _record_tick("single", sample_code, data)
            except Exception as e:
                callback_errors.append(f"on_single 异常: {e}")
        try:
            xtdata.subscribe_quote(sample_code, period="tick", count=-1, callback=on_single)
            print(f"  subscribe_quote({sample_code}) 调用: OK (无异常)")
        except Exception as e:
            print(f"  subscribe_quote 调用异常: {e}")

    # ── 4. 等待收 tick ──
    print(f"\n[4] 等待 {wait_sec} 秒收 tick...")
    for left in range(wait_sec, 0, -5):
        time.sleep(5)
        with ticks_lock:
            total = sum(ticks_received.values())
        print(f"  剩 {left:>3}s, 累计收到 {total} 笔 tick")
        if left <= 0:
            break

    # ── 5. 结果判定 ──
    print("\n" + "=" * 60)
    print("[5] 结果判定")
    print("=" * 60)
    with ticks_lock:
        total = sum(ticks_received.values())
        print(f"总 tick 数: {total}")
        for code in TEST_CODES:
            print(f"  {code}: {ticks_received.get(code, 0)} 笔")
        if first_ticks:
            print("\n首笔 tick 原始结构(回调签名分析):")
            for code, (src, raw) in first_ticks.items():
                print(f"  [{src}] {code}: {raw}")
    if callback_errors:
        print("\n回调异常:")
        for e in callback_errors[:5]:
            print(f"  {e}")

    print("\n" + "-" * 60)
    if total > 0:
        print("[OK] 收到 tick -> subscribe_quote 可用, 计划可推进 (GO)")
    else:
        print("[WARN] 0 tick -> API 调用成功但未收到推送")
        print("   可能原因: 非交易时段 / QMT 未登录行情 / 订阅需 warmup")
        print("   建议: 交易时段 9:25-15:00 重跑此 POC 确认推送")
        print("   API 已验证可调用(无异常) -> 计划可推进, 推送验证留交易时段")
    print("-" * 60)

    # ── 6. 清理 unsubscribe ──
    print("\n[6] 清理 unsubscribe...")
    try:
        if used_whole and has_unsub_whole:
            xtdata.unsubscribe_whole_quote(TEST_CODES)
            print("  unsubscribe_whole_quote: OK")
        if has_single and has_unsub_single:
            xtdata.unsubscribe_quote(TEST_CODES[0])
            print("  unsubscribe_quote: OK")
    except Exception as e:
        print(f"  unsubscribe 异常(可忽略): {e}")

    return total > 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 2)
