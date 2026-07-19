"""实盘交易模块 - 生命周期管理(启动 + 关闭 + 持仓接管 + 残留清理 + 进程管理)。

阶段 2(2026-07-19) 从 main.py 整组抽离。lifespan 是服务启动入口(装配 15 组件 +
连 QMT + 持仓接管 + 调度器),搬错 = 服务起不来(实盘可用性事故)。

依赖策略(经 code-reviewer + architect 双审计 + 13 处迭代):
  - 顶部 import: os/socket/threading as _threading/asynccontextmanager/date/datetime/FastAPI/get_logger/_state
  - logger 沿用 "live_trader.main"(审计 R6:lifespan 22 + _takeover 2 + _cleanup_dryrun 8
    共 32 处启动/接管/QMT 失败日志,运维/NSSM 按此名检索健康状态)
  - lifespan 内 15 个组件 import + 3 个局部 import(runtime_state L92/datetime as _dt L132/
    order_executor L237)随整段搬,相对路径不变(lifecycle.py 与 main.py 同级,都在 app/live_trader/)
  - 不需 subprocess/sys(_spawned 只 append Popen 对象,Popen 在 system router 创建)

历史:原 main.py:36(_check_port)/42(_acquire_lock)/65(lifespan)/281(_takeover)/
351(_cleanup_dryrun)/704(进程管理)。
"""
import os
import socket
import threading as _threading
from contextlib import asynccontextmanager
from datetime import date, datetime

from fastapi import FastAPI

from core.logger import get_logger

from ._state import state as _state

logger = get_logger("live_trader.main")  # 审计 R6:沿用 main 名,保 32 处启动/接管日志源标签


