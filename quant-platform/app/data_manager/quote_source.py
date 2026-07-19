"""行情 sourcing 深 module(候选 ①)。

设计结晶见 CONTEXT.md → QuoteSource。

interface(契约,固定不变):
    get_realtime_quotes(codes) -> DataFrame
    列: code / open / high / low / price / volume / amount / last_close / change_pct / source
    - 每个请求 code 必有一行;拿不到价 → 字段 NaN + source='missing'
    - last_close = lastClose → preClose → NaN(严禁用现价伪造,守 CLAUDE.md:26)
    - source ∈ {qmt, tdx, tencent, parquet, missing}

implementation(藏在 interface 后):
    QuoteSource(adapters) 持有一组 QuoteAdapter,per-code 逐只降级
    (高优先级 adapter 先解,解不到的 code 才往下个 adapter 传)。
    4 个真实 adapter(QmtHttp/Tdx/Tencent/Parquet)已接入默认 source。

Phase 2 起 engine.get_realtime_quote 委托给本 module(见 app/data_manager/engine.py)。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Protocol

import pandas as pd

# ── interface 契约 ──────────────────────────────────────────────
CONTRACT_COLUMNS = [
    "code", "open", "high", "low", "price",
    "volume", "amount", "last_close", "change_pct", "source",
]

# canonical 优先级(QMT→TDX→腾讯→Parquet;grilling Q2 决议:TDX 留作第4源)
SOURCE_PRIORITY = ("qmt", "tdx", "tencent", "parquet")


class QuoteAdapter(Protocol):
    """行情源 adapter 的契约:satisfies the seam。

    fetch 返回 {code: raw_dict},raw_dict 键(price 必填>0,其余可选):
    price / lastClose / preClose / open / high / low / volume / amount。
    adapter 应省略它解析不到的 code(不要塞 price<=0 的脏值)。
    """

    name: str

    def fetch(self, codes: list[str]) -> dict[str, dict]:
        ...


# ── 归一化(implementation 私有,统一在此 → locality)────────────
def _to_float(value, default: Optional[float] = None) -> Optional[float]:
    """安全转 float;None/不可解析 → default。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_last_close(raw: dict) -> Optional[float]:
    """昨收规则(守 CLAUDE.md:26):lastClose → preClose → None。

    绝不用现价/Parquet close 冒充。0/负值视为缺失。
    """
    last_close = _to_float(raw.get("lastClose"))
    if last_close is not None and last_close > 0:
        return last_close
    pre_close = _to_float(raw.get("preClose"))
    if pre_close is not None and pre_close > 0:
        return pre_close
    return None


def _or_price(value, price: float) -> float:
    """OHLC 用原值;None/<=0 → 退到现价(保留现行行为,不破坏调用方)。"""
    f = _to_float(value)
    if f is None or f <= 0:
        return price
    return f


def _make_row(code: str, raw: dict, source: str) -> dict:
    """把 adapter 的 raw dict 归一化成契约行。"""
    price = _to_float(raw.get("price"), 0.0) or 0.0
    last_close = _resolve_last_close(raw)
    change_pct = (
        round((price - last_close) / last_close * 100, 2)
        if last_close and last_close > 0
        else float("nan")
    )
    return {
        "code": code,
        "open": _or_price(raw.get("open"), price),
        "high": _or_price(raw.get("high"), price),
        "low": _or_price(raw.get("low"), price),
        "price": price,
        "volume": _to_float(raw.get("volume"), 0.0) or 0.0,
        "amount": _to_float(raw.get("amount"), 0.0) or 0.0,
        "last_close": last_close if last_close else float("nan"),
        "change_pct": change_pct,
        "source": source,
    }


def _missing_row(code: str) -> dict:
    """缺价行:所有数值 NaN,source='missing'。"""
    nan = float("nan")
    return {
        "code": code,
        "open": nan, "high": nan, "low": nan, "price": nan,
        "volume": nan, "amount": nan,
        "last_close": nan, "change_pct": nan,
        "source": "missing",
    }


