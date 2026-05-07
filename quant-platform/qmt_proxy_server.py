import os
import time
import json
import threading
import requests
from fastapi import FastAPI, Request
import uvicorn
# 禁用 loguru 颜色输出以避免编码错误
from loguru import logger as log
log.remove()
log.add(lambda msg: print(msg, end=""), colorize=False)
from pydantic import BaseModel
from typing import Optional, Any
from contextlib import asynccontextmanager

import os
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 配置代理环境变量或直接覆盖
QMT_PATH = os.getenv("QMT_PATH", r"E:\QMT\userdata_mini")
ACCOUNT_ID = os.getenv("QMT_ACCOUNT_ID", "1010303391")
PUSH_TARGET = os.getenv("PUSH_TARGET", "http://localhost:8888")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup logic
    log.info("\n========================================================")
    log.info("P8 Quant Proxy Server Starting...")
    # 动态探测物理路径
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_path = os.path.join(base_dir, "data", "parquet")
    log.info(f"Proxy Host Directory: {base_dir}")
    log.info(f"Target Parquet Root: {data_path}")

    # 驱动层初始化
    log.info("Connecting to QMT Shared Memory Engine...")
    try:
        driver.connect()
        log.success("QMT Link Established Successfully.")
    except Exception as e:
        log.error(f"FAILED TO CONNECT QMT: {e}")
    log.info("========================================================\n")
    yield
    # Shutdown logic
    log.info("Shutting down QMT Proxy — cleaning up subprocesses...")
    _kill_all_subprocesses()

app = FastAPI(title="QMT Windows Proxy Server", lifespan=lifespan)

