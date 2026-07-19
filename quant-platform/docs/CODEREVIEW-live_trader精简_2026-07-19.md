# Code Review：live_trader 精简清理（未提交工作区改动）

> 日期：2026-07-19（周日）
> 模式：Local Review（`/code-review`）
> 审查对象：`git diff HEAD` 未提交改动，共 2 个 Python 文件（19 增 25 删）
> 审查方法：主控独立核验 + python-reviewer / silent-failure-hunter 双 agent 并行（范围不重叠、禁止派子 agent），关键结论主控交叉验证
> **决策：REQUEST CHANGES**（有 1 个 HIGH，需修了再提交；main.py 部分干净可单独提交）

---

## 总结论

这次改动是 **2026-07-19 实盘主流程审计修复（commit 1df55f1）之后的精简清理**，意图是好的（简化代码、补 docstring 口径）。但精简过程中**不小心把上一次修复刚建好的"reload 失败自动重试"给去掉了**，在真钱实盘场景下构成一条静默失败路径。其余改动（main.py 内联重构）完全干净。

- 本次 diff **真正引入的问题**：1 个（HIGH）
- 本次 diff **顺手暴露的 pre-existing 问题**：5 个（不阻塞本次提交，建议另案）
- main.py 改动：**零风险**，可直接提交

---

## 审查范围（工作区 diff）

| 文件 | 改动 | 性质 |
|---|---|---|
| [app/live_trader/main.py](../app/live_trader/main.py) | `_process_one_signal` 内 `price_type`/`order_price` 两变量内联进 `OrderIntent` | 纯重构 |
| [app/live_trader/scheduler.py](../app/live_trader/scheduler.py) | `settings` import 提到模块顶 + `_maybe_reload_settings` 逻辑重写 + docstring 补"生效口径" | 重构 + 回归 |

---

## Findings

### CRITICAL — 无

### HIGH — 1 条（本次 diff 引入，阻塞提交）

#### H-1：`_maybe_reload_settings` reload 失败后永久不再重试（重试语义回归）