# ── orchestrator(深 module 主体)────────────────────────────────
class QuoteSource:
    """行情 sourcing orchestrator:per-code 逐只降级。

    持有一组按优先级排序的 QuoteAdapter。对每个 code,从高优先级起试,
    第一个能解出来的 adapter 胜出;全解不到 → missing 行。

    缓存 + 熔断(grilling Q8):orchestrator 级 3s TTL 缓存(同 code TTL 内不重新
    fetch)、每源 30s 可用性熔断(adapter.fetch 抛异常 → 熔断窗内跳过)。
    _clock 可注入便于测试。
    """

    def __init__(
        self,
        adapters: Optional[list[QuoteAdapter]] = None,
        cache_ttl: float = 3.0,
        breaker_cooldown: float = 30.0,
        _clock: Optional[Callable[[], float]] = None,
    ):
        self._adapters: list[QuoteAdapter] = list(adapters or [])
        self._cache_ttl = cache_ttl
        self._breaker_cooldown = breaker_cooldown
        self._clock: Callable[[], float] = _clock or time.time
        self._cache: dict[str, tuple[dict, float]] = {}   # code -> (row_dict, expire_ts)
        self._breaker: dict[str, float] = {}              # adapter.name -> failed_until_ts
        # H4 fix: 多线程并发(sim_trader/intraday_monitor + API + live_trader)调
        # 同一单例时,缓存/熔断器的读-改-写需用锁保护。CPython GIL 只保证单条
        # 操作原子,复合操作(读 cache + 写 cache、读 breaker + 写 breaker)不安全。
        self._lock = threading.RLock()

    def _adapter_available(self, name: str) -> bool:
        until = self._breaker.get(name)
        return not (until and self._clock() < until)

    def get_realtime_quotes(self, codes: list[str]) -> pd.DataFrame:
        if not codes:
            return pd.DataFrame(columns=CONTRACT_COLUMNS)

        now = self._clock()
        row_by_code: dict[str, dict] = {}
        pending: list[str] = []

        # 1. 吃缓存(快照 — 锁内快速 read-only)
        with self._lock:
            for code in codes:
                hit = self._cache.get(code)
                if hit and now < hit[1]:
                    row_by_code[code] = hit[0]
                else:
                    pending.append(code)

        # 2. 逐 adapter 降级 pending(不持锁,fetch 可能秒级)
        resolved: dict[str, tuple[dict, str]] = {}
        still: list[str] = list(pending)
        breaker_updates: dict[str, float] = {}  # 收集后批量写,缩短临界区
        for adapter in self._adapters:
            if not still:
                break
            # 读熔断状态(短锁)
            with self._lock:
                available = self._adapter_available(adapter.name)
            if not available:
                continue
            try:
                fetched = adapter.fetch(list(still))
            except Exception:
                # adapter 故障 → 标记熔断,稍后批量写入
                breaker_updates[adapter.name] = now + self._breaker_cooldown
                continue
            if not fetched:
                continue  # 合法空(无数据),不熔断
            nxt: list[str] = []
            for code in still:
                if code not in resolved and code in fetched:
                    resolved[code] = (fetched[code], adapter.name)
                else:
                    nxt.append(code)
            still = nxt

        # 3. 建 row + 写缓存 + 写熔断器(短锁聚合)
        expire = now + self._cache_ttl
        with self._lock:
            self._breaker.update(breaker_updates)
            for code in pending:
                if code in resolved:
                    row = _make_row(code, *resolved[code])
                else:
                    row = _missing_row(code)
                row_by_code[code] = row
                self._cache[code] = (row, expire)

        return pd.DataFrame([row_by_code[c] for c in codes], columns=CONTRACT_COLUMNS)


# ── 真实 adapter(Phase 1b)──────────────────────────────────────
# 每个 adapter:fetch 做 I/O(复刻现有 engine.py / qmt.py 逻辑,保证 Phase 2 委托后
# 行为不变);translate / parse_* / row_to_raw 是纯函数,已单测。