class QMTDriver:
    def __init__(self):
        self._market_init = False
        self._trader_init = False
        self.xt_trader = None
        self.acc_obj = None
        self._subscribed_codes = set()
        self._dirty_codes = set()
        self._dirty_lock = threading.Lock()
        self._sub_lock = threading.Lock()
        self._connect_lock = threading.Lock()
        self._code_map = {} # fmt_c -> set(orig_c)
        self._publisher_running = False
        self._publisher_thread = None

    def _format_code(self, code: str) -> str:
        if not code: return ""
        c_str = str(code).strip()
        if '.' in c_str: return c_str
        if c_str.startswith('6'): return f'{c_str}.SH'
        elif c_str in ('000001', '000016', '000300'): return f'{c_str}.SH'
        elif c_str.startswith(('0', '3')) or c_str.startswith('399'): return f'{c_str}.SZ'
        elif c_str.startswith('5'): return f'{c_str}.SH'
        return c_str

    def connect(self):
        with self._connect_lock:
            if self._market_init:
                return True
        try:
            import xtquant
            from xtquant import xtdata, xttrader

            # --- 兼容性映射：针对不同券商/版本的手动精准匹配 ---
            local_XtAccount = None

            # 路径 1: 经典路径
            try:
                from xtquant.xttype import XtAccount as Acc1
                local_XtAccount = Acc1
            except ImportError: pass

            # 路径 2: 现代路径 (重点尝试)
            if not local_XtAccount:
                try:
                    from xtquant.xttrader import XtAccount as Acc2
                    local_XtAccount = Acc2
                except ImportError: pass

            # 路径 3: 顶层路径
            if not local_XtAccount:
                local_XtAccount = getattr(xtquant, 'XtAccount', None)

            # 路径 4: 终极手动模拟 (如果实在找不到，我们定义一个兼容类，防止后续代码 crash)
            if not local_XtAccount:
                log.warning("QMT Proxy | 正进行深度兼容性模拟...")
                class MockXtAccount:
                    def __init__(self, account_id, account_type="STOCK"):
                        self.account_id = account_id
                        self.account_type = account_type
                local_XtAccount = MockXtAccount

            self._XtAccount = local_XtAccount

            # 尝试导入 xtconstant
            try:
                from xtquant import xtconstant
            except ImportError:
                xtconstant = getattr(xtquant, 'xtconstant', None)
            
            xtdata.data_dir = QMT_PATH
            self._market_init = True
            log.info(f"行情模块初始化成功: {QMT_PATH}")
            
            session_id = int(time.time())
            self.xt_trader = xttrader.XtQuantTrader(QMT_PATH, session_id)
            self.xt_trader.start()
            
            res = self.xt_trader.connect()
            if res == 0:
                log.info(f"交易引擎连接成功: {ACCOUNT_ID}")
                self._trader_init = True
                # 基于观察到的报错信息：部分版本 subscribe 期望直接传入整数 ID 而非对象
                try:
                    # 尝试以整数形式直接订阅
                    self.xt_trader.subscribe(int(ACCOUNT_ID))
                    log.success("账户极速订阅成功(Int模式)")
                except:
                    try:
                        # 尝试传统对象模式
                        if self._XtAccount:
                            self.acc_obj = self._XtAccount(ACCOUNT_ID, "STOCK")
                            self.xt_trader.subscribe(self.acc_obj)
                            log.success("账户订阅成功(Object模式)")
                    except Exception as e:
                        log.debug(f"订阅账户回执(非致命异常，跳过): {e}")
            else:
                log.error(f"交易引擎连接失败 (QMT 返回码: {res})")
        except Exception as e:
            log.error(f"QMT 驱动初始化关键异常: {e}")
            import traceback
            log.debug(traceback.format_exc())

        if self._market_init and not self._publisher_running:
            self._publisher_running = True
            self._publisher_thread = threading.Thread(target=self._tick_publisher_loop, daemon=True)
            self._publisher_thread.start()

    def _tick_callback(self, data):
        if not data: return
        log.debug(f"Received tick callback data: {list(data.keys())}")
        with self._dirty_lock:
            for code in data.keys():
                self._dirty_codes.add(code)

    def _tick_publisher_loop(self):
        while self._publisher_running:
            try:
                time.sleep(0.5) # 500ms debounce buffer
                codes_to_fetch = set()
                with self._dirty_lock:
                    if self._dirty_codes:
                        codes_to_fetch = self._dirty_codes.copy()
                        self._dirty_codes.clear()
                
                if codes_to_fetch:
                    quotes = self.get_quotes(list(codes_to_fetch))
                    if quotes:
                        # Expand quotes based on original subscriber formats (naked vs dotted)
                        expanded = {}
                        for fmt_c, q_data in quotes.items():
                            expanded[fmt_c] = q_data
                            # If we have original codes that map to this fmt_c, include them too
                            orig_set = self._code_map.get(fmt_c, set())
                            for oc in orig_set:
                                expanded[oc] = q_data
                        
                        try:
                            # Push expanded quotes to the configured target
                            requests.post(f"{PUSH_TARGET}/api/internal/quotes_push",
                                          json={"type": "market_quotes", "data": expanded},
                                          timeout=2)
                        except Exception as e:
                            log.debug(f"Webhook push to {PUSH_TARGET} failed: {e}")
            except Exception as e:
                log.error(f"Tick publisher loop error: {e}")

    def update_subscriptions(self, target_codes: list):
        if not self._market_init:
            self.connect()
        from xtquant import xtdata

        # Maintain mapping from formatted to original codes for broad-spectrum broadcasting
        with self._sub_lock:
            for code in target_codes:
                c_str = str(code).strip()
                if not c_str: continue
                fmt_c = self._format_code(c_str)
                if fmt_c not in self._code_map:
                    self._code_map[fmt_c] = set()
                self._code_map[fmt_c].add(c_str)

                # Actionable new subscription check
                if c_str not in self._subscribed_codes:
                    self._subscribed_codes.add(c_str)
                    try:
                        xtdata.subscribe_quote(fmt_c, period='tick', count=0, callback=self._tick_callback)
                    except Exception as e:
                        log.error(f"Failed to subscribe to {fmt_c}: {e}")

    def get_quotes(self, codes: list):
        if not self._market_init: self.connect()
        try:
            from xtquant import xtdata
            mapping = {str(c).strip(): self._format_code(str(c).strip()) for c in codes if str(c).strip()}
            qmt_codes = list(set(mapping.values()))
            if not qmt_codes: return {}
            ticks = xtdata.get_full_tick(qmt_codes)
            return {orig_c: ticks[fmt_c] for orig_c, fmt_c in mapping.items() if fmt_c in ticks}
        except Exception as e:
            log.error(f"行情查询失败: {e}")
            return {}

    def get_balance(self):
        if not self._trader_init: return {}
        asset = self.xt_trader.query_stock_asset(self.acc_obj)
        if asset:
            return {
                "cash": asset.cash,
                "frozen_cash": asset.frozen_cash,
                "market_value": asset.market_value,
                "total_asset": asset.total_asset
            }
        return {}

    def get_position(self):
        if not self._trader_init: return []
        positions = self.xt_trader.query_stock_positions(self.acc_obj)
        res = []
        if positions:
            for p in positions:
                res.append({
                    "code": p.stock_code,
                    "volume": p.volume,
                    "can_sell": p.can_sell_volume,
                    "avg_price": p.open_price,
                    "market_value": p.market_value
                })
        return res

    def send_order(self, code: str, price: float, volume: int, direction: int) -> int:
        if not self._trader_init: return -1
        try:
            import xtquant
            xtconstant = getattr(xtquant, 'xtconstant', None)
            if xtconstant is None:
                log.error("xtconstant 未加载，无法下单")
                return -1
            fmt_code = self._format_code(code)
            order_type = xtconstant.STOCK_BUY if direction == 23 else xtconstant.STOCK_SELL
            order_id = self.xt_trader.order_stock(
                self.acc_obj, fmt_code, order_type, int(volume), 5, price, "P8_Proxy", f"PX_{int(time.time())}"
            )
            return order_id
        except Exception as e:
            log.error(f"下单异常: {e}")
            return -1

