# 实时行情消失问题修复方案

## 问题现象
- 自选股表格、交易控制 TAB 的持仓/交易记录的实时涨跌全部不刷新
- 价格字段显示为空或 null
- 所有股票都没有实时价格数据

## 根本原因
**QMT 代理服务（qmt_proxy_server.py）未启动**，导致后端无法获取实时行情。

## 影响链路
```
前端 WebSocket 订阅 → 后端 broadcaster → MarketQuotes 
  → 调用 http://localhost:8081/api/quotes （❌ 无响应）
  → 降级 TDX 回退（也可能失败）
  → 最终返回空数据给前端
```

## 立即修复步骤

### 方案 A：重启 QMT 代理（临时）

```bash
cd /e/1target/p9_project/quant-platform
python qmt_proxy_server.py
```

**注意**：需要在 Windows 宿主机上运行，不是 Docker 容器内。

### 方案 B：切换到 live_trader（推荐）

QMT 代理已**废弃**，改用 live_trader：

1. **修改配置**：
   ```json
   // config/app_setting.json
   {
     "gateway": {
       "qmt_proxy_url": "http://localhost:8001"  // 从 8081 改为 8001
     }
   }
   ```

2. **启动 live_trader**：
   ```bash
   cd app/live_trader
   python main.py
   ```

3. **验证行情**：
   ```bash
   curl http://localhost:8001/live/quotes?codes=000001.SH
   ```

### 方案 C：增加降级行情源鲁棒性（根治）

修改 `server/market/quotes.py`，增强 TDX 回退逻辑：

```python
async def get_realtime_quotes(self, codes: List[str]) -> Dict[str, Any]:
    if not codes:
        return {}

    # 1. 优先 QMT/live_trader
    if settings.get('gateway', 'active_gateway') == 'qmt':
        result = await self.get_qmt_quotes(codes)
        if result:
            return result
        log.warning("QMT 行情失败，降级到 TDX 回退")

    # 2. TDX HTTP 回退
    result = await self.get_fallback_quotes(codes)
    if result:
        return result
    
    # 3. 最后兜底：返回昨日收盘价（避免前端全空）
    log.error("所有行情源均失败，使用昨日收盘价兜底")
    return await self.get_last_close_fallback(codes)

async def get_last_close_fallback(self, codes: List[str]) -> Dict[str, Any]:
    """兜底：从 Parquet 读取昨日收盘价"""
    try:
        from database.duckdb_manager import db
        result = {}
        for code in codes:
            df = db.load_bars(code, freq='daily')
            if df is not None and not df.empty:
                last_row = df.iloc[-1]
                result[code] = {
                    'price': float(last_row.get('close', 0)),
                    'lastPrice': float(last_row.get('close', 0)),
                    'lastClose': float(last_row.get('close', 0)),
                    'preClose': float(last_row.get('close', 0)),
                    'change_pct': 0.0,
                    'priceChangeRatio': 0.0,
                    'open': float(last_row.get('open', 0)),
                    'high': float(last_row.get('high', 0)),
                    'low': float(last_row.get('low', 0)),
                    'volume': float(last_row.get('volume', 0)),
                    'amount': float(last_row.get('amount', 0)),
                }
        return result
    except Exception as e:
        log.error(f"兜底行情源失败: {e}")
        return {}
```

## 长期防护措施

### 1. 启动时自检
在 `app/main.py` 的启动逻辑中增加行情健康检查：

```python
@app.on_event("startup")
async def check_market_gateway():
    from server.market.quotes import MarketQuotes
    mq = MarketQuotes()
    test_codes = ['000001.SH']
    result = await mq.get_realtime_quotes(test_codes)
    if not result:
        log.warning("⚠️ 行情网关不可用，请检查 QMT 代理或 live_trader 是否启动")
    else:
        log.info("✅ 行情网关正常")
```

### 2. 前端降级提示
在 `static/js/market-updater.js` 增加空数据告警：

```javascript
handleMarketQuotes(msg) {
    const data = msg.data || msg;
    
    // 检测是否全空
    if (Object.keys(data).length === 0) {
        console.warn('⚠️ 行情数据为空，请检查后端行情源');
        createNotification('warning', '行情源连接失败，正在尝试重连...');
        // 5秒后重新订阅
        setTimeout(() => this.resubscribe(), 5000);
        return;
    }
    
    // ... 原有处理逻辑
}
```

### 3. 监控告警
在 `server/market/broadcaster.py` 增加空广播告警：

```python
async def _broadcast_once(self):
    codes = self.subscription_manager.get_all_codes()
    if not codes:
        return

    quotes = await self.market_quotes.get_realtime_quotes(list(codes))
    if not quotes:
        log.warning(f"广播失败：无行情数据（订阅数: {len(codes)}）")
        # 连续 3 次空数据则告警
        self._empty_count = getattr(self, '_empty_count', 0) + 1
        if self._empty_count >= 3:
            await manager.broadcast({
                'type': 'error',
                'message': '行情源连续失败，请检查 QMT 代理或 live_trader'
            })
            self._empty_count = 0
        return
    
    self._empty_count = 0  # 重置计数
    await manager.broadcast({
        'type': 'market_quotes',
        'data': quotes
    })
```

## 验证步骤

1. **启动行情源**（方案 A 或 B）
2. **重启主服务**：`docker-compose restart web` 或重启 Flask/FastAPI
3. **打开浏览器控制台**：查看 WebSocket 消息
4. **检查自选股表格**：价格应该开始刷新
5. **检查交易控制 TAB**：实时涨跌应该有数据

## 根因总结

| 层级 | 问题 |
|---|---|
| **直接原因** | QMT 代理服务未启动（8081 端口无响应） |
| **技术根因** | 行情订阅链路缺少兜底机制，QMT 失败后 TDX 回退也失败 |
| **设计缺陷** | 无行情源健康检查，无空数据告警，前端静默失败 |

## 相关文件

- `qmt_proxy_server.py` - QMT 代理（已废弃）
- `app/live_trader/main.py` - live_trader 实盘（替代方案）
- `server/market/quotes.py` - 行情获取逻辑
- `server/market/broadcaster.py` - 行情广播器
- `static/js/market-updater.js` - 前端行情订阅
- `static/js/websocket.js` - WebSocket 管理

## 参考

- CLAUDE.md 规则：行情数据优先用 QMT
- qmt_proxy_server.py:1-17 - 废弃声明
