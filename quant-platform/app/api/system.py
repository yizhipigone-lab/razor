from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime, date
from pathlib import Path
from core.logger import get_logger
from database.duckdb_manager import db
from core.settings import settings
from server.websocket.manager import manager, sync_broadcast
from app.utils.threading import run_in_thread
from core.gateway import get_gateway
import pandas as pd

log = get_logger('API-System')
router = APIRouter()

@router.get("/health")
async def health_check():
    """提供给 Docker HEALTHCHECK 的端点"""
    return {"status": "ok", "time": datetime.now().isoformat()}

@router.get("/api/settings")
async def get_settings():
    return settings.get_all()

class SettingsUpdate(BaseModel):
    data: dict

@router.post("/api/settings")
async def update_settings(body: SettingsUpdate):
    updated_keys = []
    errors = []
    
    try:
        for section, values in body.data.items():
            if isinstance(values, dict):
                for k, v in values.items():
                    try:
                        settings.set(section, k, v)
                        updated_keys.append(f"{section}.{k}")
                    except Exception as e:
                        errors.append(f"{section}.{k}: {str(e)}")
            else:
                try:
                    settings.set(section, values)
                    updated_keys.append(section)
                except Exception as e:
                    errors.append(f"{section}: {str(e)}")
        
        # 如果更新了 cron 或 data 相关设置，通知调度器重载
        if any(k in body.data for k in ("cron", "data")):
            try:
                from app.scheduler.cron_jobs import pipeline_scheduler
                pipeline_scheduler.reload_config()
            except Exception:
                pass

        # 构建反馈消息
        if not errors:
            msg = f"✅ 设置保存成功！更新项: {', '.join(updated_keys[:8])}"
            if len(updated_keys) > 8: msg += " 等..."
            await manager.broadcast({"type": "info", "message": msg})
        else:
            msg = f"⚠️ 设置部分保存失败。成功: {len(updated_keys)} 项; 失败: {len(errors)} 项 ({', '.join(errors[:3])})"
            await manager.broadcast({"type": "error", "message": msg})

        return {"status": "ok", "message": "设置已保存", "updated": updated_keys, "errors": errors}
    except Exception as e:
        log.error(f"保存设置时发生全局错误: {e}")
        await manager.broadcast({"type": "error", "message": f"❌ 设置保存失败: {str(e)}"})
        return {"status": "error", "message": str(e)}


ENV_FILE = Path(__file__).parent.parent.parent / ".env"


@router.get("/api/settings/env-keys")
async def get_env_keys():
    """读取 .env 中的 API Key（不暴露完整密钥）"""
    keys = {"tushare_key": "", "deepseek_key": "", "masked": {}}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text("utf-8").strip().split("\n"):
            if "=" in line and not line.startswith("#"):
                k, v = line.strip().split("=", 1)
                if k == "TUSHARE_KEY":
                    keys["tushare_key"] = v
                    keys["masked"]["tushare_key"] = v[:6] + "****" + v[-4:] if len(v) > 12 else v
                elif k == "DEEPSEEK_API_KEY":
                    keys["deepseek_key"] = v
                    keys["masked"]["deepseek_key"] = v[:6] + "****" + v[-4:] if len(v) > 12 else v
    return keys


class EnvKeysUpdate(BaseModel):
    tushare_key: str = ""
    deepseek_key: str = ""


@router.post("/api/settings/env-keys")
async def update_env_keys(body: EnvKeysUpdate):
    """写入新的 API Key 到 .env 文件并更新当前进程环境变量"""
    import os
    try:
        lines = []
        found_tushare = False
        found_deepseek = False
        if ENV_FILE.exists():
            lines = ENV_FILE.read_text("utf-8").strip().split("\n")

        new_lines = []
        for line in lines:
            if line.startswith("TUSHARE_KEY="):
                if body.tushare_key:
                    new_lines.append(f"TUSHARE_KEY={body.tushare_key}")
                    os.environ["TUSHARE_KEY"] = body.tushare_key
                found_tushare = True
            elif line.startswith("DEEPSEEK_API_KEY="):
                if body.deepseek_key:
                    new_lines.append(f"DEEPSEEK_API_KEY={body.deepseek_key}")
                    os.environ["DEEPSEEK_API_KEY"] = body.deepseek_key
                found_deepseek = True
            else:
                new_lines.append(line)

        if body.tushare_key and not found_tushare:
            new_lines.append(f"TUSHARE_KEY={body.tushare_key}")
            os.environ["TUSHARE_KEY"] = body.tushare_key
        if body.deepseek_key and not found_deepseek:
            new_lines.append(f"DEEPSEEK_API_KEY={body.deepseek_key}")
            os.environ["DEEPSEEK_API_KEY"] = body.deepseek_key

        ENV_FILE.write_text("\n".join(new_lines) + "\n", "utf-8")
        log.info("API Key 已更新到 .env 文件")
        return {"status": "ok", "message": "密钥已保存，部分功能需要重启后生效"}
    except Exception as e:
        log.error(f"保存 API Key 失败: {e}")
        return {"status": "error", "message": str(e)}