driver = QMTDriver()

# 子进程跟踪（防止僵尸进程泄漏）
_spawned_processes: list = []
_spawned_lock = threading.Lock()

def _cleanup_zombies():
    """清理已退出的子进程"""
    global _spawned_processes
    with _spawned_lock:
        alive = []
        for p in _spawned_processes:
            if p.poll() is None:
                alive.append(p)
            else:
                pass  # 已退出，丢弃
        _spawned_processes = alive

def _kill_all_subprocesses():
    """终止所有子进程（服务关闭时调用）"""
    with _spawned_lock:
        for p in _spawned_processes:
            try:
                p.kill()
                p.wait(timeout=3)
            except Exception:
                pass
        _spawned_processes.clear()

# 驱动层初始化已由 lifespan 统一托管

@app.get("/api/quotes")
def api_quotes(codes: str = ""):
    c_list = [c for c in codes.split(",") if c.strip()]
    return driver.get_quotes(c_list)

class SubReq(BaseModel):
    codes: list

@app.post("/api/quotes/subscribe")
def api_quotes_subscribe(req: SubReq):
    driver.update_subscriptions(req.codes)
    return {"status": "ok"}

@app.get("/api/balance")
def api_balance():
    return driver.get_balance()

@app.get("/api/position")
def api_position():
    return driver.get_position()

class OrderReq(BaseModel):
    code: str
    price: float
    volume: int
    direction: int # 23 for Buy, 24 for Sell

@app.post("/api/order")
def api_order(req: OrderReq):
    order_id = driver.send_order(req.code, req.price, req.volume, req.direction)
    return {"status": "ok" if order_id > 0 else "error", "order_id": order_id}

