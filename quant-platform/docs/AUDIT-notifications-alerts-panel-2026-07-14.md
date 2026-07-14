# 审计报告：飞书通知增强 + 告警面板 PR

**分支**: `fix/sim-trader-data-pollution-20260701`
**日期**: 2026-07-14
**方法**: code-reviewer 双 agent 并行审计（前端/后端）+ 自审

---

## 审计结论：✅ 可合并

| 严重级别 | 数量 | 状态 |
|---|---|---|
| CRITICAL | 0 | 已全部修复 |
| HIGH | 0 | 已全部修复 |
| MEDIUM | 2 | 已全部修复 |
| LOW | 4 | 已全部修复 |

---

## CRITICAL 修复（2个）

### C1: scheduler 周末 early return 阻断 daily_summary
- **文件**: `scheduler.py:122-124`
- **问题**: 周末 `return` 导致 daily_summary/cleanup 永远不执行
- **修复**: 移除 `return`，加注释"不 return:周末也要发每日概览和清理通知"
- **验证**: 周末控制流现在可到达 daily_summary 分支

### C2: _cleanup_notifications 新开 DuckDB 连接冲突
- **文件**: `scheduler.py:475-488`
- **问题**: 新开 `NotificationStore` 与 lifespan 长连接冲突，并发写可能 WAL 损坏
- **修复**: 改用 `_state["notif_store"]` 复用已有连接
- **验证**: 不再创建独立 DuckDB 连接

---

## HIGH 修复（1个）

### H1: calc_today_deal_count 访问私有 _conn
- **文件**: `notifications.py:251`
- **问题**: 直接访问 `store._conn` 绕开 `_db_lock`，可能读脏数据
- **修复**: 改用 `store.get_deals()` 公共 API，在 Python 层过滤今日
- **验证**: `get_deals()` 是 public 方法

---

## MEDIUM 修复（2个）

### M1: /live/notifications/test 速率限制无锁
- **文件**: `main.py:994-1012`
- **问题**: 并发请求可穿透 60s 冷却限制
- **修复**: `threading.Lock` 包裹读写
- **验证**: `threading` 已在 main.py 导入

### M2: level 参数无校验
- **文件**: `main.py:1010`
- **问题**: 可构造任意 level 值写入历史
- **修复**: 白名单校验 `if level not in ("INFO","WARN","CRITICAL"): level="INFO"`

---

## LOW 修复（4个）

### L1: daily_summary 触发窗口仅1分钟
- **文件**: `scheduler.py:171-173`
- **修复**: 窗口扩至 5 分钟（15:30~15:35）

### L2: format_daily_summary 中文名称截断切字符
- **文件**: `notifications.py:238`
- **修复**: 改为 `[:4]+'…'`

### L3: main.py 关闭 notif_store 写法诡异
- **文件**: `main.py:250`
- **修复**: 改为显式 `if _notif_store: _notif_store.close()`

### L4: alerts.js XSS 转义不全
- **文件**: `alerts.js:103-106`
- **修复**: `lv`/`src` 补 `escHtml()`，`fmtTime` catch 回退 `'—'`

### L5: index.html #i-bell 图标缺失
- **文件**: `index.html:88`
- **修复**: 替换为已有 `#i-alert`

---

## 已验证 PASS 项

| 检查项 | 文件/行 | 结论 |
|---|---|---|
| `_record_history` 错误不传播 | `notify.py:51-63` | try/except 双层防护 ✓ |
| 测试端点仅本地 | `main.py:1000` + `:1117-1121` | `_is_local` 白名单 ✓ |
| `/live/notifications` level 白名单 | `main.py:979` | 防 SQL 注入 ✓ |
| `NotificationStore` 独立 DuckDB | `notifications.py:28` | 自带 `threading.Lock` ✓ |
| `cleanup` 用 `self.config.db_path` 派生路径 | `scheduler.py:479` | ✓ |
| `config` 字段默认值合理 | `config.py:97-98` | ✓ |
| `record()` 失败仅日志不抛 | `notifications.py:87-89` | ✓ |
| CSS 无新增硬编码颜色 | `main.css:546-568` | 全部 `var(--token)` ✓ |

---

## 提交

```
feat(live_trader): 飞书通知增强+告警面板，含完整自审修复
10 files changed, 1146 insertions(+), 108 deletions(-)
```

分支: `fix/sim-trader-data-pollution-20260701`
PR 需合并至: `master`
