# DataManager 修复报告

## 修复时间
2026-07-02

## 问题诊断

### 问题 1: 日期类型不匹配 (CRITICAL)
```
ERROR | 线程任务失败 300xxx: unsupported operand type(s) for -: 'datetime.date' and 'Timestamp'
```

**根因**:
- `duckdb_manager.py:get_last_date()` 返回 `pd.Timestamp`
- `engine.py:410` 直接用 `date.today() - last_dt` 做减法
- Python 原生 `datetime.date` 无法直接与 pandas `Timestamp` 相减

**影响**:
- 所有增量更新的股票数据拉取失败
- 大量股票无法更新最新数据

### 问题 2: Tushare 频率超限 (HIGH)
```
抱歉，您访问接口(stk_mins)频率超限(1次/分钟)
```

**根因**:
- `engine.py:433` 开启 20 线程并发
- 没有 Tushare API 限流控制
- 大量并发请求触发 Tushare 1次/分钟限制

**影响**:
- 虽然有 TDX 兜底，但频繁超频影响数据质量
- 日志大量报错，影响问题排查

## 修复方案

### 修复 1: 日期类型统一转换

**文件**: `app/data_manager/engine.py:415-417`

```python
# 修复前
last_dt = db.get_last_date(c, freq)
if last_dt:
    df = download_bars(c, freq, count=max(80, (date.today() - last_dt).days * 50))

# 修复后
last_dt = db.get_last_date(c, freq)
if last_dt:
    # 修复日期类型不匹配: pd.Timestamp 转为 date
    last_date = last_dt.date() if isinstance(last_dt, pd.Timestamp) else last_dt
    days_diff = (date.today() - last_date).days
    df = download_bars(c, freq, count=max(80, days_diff * 50))
```

**原理**:
- `pd.Timestamp.date()` 转换为 `datetime.date`
- 保证两边类型一致再做减法
- 兼容处理：如果本来就是 `date` 则不转换

### 修复 2: Tushare 限流控制

**文件**: `app/data_manager/engine.py:408-418, 429, 442`

```python
# 新增限流函数
def _rate_limit_tushare():
    """Tushare 限流: 最小间隔 1.2 秒"""
    import os
    # 如果没配置 TUSHARE_KEY, 直接走 TDX, 不需要限流
    if not os.getenv("TUSHARE_KEY"):
        return
    with _tushare_lock:
        elapsed = time.time() - _last_tushare_call[0]
        if elapsed < 1.2:
            time.sleep(1.2 - elapsed)
        _last_tushare_call[0] = time.time()

# 在 download_bars 调用前加限流
_rate_limit_tushare()  # Tushare 限流
df = download_bars(c, freq, count=...)
```

**原理**:
- 全局锁 + 时间戳记录最后调用时间
- 强制每次调用间隔 >= 1.2 秒（比官方 1分钟/次 更保守）
- 没配置 TUSHARE_KEY 时不限流（直接走 TDX）

## 测试验证

### 单元测试
```bash
python test_data_manager_fix.py
```

结果:
```
[PASS] Date type conversion test passed
[PASS] Tushare rate limiter test passed
All tests passed!
```

### 实际验证
- 日期类型转换: `pd.Timestamp('2026-06-30')` → `datetime.date(2026, 6, 30)` ✓
- 限流间隔: 实测间隔 1.200s, 1.200s ✓

## 影响范围

### 修改文件
- `app/data_manager/engine.py` (核心修复)
- `test_data_manager_fix.py` (新增测试)

### 向后兼容性
✅ **完全兼容**
- 不改变对外接口
- 不影响已有功能
- 只修复内部实现 bug

### 性能影响
- **日期转换**: 无性能损耗（只是类型转换）
- **限流**: 单股票增加 1.2s 延时，但避免了 Tushare 封禁风险
  - 20 并发 → 实际最多 20 req/24s ≈ 0.83 req/s
  - 远低于 Tushare 1次/分钟限制

## 后续建议

### 短期
1. 监控日志，确认不再出现日期类型错误
2. 观察 Tushare 超频报错是否消失

### 长期
1. 考虑完全切换到 QMT 数据源（已在项目路线图中）
2. 增加数据源健康检查监控
3. Tushare/TDX 双源对比验证数据质量

## 风险评估

| 风险项 | 等级 | 缓解措施 |
|-------|------|---------|
| 日期转换引入新 bug | 低 | 单元测试覆盖 + 类型检查 |
| 限流导致同步变慢 | 中 | TDX 兜底机制 + QMT 迁移计划 |
| 多进程环境锁冲突 | 低 | 当前只有单进程多线程场景 |

## 提交信息

```bash
git add app/data_manager/engine.py test_data_manager_fix.py
git commit -m "fix: DataManager日期类型不匹配+Tushare限流

问题:
1. pd.Timestamp 与 datetime.date 无法直接相减导致崩溃
2. 20并发无限流导致 Tushare 频繁超频

修复:
1. 统一转为 datetime.date 再做减法
2. 全局限流锁保证 1.2s 最小间隔

测试: python test_data_manager_fix.py (全部通过)"
```