def _check_port_in_use(port: int) -> bool:
    """检查端口是否在监听(§16.8 启动互斥)"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _acquire_lock(lock_file: str) -> bool:
    """获取文件锁(§16.8)"""
    try:
        import portalocker
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        fd = open(lock_file, "w")
        portalocker.lock(fd, portalocker.LOCK_EX | portalocker.LOCK_NB)
        _state["lock_fd"] = fd
        return True
    except ImportError:
        # portalocker 未装,用简单文件存在检查
        if os.path.exists(lock_file):
            return False
        os.makedirs(os.path.dirname(lock_file), exist_ok=True)
        with open(lock_file, "w") as f:
            f.write(str(os.getpid()))
        _state["lock_file"] = lock_file
        return True
    except Exception:
        return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """生命周期:启动 + 关闭"""
    from .config import load_config
    from .store import LiveTraderStore
    from .notify import Notifier
    from .notifications import NotificationStore
    from .kill_switch import KillSwitch
    from .clearance_lock import ClearanceLock
    from .qmt_wrapper import QmtWrapper
    from .callback_handler import CallbackHandler
    from .connection_manager import ConnectionManager
    from .risk_gate import RiskGate
    from .pnl_engine import PnlEngine
    from .reconciler import Reconciler
    from .exit_monitor import ExitMonitor
    from .audit import AuditLogger
    from .scheduler import LiveScheduler

    # ===== 启动 =====
    logger.info("=" * 60)
    logger.info("实盘交易模块 live_trader 启动")
    logger.info("=" * 60)

    # 加载配置(fail-fast 校验)
    config = load_config()
    _state["config"] = config
    # v2(A6): 运行时状态(mode/开关)从 config 移出,支持运行时切换;启动单源(F6)
    from .runtime_state import load_runtime_state
    runtime_state = load_runtime_state(config)
    _state["runtime_state"] = runtime_state
    logger.info(f"模式: {runtime_state.mode} | 资金: {config.live_capital} | 账号: {config.qmt_account_id}")

    # 启动互斥(§16.8):检查 8081(qmt_proxy 已废弃)是否在跑(安全守卫)
    if _check_port_in_use(8081):
        msg = "检测到端口 8081 在监听(可能是旧的 qmt_proxy_server),请先停止(防 xtquant 资源池冲突)"
        logger.error(msg)
        raise RuntimeError(msg)

    # 文件锁
    if not _acquire_lock(config.lock_file):
        msg = f"无法获取文件锁 {config.lock_file},可能已有 live_trader 在跑"
        logger.error(msg)
        raise RuntimeError(msg)
    logger.info(f"文件锁获取: {config.lock_file}")

    # 初始化组件
    store = LiveTraderStore(config)
    # 通知历史存储(独立 DuckDB, sync 写, Phase 1)
    notif_store = NotificationStore(
        os.path.join(os.path.dirname(config.db_path), "live_notifications.duckdb")
    )
    notifier = Notifier(config.wework_webhook, config.multi_channel_alert,
                        feishu_webhook=config.feishu_webhook, channel=config.notify_channel,
                        notif_store=notif_store)
    kill_switch = KillSwitch(config, store, notifier,
                             enabled_check=lambda: runtime_state.kill_switch_enabled)
    clearance_lock = ClearanceLock(config)
    pnl_engine = PnlEngine(store)
    audit = AuditLogger(store)

    # 检查 kill switch 文件(上次崩溃可能留下)
    if kill_switch.is_active():
        # 带上当初激活的真实信息(从残留文件加载), 否则笼统"检测到残留"让人不知为何被按下
        _ks = kill_switch.status()
        # 激活时间 ISO -> 友好格式, 解析失败回退原值
        _at_raw = _ks.get('activated_at')
        try:
            from datetime import datetime as _dt
            _at = _dt.fromisoformat(_at_raw).strftime('%Y-%m-%d %H:%M:%S') if _at_raw else '未知'
        except Exception:
            _at = _at_raw or '未知'
        _detail = (f"当初原因:{_ks.get('reason') or '未知'} | "
                   f"当初来源:{_ks.get('source') or '未知'} | "
                   f"激活于:{_at}")
        logger.warning(f"⚠ 启动检测到残留 kill switch ({_detail}), 需人工解除后才下单")
        if notifier:
            # scheduler 激活的(非交易日)会在交易日 09:20 自动解除, 提示无需人工; 其他来源仍需人工
            _hint = "交易日 09:20 自动解除, 无需人工" if _ks.get('source') == 'scheduler' else "需人工介入"
            notifier.kill_switch_activated(f"启动检测到残留（{_detail}）", "startup_check", hint=_hint)

    qmt = QmtWrapper(config)
    callback = CallbackHandler(config, store, kill_switch, clearance_lock, pnl_engine, notifier, runtime_state, audit=audit)
    conn = ConnectionManager(config, qmt, callback, kill_switch, store)
    risk_gate = RiskGate(config, store, kill_switch, qmt)
    reconciler = Reconciler(config, store, qmt, kill_switch, notifier, pnl_engine)
    exit_monitor = ExitMonitor(config, store, qmt, risk_gate, clearance_lock,
                                kill_switch, callback, audit, pnl_engine, runtime_state)

    # 注入依赖
    callback.kill_switch = kill_switch
    callback.clearance_lock = clearance_lock
    callback.pnl_engine = pnl_engine
    callback.notify = notifier

    # 连接 QMT
    try:
        conn.connect()
        # 启动恢复(§17.1)
        conn.reconcile_on_startup()
        # 持仓接管(§3.3.1),返回 QMT 实际持仓集合供残留检查复用
        confirmed_qmt_codes = _takeover_positions(store, qmt, config, audit)
        # 清理 dry-run 残留(§18.3 严格保护:live 启动时校验无 dry-run 残留)
        _cleanup_dryrun_residue(store, config, audit, confirmed_qmt_codes=confirmed_qmt_codes)
        # 写入当日首条资产备份(为闸门5a日亏计算提供基准,§16.4)
        # 2026-07-16:加守卫——非交易时段且今日已有快照时跳过,避免夜间反复重启
        # 写入 00:17/01:00 等 stray 点污染净值曲线(盘中聚合已过滤,此处从源头少写)
        if qmt.connected:
            asset_data = qmt.query_asset()
            if asset_data:
                _now_hhmm = datetime.now().strftime("%H:%M")
                _trading_hours = "09:25" <= _now_hhmm <= "15:05"
                _has_today = store.get_open_asset() is not None  # 今日是否已有快照
                if _trading_hours or not _has_today:
                    store.backup_asset(asset_data)
                    logger.info(f"资产备份(闸门5a基准): total_asset={asset_data.get('total_asset', 0):.2f}")
                else:
                    logger.info("启动资产备份跳过(非交易时段且今日已有快照,避免 stray 点)")
        # 2026-07-04 修复:启动时若 QMT 连接+持仓接管成功,自动解除残留 kill_switch
        # 场景:周末 QMT 服务端维护期间,旧版本因 status=3 误激活 kill_switch,
        #       维护结束后新进程启动时,残留 .kill_switch 文件导致系统一直处于停机状态。
        #       现在连接+接管都成功说明交易通道正常,自动清理残留(只清来源=on_account_status 的残留)
        if kill_switch.is_active() and qmt.connected and confirmed_qmt_codes is not None:
            ks_status = kill_switch.status()
            if ks_status.get("source") in ("on_account_status", "startup_check"):
                logger.warning(
                    f"启动健康检查:QMT 已连接+持仓接管成功({len(confirmed_qmt_codes)} 只),"
                    f"自动清理残留 kill_switch(原因为 {ks_status.get('source')}:{ks_status.get('reason', '')})"
                )
                kill_switch.deactivate()
                audit.log("startup_killswitch_auto_clear",
                          reason=f"QMT连接+持仓接管成功,自动解除残留 ({ks_status.get('reason', '')})")
            else:
                logger.warning(
                    f"启动时 kill_switch 已激活(source={ks_status.get('source')}),"
                    f"非自动激活,需人工解除"
                )
        # 冷启动 last_close 补全:QMT 连接后主动刷新一次行情
        # 确保持仓有 last_close(昨收),供今日盈亏计算(过夜持仓=现价-昨收)
        if qmt.connected:
            try:
                _positions = store.get_positions()
                if _positions:
                    _codes = [p.get("code", "") for p in _positions if p.get("code")]
                    if _codes:
                        _quotes = qmt.get_realtime_quotes(_codes)
                        if _quotes:
                            _updated = store.refresh_quotes(_quotes)
                            logger.info(f"冷启动行情刷新: {_updated} 条持仓补 last_close")
                        else:
                            logger.warning("冷启动 refresh 跳过:QMT 未返回行情,等首次行情回调")
                else:
                    logger.info("冷启动 refresh 跳过:无持仓")
            except Exception as e:
                logger.warning(f"冷启动行情刷新失败: {e},等首次行情回调")
        else:
            logger.warning("冷启动 refresh 跳过:QMT 未连接,等首次行情回调")
    except Exception as e:
        logger.error(f"QMT 连接失败: {e}")
        # 主开关禁用时 activate 空操作(返回 False),不发"已激活"通知,防幽灵告警
        if kill_switch.activate(reason=f"QMT连接失败: {e}", source="startup"):
            if notifier:
                notifier.kill_switch_activated(f"QMT连接失败: {e}", "startup")

    _state.update({
        "store": store, "notifier": notifier, "notif_store": notif_store,
        "kill_switch": kill_switch,
        "clearance_lock": clearance_lock, "qmt": qmt, "callback": callback,
        "conn": conn, "risk_gate": risk_gate, "reconciler": reconciler,
        "exit_monitor": exit_monitor, "audit": audit, "pnl_engine": pnl_engine,
    })

    # 候选③:OrderExecutor 深 module(3 路下单委托同一入口)
    from .order_executor import OrderExecutor
    executor = OrderExecutor(
        config=config, runtime_state=runtime_state,
        store=store, qmt=qmt, risk_gate=risk_gate,
        clearance_lock=clearance_lock, kill_switch=kill_switch,
        callback_handler=callback, audit=audit, notifier=notifier,
    )
    _state["executor"] = executor
    # exit_monitor 也需要同实例(exit_monitor 内部 _execute_sell 要委托)
    exit_monitor.order_executor = executor

    # 启动调度器(离场扫描 + 对账 + EOD 归档 + 非交易日检测)
    scheduler = LiveScheduler(
        config, store, qmt, exit_monitor, reconciler, kill_switch, notifier, audit,
        runtime_state=runtime_state,
    )
    scheduler.start()
    _state["scheduler"] = scheduler

    logger.info("live_trader 启动完成")
    yield

    # ===== 关闭 =====
    logger.info("live_trader 关闭中...")
    try:
        scheduler.stop()
        conn.stop()
        qmt.stop()
        callback.stop()
        store.close()
        _notif_store = _state.get("notif_store")
        if _notif_store:
            _notif_store.close()
        _kill_all_subprocesses()
        # 释放文件锁
        if "lock_fd" in _state:
            _state["lock_fd"].close()
        if "lock_file" in _state and os.path.exists(_state["lock_file"]):
            os.remove(_state["lock_file"])
    except Exception as e:
        logger.error(f"关闭异常: {e}")
    logger.info("live_trader 已关闭")


def _takeover_positions(store, qmt, config, audit, code: str = None) -> set:
    """持仓接管(§3.3.1):分类 managed=true/false

    Args:
        code: 可选,只同步单只(2026-07-18 手工下单 M4);None=全量接管(默认,旧行为)

    Returns:
        QMT 实际持仓代码集合(供后续残留检查复用,避免重复查询 xtquant 抖动)
    """
    if not qmt.connected:
        return set()
    qmt_positions = qmt.query_positions()
    if not qmt_positions:
        logger.info("持仓接管:QMT 无持仓")
        return set()

    # 单 code 过滤(格式归一后比对)
    code_filter = None
    if code:
        from app.utils.xtquant_compat import format_code as _fmt
        code_filter = _fmt(code.split(".")[0])

    qmt_codes = set()
    preserved = set(config.preserved_codes)
    for pos in qmt_positions:
        code = pos.get("code", "")
        if not code:
            continue
        from app.utils.xtquant_compat import format_code, strip_code_suffix
        code_fmt = format_code(code) if '.' not in code else code
        if code_filter and code_fmt != code_filter:
            continue
        qmt_codes.add(code_fmt)
        managed = code_fmt not in preserved and strip_code_suffix(code_fmt) not in [strip_code_suffix(p) for p in preserved]

        existing = store.get_position(code_fmt)
        _avg = float(pos.get("avg_cost", 0) or 0)
        _last = float(pos.get("last_price", 0) or 0)
        _vol = float(pos.get("volume", 0) or 0)
        _mv = _last * _vol
        _fp = (_last - _avg) * _vol
        _pr = (_fp / (_avg * _vol) * 100) if (_avg * _vol) > 0 else 0.0
        pos_data = {
            "code": code_fmt,
            "volume": pos.get("volume", 0),
            "can_use_volume": pos.get("can_use_volume", 0),
            "frozen_volume": pos.get("frozen_volume", 0),
            "pending_buy_volume": (existing or {}).get("pending_buy_volume", 0),
            "avg_cost": pos.get("avg_cost", 0),
            "last_price": pos.get("last_price", 0),
            "market_value": _mv,
            "float_profit": _fp,
            "profit_rate": _pr,
            "peak_price": (existing or {}).get("peak_price", 0) or pos.get("last_price", 0),
            "sell_count": (existing or {}).get("sell_count", 0),
            "entry_date": (existing or {}).get("entry_date") or (
                # 2026-07-16: 新持仓 entry_date 缺失时优先从成交记录回填,
                # 否则冷启动后 hold_days 永远=1(exit_monitor 当 None→today→len=0→max(1,0))
                store._get_earliest_buy_date(code_fmt) or date.today()
            ),
            "managed": managed,
            "strategy_name": (existing or {}).get("strategy_name", ""),
        }
        store.upsert_position(pos_data)
        tag = "保留(ETF)" if not managed else "策略"
        logger.info(f"持仓接管: {code_fmt} {tag} vol={pos.get('volume',0)}")
        audit.log("position_takeover", code=code_fmt, reason=f"{tag} vol={pos.get('volume',0)}")
    return qmt_codes


def _cleanup_dryrun_residue(store, config, audit, confirmed_qmt_codes: set = None) -> None:
    """清理 dry-run 残留(§18.3 严格保护)

    live 启动时(或 dry-run 切 live 前),删除本地 live_positions 中
    QMT 实际没有但本地有的 managed=true 持仓(dry-run mock 下单残留)。

    规则:
    - 优先使用 confirmed_qmt_codes(由 _takeover_positions 刚确认的 QMT 持仓集合),
      避免重复查询 xtquant 导致缓存抖动误报(2026-07-02 事故根因)
    - 无 confirmed_qmt_codes 时才独立查询 QMT(QMT 查询做 3 次重试取并集)
    - 本地 managed=true 但 QMT 没有的 → 候选残留
    - 保留持仓(managed=false,ETF)不动,即使 QMT 没有也不删(可能是临时查不到)
    - dry-run 模式:候选残留直接清理(mock 数据,安全)
    - live 模式:**绝不自动删除**(防止 QMT 查询抖动误删真实持仓,2026-07-02 事故),
      只告警 + 写 audit,人工核查后手动清理
    """
    if not store or not store._conn:
        return
    try:
        local_positions = store.get_positions()
        if not local_positions:
            return

        # QMT 实际持仓代码集合
        # 优先复用 _takeover_positions 刚确认的集合(防抖动误报)
        qmt_codes = set(confirmed_qmt_codes) if confirmed_qmt_codes else set()

        # 无 confirmed 集合时才独立查询 QMT(3 次重试取并集)
        if not qmt_codes:
            qmt = _state.get("qmt")
            if qmt and qmt.connected:
                from app.utils.xtquant_compat import format_code
                import time as _time
                for attempt in range(3):
                    qmt_positions = qmt.query_positions()
                    for p in (qmt_positions or []):
                        code = p.get("code", "")
                        if code:
                            qmt_codes.add(format_code(code) if '.' not in code else code)
                    # 已覆盖本地全部 managed 代码即可提前结束
                    local_managed = {p.get("code", "") for p in local_positions if p.get("managed", True)}
                    if local_managed <= qmt_codes:
                        break
                    if attempt < 2:
                        _time.sleep(0.2)

        # 找候选残留:本地 managed=true 但 QMT 三次都没查到
        residue = []
        for p in local_positions:
            code = p.get("code", "")
            managed = p.get("managed", True)
            if managed and code not in qmt_codes:
                residue.append(code)

        if not residue:
            logger.info("dry-run 残留清理:无残留")
            return

        # dry-run 模式:清理残留委托和成交(mock 数据),并删残留持仓
        if config.mode == "dry-run":
            for code in residue:
                vol = next((p.get("volume", 0) for p in local_positions if p.get("code") == code), 0)
                store._conn.execute("DELETE FROM live_positions WHERE code = ?", [code])
                audit.log("dryrun_residue_cleanup", code=code,
                         reason=f"QMT无此持仓,本地残留已删除 vol={vol}")
                logger.warning(f"清理 dry-run 残留持仓: {code}")
            store._conn.execute("DELETE FROM live_orders WHERE mode = 'dry-run'")
            store._conn.execute("DELETE FROM live_deals WHERE mode = 'dry-run'")
            logger.info("dry-run 模式:清理 dry-run 残留委托和成交")
            logger.info(f"dry-run 残留清理完成:删除 {len(residue)} 个残留持仓")
            return

        # live 模式:绝不自动删除(防误删真实持仓),只告警 + audit
        for code in residue:
            vol = next((p.get("volume", 0) for p in local_positions if p.get("code") == code), 0)
            # 是否有 live 成交记录(有则更可能是真实持仓,强力提示人工核查)
            has_live_deal = False
            try:
                row = store._conn.execute(
                    "SELECT 1 FROM live_deals WHERE code = ? AND mode = 'live' LIMIT 1", [code]
                ).fetchone()
                has_live_deal = bool(row)
            except Exception:
                pass
            audit.log("live_residue_suspect", code=code,
                      reason=f"QMT三次未查到但本地 managed 持仓 vol={vol} live_deal={has_live_deal}(未自动删除,待人工核查)")
            logger.error(
                f"live 模式疑似残留(未自动删除): {code} vol={vol} "
                f"live_deal={has_live_deal} —— 需人工核查(QMT 查询可能抖动,或为 dry-run 残留)"
            )

        notifier = _state.get("notifier")
        kill_switch = _state.get("kill_switch")
        reason = (f"live启动发现{len(residue)}个疑似残留持仓(未自动删除): "
                  f"{', '.join(residue)}。QMT三次查询未覆盖,需人工核查")
        # 主开关禁用时 activate 空操作(返回 False),不发"kill switch 已激活"通知,
        # 但残留持仓这一真实状况仍经 logger.error 记录(仅修正文案,不谎报急停)。
        activated = False
        if kill_switch and not kill_switch.is_active():
            activated = kill_switch.activate(reason=reason, source="live_residue_check")
        if activated and notifier:
            try:
                notifier.kill_switch_activated(reason, "live_residue_check")
            except Exception:
                pass
        _ks_note = "已激活 kill switch" if activated else "kill switch 主开关已禁用未激活"
        logger.error(f"live 模式发现 {len(residue)} 个疑似残留持仓(未自动删除,{_ks_note},需人工核查): {residue}")
    except Exception as e:
        logger.error(f"dry-run 残留清理失败: {e}")


# ===== 子进程生命周期管理(从 qmt_proxy_server.py 迁移) =====

_spawned_processes: list = []
_spawned_lock = _threading.Lock()


def _cleanup_zombies():
    """清理已退出的子进程"""
    with _spawned_lock:
        alive = [p for p in _spawned_processes if p.poll() is None]
        _spawned_processes.clear()
        _spawned_processes.extend(alive)


def _kill_all_subprocesses():
    """终止所有子进程(服务关闭时调用)"""
    with _spawned_lock:
        for p in _spawned_processes:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        _spawned_processes.clear()