class QmtHttpAdapter:
    """QMT 源:经 live_trader:8001 HTTP(server 侧 QMT 唯一 adapter,grilling Q3)。

    fetch 调 qmt_gateway.get_live_trader_quotes(纯 QMT,live_trader:8001);
    腾讯/Parquet 兜底由 quote_source 其他 adapter 负责。
    """

    name = "qmt"

    @staticmethod
    def translate(gateway_dict: dict) -> dict:
        out: dict[str, dict] = {}
        for code, q in (gateway_dict or {}).items():
            price = _to_float(q.get("lastPrice"))
            if price is None or price <= 0:
                continue
            raw: dict = {
                "price": price,
                "lastClose": q.get("lastClose"),
                "open": q.get("open"),
                "high": q.get("high"),
                "low": q.get("low"),
                "volume": q.get("volume"),
            }
            if q.get("preClose"):
                raw["preClose"] = q.get("preClose")
            if q.get("amount") is not None:
                raw["amount"] = q.get("amount")
            out[code] = raw
        return out

    def fetch(self, codes: list[str]) -> dict:
        # 只取 live_trader:8001(QMT 纯源);腾讯/Parquet 兜底由 quote_source 其他 adapter 负责。
        # 这样 4 个 adapter 真正独立,昨收规则(含 Parquet 不冒充)在 orchestrator 统一生效。
        from app.trader.gateways.qmt import qmt_gateway
        return self.translate(qmt_gateway.get_live_trader_quotes(list(codes)) or {})


class TencentAdapter:
    """腾讯 HTTP 源。合并自 engine.py:291-339 / data_loader.py:104-152 / qmt.py:171-199。"""

    name = "tencent"

    @staticmethod
    def parse_response(text: str, code_lookup: dict) -> dict:
        """s_ 批量响应解析(复刻 engine.py:303-335 索引)。
        parts[2]=code, [3]=price, [4]=chg, [6]=vol;last_close=price-chg。
        """
        out: dict[str, dict] = {}
        for line in (text or "").split(";"):
            if "~" not in line or "=" not in line:
                continue
            try:
                seg = line.split("=", 1)[1].replace('"', "").strip()
                parts = seg.split("~")
                if len(parts) < 7:
                    continue
                code_raw = parts[2]
                if not code_raw:
                    continue
                price = float(parts[3])
                if price <= 0:
                    continue
                chg = float(parts[4])
                orig = code_lookup.get(code_raw, code_raw)
                out[orig] = {
                    "price": price,
                    "lastClose": price - chg,
                    "volume": float(parts[6]) if len(parts) > 6 else 0,
                }
            except (ValueError, IndexError):
                continue
        return out

    @staticmethod
    def _tenc_code(code: str) -> str:
        """代码 → 腾讯行情代码(s_ 前缀)。尊重入参后缀,不再用数字猜市场。

        旧实现剥后缀后把 000 开头一律判沪市,导致 000001.SZ(平安银行)被查成
        s_sh000001(上证指数 ~3955 点)。现规则:带后缀按后缀;裸码按数字
        (6→沪,8/4→北,其余 0/3→深)。裸 000001 默认深市股票(平安银行),
        指数查询走 .SH 后缀路径(engine.get_index_realtime 用 '000001.SH')。
        """
        c = str(code)
        if "." in c:
            bare, suffix = c.split(".", 1)
            prefix = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suffix.upper(), "sz")
        else:
            bare = c
            if c.startswith("6"):
                prefix = "sh"
            elif c.startswith(("8", "4")):
                prefix = "bj"
            else:  # 0/3 开头 → 深市(含 000001 平安银行)
                prefix = "sz"
        return f"s_{prefix}{bare}"

    def fetch(self, codes: list[str]) -> dict:
        import requests

        if not codes:
            return {}
        code_lookup = {str(c).split(".")[0]: str(c) for c in codes}
        tenc_codes = [self._tenc_code(c) for c in codes]
        out: dict[str, dict] = {}
        for i in range(0, len(tenc_codes), 300):
            batch = tenc_codes[i:i + 300]
            url = f"http://qt.gtimg.cn/q={','.join(batch)}"
            try:
                resp = requests.get(url, timeout=5)
                if resp.status_code != 200:
                    continue
                out.update(self.parse_response(resp.text, code_lookup))
            except Exception:
                continue
        return out