**位置**：[app/live_trader/scheduler.py:151-157](../app/live_trader/scheduler.py#L151-L157)

```python
prev = self._last_cfg_mtime
self._last_cfg_mtime = mtime          # ← reload 之前就更新了
if prev is None or mtime == prev:
    return
logger.info(f"检测到 app_setting.json 变化(mtime {prev} -> {mtime}), reload settings")
settings.reload()                     # ← 若抛异常, _last_cfg_mtime 已是新值
```

**回归证据**（`git show HEAD`）——旧版是 reload 在前、mtime 更新在后：
```python
if mtime != self._last_cfg_mtime:
    logger.info(...)
    settings.reload()                 # 抛异常 → 下一行不执行
    self._last_cfg_mtime = mtime      # mtime 保持旧值 → 下次 tick 自动重试
```

**静默失败链路**（已主控交叉验证每一步）：
1. 主 API（8888）调 `settings.save()` 落盘 —— [core/settings.py:88-93](../core/settings.py#L88-L93) 用 `open("w")` 先截断再 `json.dump`，**非原子写**（无 tmp+rename）。
2. 实盘（8001）每秒 stat 一次，恰好撞上"文件已截断、内容未写完"的窗口（ms 级）。
3. `settings.reload()` → [core/settings.py:84-86](../core/settings.py#L84-L86) 调 `_load()` → [core/settings.py:33-34](../core/settings.py#L33-L34) 的 `json.load(f)` 读到半截 JSON → 抛 `JSONDecodeError`（**此句无局部 try/except**，仅 `_check_risk_consistency` 那段被兜住）。
4. 异常冒到 [`_loop`](../app/live_trader/scheduler.py#L159-L168) 被 `except Exception as e: logger.error(f"调度异常: {e}")` 吞成一条笼统日志。**主循环不崩**，但当秒 `_tick` 剩余子任务（exit_scan / quotes_refresh 等）全部跳过。
5. 下一个 tick：`mtime == prev`（新==新）→ 直接 return，**永不重试**。
6. 用户看到 [scheduler.py:156](../app/live_trader/scheduler.py#L156) 那条 `检测到...变化, reload settings` 的 info 日志，**以为成功了**，实际参数卡在旧值，直到文件再次被改写。

**影响**：真钱场景下，用户盘中想收紧止损/止盈 → 撞上 reload 窗口 → 实盘按旧阈值跑一天 + 无显式告警。是审计报告 FAIL-B 想治的"改了不生效"病的变体——本次精简把刚建好的自愈又削掉了。

**两个 agent 的定级分歧与主控仲裁**：

| Agent | 定级 | 主要论据 |
|---|---|---|
| python-reviewer | MEDIUM | 触发是稀有竞争窗口、旧值仍合法、下次改文件能自愈、主循环不崩 |
| silent-failure-hunter | CRITICAL | 真钱资金风险、是对 HEAD 自愈逻辑的主动回归、静默不告警 |
| **主控仲裁** | **HIGH** | 见下 |

**定 HIGH 的理由**：
- **不够 CRITICAL**：项目定义 CRITICAL = "安全漏洞或数据丢失风险"。这不是安全漏洞；触发需 ms 级竞争窗口被秒级 stat 撞上，概率低；失败时旧值仍合法、不崩、再改一次配置能恢复。不是"必定资金损失"。
- **超过 MEDIUM**：这是对 commit 1df55f1 刚建立的"reload 失败自愈"的**主动回归**（本次精简的目的就是简化那段代码，结果削掉了失败恢复路径），不是 latent bug；真钱上下文 + 静默不告警 + 用户误以为生效；修复极廉价（2 行重排）。
- HIGH（WARN，合并前应修）最贴切。

**建议修法**（保留新逻辑的简洁结构，恢复重试 + 明确告警）：
```python
def _maybe_reload_settings(self) -> None:
    try:
        mtime = os.path.getmtime(str(CONFIG_FILE))
    except OSError:
        return
    prev = self._last_cfg_mtime
    if prev is not None and mtime != prev:
        logger.info(f"检测到 app_setting.json 变化(mtime {prev} -> {mtime}), reload settings")
        try:
            settings.reload()
        except Exception:
            # 不更新 _last_cfg_mtime, 下次 tick 自动重试; 显式告警而非被 _loop 吞成"调度异常"
            logger.exception(f"settings.reload 失败, 配置仍为旧值, 下秒重试(mtime={mtime})")
            return
    self._last_cfg_mtime = mtime   # 仅成功(或首次/无变化)才落 mtime
```
四情况逐一验证：首次 tick / mtime 不变 → 不 reload 只更新 mtime；mtime 变且 reload 成功 → 更新 mtime；mtime 变且 reload 失败 → 不更新 mtime + 不上抛 + 下次重试。全部正确。

---

### MEDIUM — 1 条（本次 diff 引入）

#### M-1：reload 的 info 日志在 reload 之前打，失败时误导排查
**位置**：[app/live_trader/scheduler.py:156](../app/live_trader/scheduler.py#L156)
reload 失败时日志里会同时出现 `检测到变化, reload settings`(info) + `调度异常: ...`(error)，排查时容易只看 info 误判成功。修法：把 info 挪到 reload 成功之后，或在异常分支显式打 error（H-1 的修法已含此意）。

> 注：旧版（HEAD）也是 reload 前打 info，严格说是 pre-existing；但既然 H-1 要重写这段，顺带修掉成本为零。

---

### LOW / 附带发现（pre-existing，不阻塞本次提交，建议另案）

以下均非本次 diff 引入，但双 agent 顺链发现，记录备查：

| 编号 | 位置 | 问题 | 建议 |
|---|---|---|---|
| **P-1** | [core/settings.py:88-93](../core/settings.py#L88-L93) | `save()` 非原子写（`open("w")` 截断 + `json.dump`，无 tmp+rename）—— **H-1 的触发根因** | 改原子写：先写 `app_setting.json.tmp` 再 `os.replace(tmp, CONFIG_FILE)`。根治后 H-1 的竞争窗口消失 |
| **P-2** | [scheduler.py:166-167](../app/live_trader/scheduler.py#L166-L167) | `_loop` 兜底 `logger.error(f"调度异常: {e}")` 丢栈、不用 `notifier`（`__init__` 已注入），低于自家 [safe_task.py:51-52](../app/scheduler/safe_task.py#L51-L52) 的 `self._log.exception(...)` 标准 | 改 `logger.exception(...)`；真钱路径异常额外 `notifier.send` |
| **P-3** | [scheduler.py:148-151](../app/live_trader/scheduler.py#L148-L151) | `OSError` 静默 return 无告警（CONFIG_FILE 被误删/权限丢失时热加载被悄悄禁用） | 至少 `logger.warning(...)`，首次发生 `notifier` 告警一次（标志位去重防刷屏） |
| **P-4** | [main.py:1006-1011](../app/live_trader/main.py#L1006-L1011) | `signal.price<=0` 时用 `price=10.0` 兜底估算 volume；实际股价远高于 10 元（如 100 元）时，估算 volume 偏大 10 倍 → 配合对手方最优市价单，实际成交金额可能显著超 `max_buy_amount` | 真钱路径隐患，建议单独立项审计（用 QMT 实时价估算 volume，而非写死 10 元） |
| **P-5** | [core/settings.py:81-82](../core/settings.py#L81-L82) | `_check_risk_consistency` 裸 `except Exception: pass` 会吞未来引入的 bug | 改 `logger.debug(..., exc_info=True)` |
| **P-6** | [scheduler.py:149](../app/live_trader/scheduler.py#L149) | `str(CONFIG_FILE)` 多余（`CONFIG_FILE` 已是 `Path`，`os.path.getmtime` 原生接受） | 纯洁癖，可选 |

---

## main.py 改动专项（干净）

[app/live_trader/main.py:1018-1032](../app/live_trader/main.py#L1018-L1032) 把 `price_type`/`order_price` 两个只被用一次的局部常量内联进 `OrderIntent` 构造。双 agent 一致确认：

- **运行时行为完全等价**，无副作用差异（`PRICE_TYPE_PEER_FIRST` 仍是函数内 lazy import，零风险）。
- 顺链核查市价单（`price=0` + `PRICE_TYPE_PEER_FIRST`）下单路径 **无静默吞错**：
  - `OrderIntent`（schemas.py）是裸 dataclass，无字段校验，`price=0` 原样接受；
  - [order_executor.py](../app/live_trader/order_executor.py) TDX 取价失败保持 `intent.price=0` 是市价单预期；QMT 调用异常有 `logger.warning`；
  - `seq<=0` 显式返回 `qmt_rejected`，`volume<=0` 显式返回 error dict，均不静默。

**结论**：main.py 可直接提交，不阻塞。

---

## 交叉验证记录（主控独立核对，防 context-dependency 错误）

| 验证项 | 核对方式 | 结论 |
|---|---|---|
| 旧版有重试顺序 | `git show HEAD:.../scheduler.py` | ✅ 旧版 reload 在前、mtime 更新在后，回归成立 |
| `reload()` 会抛异常 | Read [core/settings.py:29-39, 84-86](../core/settings.py#L29-L39) | ✅ `json.load` 无局部 try/except，`JSONDecodeError`/`PermissionError` 原样抛 |
| 主循环不崩 | Read [scheduler.py:159-168](../app/live_trader/scheduler.py#L159-L168) `_loop` | ✅ `except Exception` 兜底 + sleep 1s 继续 |
| import 提顶无循环依赖 | Grep `core/settings.py` import | ✅ 模块级全 stdlib，`app.*` 全 lazy import；`main.py`/`run.py` 等 5+ 文件同模式 |
| docstring"现读生效"属实 | Read [exit_monitor.py:182, 232-236](../app/live_trader/exit_monitor.py#L232-L236) | ✅ `_load_risk_params` 每次调 `load_risk_params()`+`asdict`，无缓存；reload 后下次 exit_scan 拿新值 |
| `save()` 非原子写 | Read [core/settings.py:88-93](../core/settings.py#L88-L93) | ✅ `open("w")` + `json.dump`，无 tmp+rename |
| `price=10.0` 兜底属实 | Read [main.py:1004-1011](../app/live_trader/main.py#L1004-L1011) | ✅ 属实，pre-existing |
| 语法 | `python -m py_compile` 两文件 | ✅ 通过 |

---

## 验证结果

| 检查 | 结果 |
|---|---|
| 语法（py_compile） | ✅ Pass |
| pytest | ⏭ 未跑（本次改动未触及测试覆盖路径，建议改 H-1 时补一条 reload 失败重试的单元测试） |

---

## 文件清单

| 文件 | 改动类型 | 阻塞？ |
|---|---|---|
| [app/live_trader/main.py](../app/live_trader/main.py) | 修改（内联重构） | ❌ 干净，可提交 |
| [app/live_trader/scheduler.py](../app/live_trader/scheduler.py) | 修改（import 提顶 + reload 逻辑重写 + docstring） | ✅ 修 H-1 后再提交 |

---

## 修复优先级建议

- **本次提交前必修**：H-1（scheduler.py 重试顺序回正 + 局部 try/except，约 4 行）。
- **本次提交前可选**：M-1（H-1 的修法已顺带覆盖，零额外成本）。
- **另案（不阻塞本次）**：P-1 原子写（根治 H-1 触发根因）→ P-2/P-3 热加载可见性 → P-4 真钱 volume 估算审计。