@router.post("/api/monitor/start")
async def start_monitor():
    from app.monitor.engine import monitor_engine
    gw = get_gateway()
    def _order_cb(code, volume, reason, trade_type):
        gw.sell(code, 0, volume, reason=reason)
    monitor_engine._order_callback = _order_cb
    monitor_engine.start()
    return {"status": "ok", "message": "盘中监控已启动"}

@router.post("/api/monitor/stop")
async def stop_monitor():
    from app.monitor.engine import monitor_engine
    monitor_engine.stop()
    return {"status": "ok", "message": "盘中监控已停止"}

@router.post("/api/monitor/run_once")
async def run_monitor_once():
    """手工触发一次风控检查（盘中调试用）"""
    def _run():
        from app.monitor.engine import monitor_engine
        monitor_engine.run_risk_check()
        sync_broadcast({"type": "log", "msg": "风控检查完成"})
    run_in_thread(_run)
    return {"status": "started"}

# ─── K 线数据（同步/图表统一接口） ────────────────────────────
@router.get("/api/bars/{code}")
async def get_bars(code: str, freq: str = "daily", limit: int = 400):
    """获取 K 线，增加极致鲁棒性，绝对避免 500 Internal Error 导致前端解析 JSON 失败"""
    try:
        # 加载 K 线
        df = db.load_bars(code, freq=freq)
        if df is None or df.empty:
            return []
            
        date_col = "date" if "date" in df.columns else "datetime"
        # 确保日期列为 datetime 类型并排序，截取最后 limit 条数据
        if date_col in df.columns:
            df[date_col] = pd.to_datetime(df[date_col])
            df = df.sort_values(date_col).tail(limit)
        
        # 鲁棒型数据清洗：深度处理 JSON 不支持的 NaN 和 Infinity
        import numpy as np
        for col in df.select_dtypes(include=[np.number]).columns:
            # 统一将无穷大和空值转为 0，这在图表渲染中比 null 更稳定
            df[col] = df[col].replace([np.inf, -np.inf], np.nan).fillna(0.0)
            
        records = df.to_dict(orient="records")
            
        # --- 注入实时行情缝合最后一根 K 线 ---
        try:
            if settings.get("gateway", "active_gateway") == "qmt":
                from app.trader.gateways.qmt import qmt_gateway
                quotes = qmt_gateway.get_realtime_quotes([code])
                if quotes and code in quotes:
                    quote = quotes[code]
                    last_price = quote.get('lastPrice', 0)
                    pre_close = quote.get('lastClose') or quote.get('preClose', 0)
                    if last_price > 0:
                        # 用(最新价 - 昨收)/昨收 精确计算涨跌幅
                        pct_chg = round((last_price - pre_close) / pre_close * 100, 2) if pre_close > 0 else 0
                        today_str = datetime.now().strftime('%Y-%m-%d')
                        today_vol = quote.get('volume', 0)
                        # 获取昨日成交量（最后一条历史 bar 的量）
                        yest_vol = records[-1].get('volume', 0) if records else 0
                        vol_active = yest_vol > 0 and today_vol > yest_vol

                        if not records or records[-1].get(date_col) != today_str:
                            live_bar = {
                                date_col: today_str,
                                "open": quote.get('open', last_price),
                                "high": quote.get('high', last_price),
                                "low": quote.get('low', last_price),
                                "close": last_price,
                                "pre_close": pre_close,
                                "volume": today_vol,
                                "amount": quote.get('amount', 0),
                                "pct_chg": pct_chg,
                                "vol_active": vol_active,
                            }
                            records.append(live_bar)
                        else:
                            records[-1].update({
                                "close": last_price,
                                "pre_close": pre_close,
                                "high": max(records[-1].get("high", 0), quote.get('high', 0)),
                                "low": min(records[-1].get("low", 999999), quote.get('low', 999999)),
                                "volume": today_vol,
                                "pct_chg": pct_chg,
                                "vol_active": vol_active,
                            })
        except Exception as e:
            log.warning(f"图表实时行情缝合失败: {e}")
        # --- END 实时行情缝合 ---

        processed_records = []
        for r in records:
            # 确保所有数值都是 JSON 安全的
            safe_r = {}
            for k, v in r.items():
                if isinstance(v, (datetime, date)):
                    safe_r[k] = v.strftime('%Y-%m-%d')
                elif isinstance(v, float) and (np.isnan(v) or np.isinf(v)):
                    safe_r[k] = 0.0
                elif hasattr(v, "item"): # 处理 numpy 类型
                    try: safe_r[k] = v.item()
                    except: safe_r[k] = float(v)
                else:
                    safe_r[k] = v
            processed_records.append(safe_r)

        return processed_records

    except Exception as e:
        import traceback
        err_msg = f"API | 加载 {code} K线严重崩溃: {e}"
        log.error(err_msg)
        log.error(traceback.format_exc())
        return []

# ─── AI 智能体 API ─────────────────────────────────────────────