class TdxAdapter:
    """TDX socket 源(grilling Q2:留作第4源;CLAUDE.md 已补 TDX)。
    复刻原 engine.py:242-289 的 TDX 兜底逻辑。
    """

    name = "tdx"

    @staticmethod
    def parse_quotes(quotes, tdx_to_orig: dict) -> dict:
        out: dict[str, dict] = {}
        for q in (quotes or []):
            try:
                price = _to_float(q.get("price"))
                last_close = _to_float(q.get("last_close")) or _to_float(q.get("pre_close"))
                if price is None or price <= 0:
                    price = last_close  # engine.py:273 兜底
                if price is None or price <= 0:
                    continue
                orig = tdx_to_orig.get(q.get("code"), q.get("code"))
                out[orig] = {
                    "price": price,
                    "lastClose": last_close,
                    "open": q.get("open"),
                    "high": q.get("high"),
                    "low": q.get("low"),
                    "volume": q.get("vol", 0),
                    "amount": q.get("amount", 0),
                }
            except Exception:
                continue
        return out

    def fetch(self, codes: list[str]) -> dict:
        # 复刻 engine.py:243-287:连接 TDX、80 只一批、市场推断、get_security_quotes
        if not codes:
            return {}
        try:
            from pytdx2.hq import TdxHq_API
        except ImportError:
            return {}
        api = TdxHq_API()
        tdx_to_orig: dict[str, str] = {}
        try:
            if not api.connect("119.147.212.81", 7709, time_out=2):
                return {}
            collected = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                tdx_queries = []
                for c in batch:
                    c_str = str(c)
                    if "." in c_str:
                        parts = c_str.split(".")
                        clean_code = parts[0]
                        market = 1 if parts[1].upper() == "SH" else 0
                    else:
                        clean_code = c_str
                        market = 1 if (clean_code.startswith("6") or
                                       (clean_code.startswith("000") and len(clean_code) <= 6)) else 0
                    tdx_queries.append((market, clean_code))
                    tdx_to_orig[clean_code] = c_str
                qs = api.get_security_quotes(tdx_queries)
                if qs:
                    collected.extend(qs)
            return self.parse_quotes(collected, tdx_to_orig)
        except Exception:
            return {}
        finally:
            try:
                api.disconnect()
            except Exception:
                pass


class ParquetAdapter:
    """Parquet 历史收盘价兜底(无昨收 → last_close=NaN,守 CLAUDE.md:26)。
    本 adapter 不用 close 冒充昨收。历史:2026-07 候选① 已把所有调 qmt.get_realtime_quotes
    的旧 Parquet 冒充路径替换为本 adapter(见 commit 69e1632),此处仅保留首遍文档。
    """

    name = "parquet"

    @staticmethod
    def row_to_raw(row) -> dict | None:
        close = _to_float(row.get("close") if hasattr(row, "get") else None)
        if close is None or close <= 0:
            return None
        return {
            "price": close,
            "open": row.get("open"),
            "high": row.get("high"),
            "low": row.get("low"),
            "volume": row.get("volume", 0),
            # 故意不设 lastClose:Parquet 无真实昨收
        }

    def fetch(self, codes: list[str]) -> dict:
        import os
        import pandas as pd

        out: dict[str, dict] = {}
        for code in codes:
            code_bare = str(code).split(".")[0]
            pq_path = f"data/parquet/daily/{code_bare}.parquet"
            if not os.path.exists(pq_path):
                continue
            try:
                df = pd.read_parquet(pq_path)
                if df.empty:
                    continue
                raw = self.row_to_raw(df.iloc[-1])
                if raw:
                    out[str(code)] = raw
            except Exception:
                continue
        return out


# ── 模块级 port(单例)──────────────────────────────────────────
# Phase 1b:默认 source 已接入 4 adapter。空输入短路(不构造 adapter,测试用)。
_default_source: Optional[QuoteSource] = None


def _build_default_source() -> QuoteSource:
    """默认 4-adapter source,按 SOURCE_PRIORITY 顺序接入。"""
    return QuoteSource(adapters=[
        QmtHttpAdapter(),
        TdxAdapter(),
        TencentAdapter(),
        ParquetAdapter(),
    ])


def _get_default_source() -> QuoteSource:
    global _default_source
    if _default_source is None:
        _default_source = _build_default_source()
    return _default_source


def get_realtime_quotes(codes: list[str]) -> pd.DataFrame:
    """模块级 port:走默认 4-adapter source。

    空输入短路(不构造 adapter),非空输入走默认单例。
    """
    if not codes:
        return pd.DataFrame(columns=CONTRACT_COLUMNS)
    return _get_default_source().get_realtime_quotes(codes)
