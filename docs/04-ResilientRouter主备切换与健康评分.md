# 04 ResilientRouter 主备切换与健康评分

一句话定位：任何一个数据源挂掉，都不应该让平台停摆——路由层负责自动切备源、熔断故障源、并在恢复后把它放回来。

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `backend/app/providers/router.py` | `ResilientRouter`：候选链、逐个尝试、故障切换 |
| `backend/app/providers/health.py` | 健康评分、连续失败计数、熔断 cooldown、恢复判定 |
| `backend/app/providers/transport.py` | 统一 HTTP：超时、重试、限流与错误分类 |
| `backend/app/core/config.py` | 阈值配置 |

## 切换流程

```
fetch(category)
  ├─ 取该 category 的候选链（按 priority 排序）
  ├─ 过滤：enabled 且 is_configured() 且不在熔断 cooldown 中
  ├─ 按顺序逐个 _try_one()
  │    ├─ 成功 → _sanity() 健全性检查 → 返回结果
  │    └─ 失败 → 记 health.record_failure → 试下一个
  ├─ 全部失败 → 返回最后一次可信数据，标记 STALE
  └─ 发生切换 → health.record_failover(from, to, reason)
```

关键点：

- `healthy = [p for p in chain if p.enabled and p.is_configured() and not self.health.in_cooldown(p.name)]`
- 熔断中的源会被**跳过**，但仍记录原因，便于排查。
- 切换事件写入 `provider_failover_events` 表，页面上可追溯「什么时候、从谁切到谁、为什么」。

## 健康评分（0~100）

`HealthTracker.compute_score()` 的构成：

| 维度 | 权重 | 计算 |
| --- | --- | --- |
| stability | 0.35 | `0.6 × 24h成功率 + 0.4 × 1h成功率`；无数据时给 100（不惩罚） |
| consistency | 0.20 | 交叉验证一致性 × 100 |
| latency | 0.15 | ≤200ms 满分，≥3000ms 零分，线性衰减 |
| completeness | 0.15 | 字段完整度 × 100 |
| freshness | 0.15 | 距上次成功 > 3600s 后线性衰减（每小时扣 20） |
| penalty | 减项 | `min(40, 连续失败次数 × 10)` |

最终 `score = Σ(维度 × 权重) - penalty`，钳制在 `[0, 100]`。

## 状态判定

`status_of()` 按以下顺序判定：

| 条件 | 状态 |
| --- | --- |
| 手工指定 | `manual_status` |
| 未启用 | `DISABLED` |
| 未配置 Key | `NOT_CONFIGURED` |
| 限流中 | `RATE_LIMITED` |
| 连续失败 ≥ `FAILURE_THRESHOLD` | 按失败类型映射（如 `OFFLINE`） |
| 连续失败 > 0 但未达阈值 | `DEGRADED` |
| 正常 | `ONLINE` |

## 熔断与恢复

| 机制 | 规则 |
| --- | --- |
| 熔断触发 | 连续失败 ≥ `FAILURE_THRESHOLD`（默认 **3**） |
| 熔断时长 | `base × 2^min(连续失败-2, 4)`，上限 **300 秒**（指数退避） |
| 恢复条件 | 连续成功 ≥ `RECOVERY_SUCCESS_STREAK`（默认 **3**） |
| 恢复冷却 | 距上次失败 ≥ `RECOVERY_COOLDOWN_SECONDS`（默认 **180** 秒） |
| 恢复动作 | 清零 `consecutive_failures` 与 `consecutive_successes` |

配置项见 `backend/app/core/config.py`：

```
FAILURE_THRESHOLD = 3
RECOVERY_SUCCESS_STREAK = 3
RECOVERY_COOLDOWN_SECONDS = 180
HEALTH_WINDOW_24H = 86400
```

## 请求留痕

- `provider_requests` 表记录每次请求（耗时、结果）。
- `provider_health` 表保存每个源的健康快照。
- `provider_failover_events` 表保存切换事件。
- 页面「数据源中心」可查看健康分、连续失败次数、是否熔断、切换历史。

## 与智能监测的关系

`Alert Engine` **不自己选源**，只消费 router 给出的结果与来源标记：

- 事件中记录 `provider`（实际使用的源）与 `data_quality`。
- 告警邮件中显示「主要数据源 / 备用数据源」。
- 主源失效时，只要备源拿到数据，预警照常工作（有测试锁定：`test_alert_works_when_primary_provider_fails`）。

## 相关文档

- 数据源清单见 `03-数据源注册表与数据源清单.md`
- 交叉验证见 `05-交叉验证与数据置信度.md`
- 可观测性见 `09-可观测性日志请求ID与审计留痕.md`

> 本文只对代码事实负责；如代码变更后本文过期，请以代码为准。