@app.get("/api/stocklist")
def api_stocklist(details: bool = False, codes: str = ""):
    """获取 QMT 全市场股票列表（支持增量详情查询）"""
    from xtquant import xtdata
    try:
        markets = ['上证A股', '深证A股']
        try:
            markets.append('北证A股')
        except:
            pass
        all_codes = []
        for m in markets:
            try:
                all_codes.extend(xtdata.get_stock_list_in_sector(m))
            except Exception as e:
                log.warning(f"获取板块 {m} 股票列表失败: {e}")
        all_codes = sorted(set(all_codes))
    except Exception as e:
        log.error(f"获取QMT股票列表失败: {e}")
        return {"status": "error", "message": str(e)}

    if not details:
        return {"status": "ok", "count": len(all_codes), "codes": all_codes}

    # 筛选特定代码（增量详情查询）
    target = []
    if codes:
        target = [c.strip() for c in codes.split(",") if c.strip()]
    else:
        target = all_codes

    stocks = []
    for code in target:
        try:
            d = xtdata.get_instrument_detail(code)
            if d is None:
                d = {}
            open_date = d.get("OpenDate", "")
            if open_date:
                od_str = str(open_date)
                open_date = f"{od_str[:4]}-{od_str[4:6]}-{od_str[6:]}" if len(od_str) == 8 else ""
            stocks.append({
                "code": code,
                "name": d.get("InstrumentName", ""),
                "sector": d.get("ProductName", ""),
                "list_date": open_date,
            })
        except Exception as e:
            log.warning(f"获取 {code} 详情失败: {e}")
            stocks.append({"code": code, "name": "", "sector": "", "list_date": ""})

    return {"status": "ok", "count": len(stocks), "stocks": stocks}

@app.get("/api/index/members")
def api_index_members(index: str = "沪深300"):
    """获取指定指数的成分股列表（通过 QMT 板块接口）"""
    from xtquant import xtdata
    try:
        codes = xtdata.get_stock_list_in_sector(index)
        if not codes:
            return {"status": "ok", "index": index, "count": 0, "codes": [], "stocks": []}

        codes = sorted(set(codes))
        stocks = []
        for c in codes:
            d = xtdata.get_instrument_detail(c)
            stocks.append({
                "code": c,
                "name": d.get("InstrumentName", "") if d else ""
            })

        log.info(f"index_members | {index}: 返回 {len(codes)} 只成分股")
        return {
            "status": "ok",
            "index": index,
            "count": len(codes),
            "codes": codes,
            "stocks": stocks
        }
    except Exception as e:
        log.error(f"获取指数 {index} 成分股失败: {e}")
        return {"status": "error", "index": index, "message": str(e)}

class SyncReq(BaseModel):
    freq: str = "5m"
    days: int = 30
    start_date: Optional[str] = None
    end_date: Optional[str] = None

@app.post("/api/sync/intra")
def api_sync_intra(req: SyncReq):
    import subprocess
    import sys
    try:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmt_sync_job.py")
        cmd = [sys.executable, script_path, "--freq", req.freq, "--days", str(req.days)]
        if req.start_date:
            cmd.extend(["--start", req.start_date])
        if req.end_date:
            cmd.extend(["--end", req.end_date])

        print(f">>>>>>> [TASK] Dispatching isolated worker: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)
        with _spawned_lock:
            _spawned_processes.append(proc)
        return {"status": "dispatched_to_isolated_worker"}
    except Exception as e:
        print(f">>>>>>> [ERROR] Failed to dispatch isolated worker: {e} <<<<<<<")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

class IndexSyncReq(BaseModel):
    start_date: Optional[str] = None
    end_date: Optional[str] = None

@app.post("/api/sync/index_daily")
def api_sync_index_daily(req: IndexSyncReq):
    """通过隔离子进程同步主流指数日线 OHLCV 数据到本地 Parquet"""
    import subprocess
    import sys
    try:
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "qmt_sync_index_job.py")
        cmd = [sys.executable, script_path]
        if req.start_date:
            cmd.extend(["--start", req.start_date])
        if req.end_date:
            cmd.extend(["--end", req.end_date])

        print(f">>>>>>> [TASK] Dispatching index daily sync worker: {' '.join(cmd)}")
        proc = subprocess.Popen(cmd)
        with _spawned_lock:
            _spawned_processes.append(proc)
        return {"status": "dispatched_to_isolated_worker"}
    except Exception as e:
        print(f">>>>>>> [ERROR] Failed to dispatch index daily sync worker: {e} <<<<<<<")
        import traceback
        traceback.print_exc()
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    # 使用标准模式启动，避免 reload=True 在 Windows 下产生僵尸子进程
    uvicorn.run(app, host="0.0.0.0", port=8081)
