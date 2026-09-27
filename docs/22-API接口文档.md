# 22 · API 接口文档

后端 HTTP 接口的完整清单：真实路径、方法、鉴权要求与关键查询参数，全部来自 `backend/app/api/` 源码。

## 代码位置

| 文件 | 职责 |
| --- | --- |
| `backend/app/main.py` | 应用入口、路由挂载前缀、CORS、统一错误处理 |
| `backend/app/api/system.py` | 系统 / 数据源中心 / 任务 / 回填 |
| `backend/app/api/market.py` | 行情、分析、估值、周期、风险、预测 |
| `backend/app/api/research.py` | 回测 / 回放 / 策略 / 模型 / 指标字典 |
| `backend/app/api/portfolio.py` | 资金计划 / 交易账本 / 持仓 |
| `backend/app/api/assistant.py` | AI 解释助手 |
| `backend/app/api/deps.py` | 鉴权依赖（`AdminDep` / `WriteDep` / `RequestIdDep`） |
| `backend/app/api/schemas.py` | 请求体模型与字段校验 |

## 通用约定

| 项 | 事实 |
| --- | --- |
| 默认前缀 | `/api/v1`（`Settings.API_PREFIX`，可在 `.env` 覆盖） |
| 路由挂载 | `main.py` 中 `{prefix}/system`、`/market`、`/research`、`/portfolio`、`/assistant` |
| 响应格式 | 直接返回 JSON 对象，**没有** `{"data": ...}` 外层信封 |
| 请求追踪 | 请求头 `X-Request-ID`；未传时由 `bind_request_id()` 生成 16 位十六进制 |
| CORS | `main.py` 白名单来自 `CORS_ORIGINS`；允许的自定义头含 `X-Admin-Token` |
| 在线文档 | `/docs`（`APP_ENV=production` 时 `docs_url=None`，即关闭）；`/redoc` 始终关闭 |
| 其他路由 | `GET /healthz`（`{"status","ts"}`）、`GET /`（前端 `index.html`）、`/static` |

鉴权由两个依赖控制（均在 `deps.py`）：

- `AdminDep = require_admin`：仅当 `APP_ENV=production` 生效，要求请求头 `X-Admin-Token` **等于** `ADMIN_TOKEN`，否则 401 `unauthorized`；非生产环境直接放行。
- `WriteDep = require_write_permission`：仅当生产环境生效，**只校验 `X-Admin-Token` 是否存在**（未与 `ADMIN_TOKEN` 比对），缺失返回 403 `forbidden`。

错误响应统一为（`core/errors.py` 的 `AppError.to_dict()`）：

```json
{ "error": { "code": "not_found", "message": "计划不存在", "retryable": false, "detail": {} } }
```

## 系统 / 数据源中心 —— `/api/v1/system`

| 方法 | 路径 | 鉴权 | 关键参数 |
| --- | --- | --- | --- |
| GET | `/system/health` | 无 | 返回 `status`/`database`/`version`/`time` |
| GET | `/system/ready` | 无 | `ready` = 数据库连通 且 有可用 Provider |
| GET | `/system/info` | 无 | 应用/环境/时区/库类型/缓存类型（不含任何密钥） |
| GET | `/system/providers` | 无 | Provider 清单与配置 |
| GET | `/system/providers/dashboard` | 无 | 健康总览（含 `summary`） |
| GET | `/system/providers/categories` | 无 | 每个数据类别的 Provider 链 |
| GET | `/system/providers/failovers` | 无 | `limit` 1~500，默认 50 |
| POST | `/system/providers/test-all` | Admin | 一键探测全部数据源 |
| PATCH | `/system/providers/{name}` | Admin | 见下方请求体 |
| GET | `/system/data-quality` | 无 | 覆盖率 + 最近 30 条质量检查 |
| GET | `/system/jobs` | 无 | 调度任务列表与下次执行时间 |
| POST | `/system/jobs/{job_id}/run` | Admin | `job_id` 见「调度任务 ID」 |
| POST | `/system/backfill` | Admin | 请求体 `BackfillRequest` |
| GET | `/system/stats` | 无 | 各数据表行数 |
| GET | `/system/failover-events` | 无 | `limit` 1~500（与 `/providers/failovers` 等价别名） |
| GET | `/system/settings` | 无 | 后台配置中心：全部配置项（敏感项只回掩码/是否已填） |
| PUT | `/system/settings` | Admin | 请求体 `{"values": {"SMTP_HOST": "smtp.qq.com"}}`，**立即生效** |
| POST | `/system/settings/reset` | Admin | `{"keys": ["SMTP_HOST"]}`（省略则全部）恢复为 `.env`/默认值 |
| GET | `/system/settings/keys` | 无 | 仅返回合法配置 key 列表，供前端/脚本校验 |
| GET | `/system/proxy` | 无 | 当前生效代理（掩码展示，绝不回传密码明文） |
| POST | `/system/proxy/test` | Admin | 用给定代理真实请求若干目标做连通性测试；`proxy_url` 省略=直连对照，`save:true` 时写入全局配置 |
| GET | `/system/dns/diagnose` | 无 | `?host=api.binance.com`，对比本地 DNS 与 DoH 答案，返回 `clean`/`suspicious`/`poisoned` 与行动建议 |
| POST | `/system/providers/reenable-stale` | Admin | 恢复被策略自动停用的源；`{"include_manual": false}` 默认不翻人工禁用 |

网络与 DNS 的完整说明见 **《36 · 网络策略：代理与 DNS 解析》**。

`PATCH /system/providers/{name}` 请求体（`ProviderUpdateRequest`）：

```json
{ "enabled": true, "priority": 10, "locked": false, "reason": "手动下线",
  "proxy": "socks5://user:pass@1.2.3.4:1080" }
```

语义：`enabled` → 启停；`priority` 非空 → 改优先级（默认 `locked=true`）；`locked=false` 且未传 `priority` → 解除锁定；`proxy` → 该数据源**独立代理**（留空字符串表示回到跟随全局代理，落 `providers.proxy` 列，重启后自动还原）。

`POST /system/backfill` 请求体：`{"start_date": "2013-01-01", "end_date": null, "interval": "1d", "reset": false}`，内部调用 `SchedulerManager.run_backfill(start_date, reset)`（`end_date` 当前未被使用）。

调度任务 ID（`scheduler/manager.py`）：`market_tick`、`market_1h`、`market_1d`、`derivatives`、`onchain`、`sentiment`、`macro`、`etf`、`quality`、`gap_repair`、`health_probe`。

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST "http://127.0.0.1:8787/api/v1/system/jobs/market_1d/run"
```

### 配置中心 `/system/settings`

后台可视化配置（用户侧说明见 `docs/10-配置参考与环境变量清单.md` 第 0.2 节）。设计要点：**元数据（`SETTINGS_SPEC`）随代码走，`app_settings` 表只存 `key -> value`**，新增配置项不必改前端、也不必改表结构。

`GET` 返回结构（`categories` 是分类元数据，`groups` 按分类 code 聚合配置项）：

```json
{
  "categories": [ { "code": "smtp", "name": "邮件通知（SMTP）", "order": 20, "help": "…" } ],
  "groups": {
    "smtp": [
      { "key": "SMTP_HOST", "label": "SMTP 服务器", "type": "str", "category": "smtp",
        "sensitive": false, "requires_restart": false, "help": "…", "placeholder": "smtp.qq.com",
        "options": null, "source": "database", "overridden": true, "value": "smtp.qq.com" }
    ]
  },
  "count": 51
}
```

单项字段语义：

| 字段 | 说明 |
| --- | --- |
| `value` | 当前值（已按 `type` 转成 `int`/`float`/`bool`/`str`）。**`sensitive=true` 时恒为空串**，改为回传 `has_value` + `masked`（如 `tes************456`）——只写不读明文。 |
| `source` | `database`（后台改过，优先级最高）｜`env`（来自 `.env`/环境变量）｜`default`（代码默认值）。 |
| `overridden` | `source == "database"`，即这一项是否已被后台覆盖。 |
| `requires_restart` | `true` 的项保存后**必须重启进程**才生效，当前共 3 项：`PORT`、`SCHEDULER_ENABLED`、`SECRET_KEY`。其余**保存即生效**。 |
| `options` | 非空时前端渲染为下拉框（如 `LOG_LEVEL`）。 |

`PUT` 请求体是**扁平的 `key -> value` 映射**，只需提交改过的项；敏感项**留空表示不修改**（不是清空），要清除请用 `reset`：

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X PUT "http://127.0.0.1:8787/api/v1/system/settings" \
  -H "Content-Type: application/json" \
  -d '{"SMTP_HOST": "smtp.qq.com", "SMTP_PASSWORD": "授权码"}'
# -> {"updated": ["SMTP_HOST","SMTP_PASSWORD"], "count": 2, "note": "已保存并立即生效"}
```

保存后会立刻 `apply_to_settings()` 写回运行时 `settings` 单例，所以数据源密钥（由 `ProviderBase.api_key()` 读取，**配置中心优先于 `.env`**）、SMTP、告警阈值等都不用重启。请求体为空、或含未识别的 key 会返回 `validation_error`，且**一个都不写**（`set_many` 先全量校验再落库）。

`POST /system/settings/reset` 删除库里的覆盖值，让它重新跟随 `.env`/默认值。`keys` **必填**（不支持「全部重置」，避免误操作一次清空所有配置）：

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" -X POST "http://127.0.0.1:8787/api/v1/system/settings/reset" \
  -H "Content-Type: application/json" -d '{"keys": ["SMTP_HOST", "SMTP_PASSWORD"]}'
# -> {"removed": ["SMTP_HOST","SMTP_PASSWORD"], "count": 2, "note": "已恢复为跟随 .env / 默认值"}
```

`reset` 内部会重新 `apply_to_settings()`：由于 `SettingsStore` 保存了启动时快照 `_baseline`，被删除的键会被**回填成原来的基线值**而不是留在上一次的修改值上（该行为由 `test_reset_restores_baseline_value` 锁定）。

刻意不纳入配置中心的项：`DATABASE_URL`、`HOST` —— 改错会直接导致服务起不来，只走 `.env`；`SECRET_KEY`/`ADMIN_TOKEN` 虽在配置中心里，但按 `sensitive` 处理（只写不读明文，见 `docs/28-安全与密钥管理.md`）。

## 行情与分析 —— `/api/v1/market`

| 方法 | 路径 | 鉴权 | 关键参数 |
| --- | --- | --- | --- |
| GET | `/market/overview` | 无 | 首页一句话结论 |
| GET | `/market/price` | 无 | `symbol`（默认 `BTC`） |
| GET | `/market/candles` | 无 | `symbol`、`interval`、`limit` 1~5000（默认 365） |
| GET | `/market/indicators` | 无 | `symbol`、`interval`、`limit` 50~5000（默认 500） |
| GET | `/market/analysis` | 无 | 估值 / 周期 / 风险 / 综合状态 |
| GET | `/market/sentiment` | 无 | — |
| GET | `/market/derivatives` | 无 | — |
| GET | `/market/onchain` | 无 | — |
| GET | `/market/macro` | 无 | — |
| GET | `/market/etf` | 无 | — |
| GET | `/market/forecast` | 无 | `horizon` 7~365（默认 30） |
| GET | `/market/timeline` | 无 | `limit` 1~500（默认 50） |
| POST | `/market/plan/{plan_id}/simulate-plan` | Write | 查询串 `start_date`、`end_date`、`validation_mode` |

`/market/candles` 明确优先读本地库：返回中 `source.mode = "local_database"`，并附 `local_coverage` 与历史用过的 Provider 列表。

## 回测与研究 —— `/api/v1/research`

| 方法 | 路径 | 鉴权 | 关键参数 |
| --- | --- | --- | --- |
| POST | `/research/backtest` | Write | 请求体 `BacktestRequest` |
| GET | `/research/backtest/runs` | 无 | `limit` 1~500（默认 50） |
| GET | `/research/strategies` | 无 | 策略清单与参数说明 |
| POST | `/research/replay` | 无 | `{"date": "2021-11-10", "perspective": "then"}` |
| GET | `/research/replay` | 无 | `date`（8~10 字符）、`perspective`（`then`\|`aftermath`） |
| GET | `/research/indicators/dictionary` | 无 | `category` 可选过滤 |
| GET | `/research/indicators/{code}/explain` | 无 | 单指标可追溯解释 |
| GET | `/research/models` | 无 | 模型版本（开发区间 / 测试区间 / 权重 / 失效条件） |
| GET | `/research/signals` | 无 | 信号定义 |

`BacktestRequest` 主要字段（默认值见 `schemas.py`）：`strategy_code=dca_fixed`、`initial_capital`、`monthly_contribution`、`weekly_contribution`、`contribution_frequency=monthly`、`start_date`、`end_date`、`fee_rate=0.001`、`slippage=0.0005`、`max_single_contribution`、`cash_reserve`、`validation_mode=insample`、`oos_split=0.3`。校验规则：费率/滑点必须在 0~0.1；金额不能为负。

## 资金计划与资产 —— `/api/v1/portfolio`

| 方法 | 路径 | 鉴权 | 关键参数 |
| --- | --- | --- | --- |
| GET | `/portfolio/plans` | 无 | — |
| POST | `/portfolio/plans` | Write | 请求体 `PlanRequest` |
| GET | `/portfolio/plans/{plan_id}` | 无 | 不存在时返回 `{"available": false, ...}`（非 404） |
| PATCH | `/portfolio/plans/{plan_id}` | Write | 同 `PlanRequest`，按 `exclude_unset` 更新 |
| DELETE | `/portfolio/plans/{plan_id}` | Write | 返回 `{"deleted": bool}` |
| GET | `/portfolio/plans/{plan_id}/next` | 无 | 下次投入时间与建议金额 |
| GET | `/portfolio/plans/{plan_id}/snapshot` | Write | 生成资产快照 |
| GET | `/portfolio/strategies` | 无 | 可用定投策略 |
| GET | `/portfolio/transactions` | 无 | `plan_id`、`limit` 1~2000（默认 500） |
| POST | `/portfolio/transactions/{plan_id}` | Write | `TransactionRequest`（`price` 必须 > 0） |
| DELETE | `/portfolio/transactions/{tx_id}` | Write | 返回 `{"deleted": bool}` |
| GET | `/portfolio/holdings` | 无 | `plan_id` 可选 |

## AI 解释助手 —— `/api/v1/assistant`

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| POST | `/assistant/ask` | 无 | `{"question": "...", "context_module": null}`；`question` 长度 2~1000 |
| GET | `/assistant/indicator-help/{code}` | 无 | 指标通俗解释 |

未配置 `OPENAI_API_KEY` 或 `AI_ASSISTANT_ENABLED=false` 时，返回 `mode="rule_based"` 并附 `note` 说明「使用规则解释引擎」；配置后走 LLM，请求 `OPENAI_BASE_URL/chat/completions`，异常时自动降级到规则引擎。

## 已知局限 / 注意事项

- 生产环境写操作（`WriteDep`）只判断 `X-Admin-Token` 是否存在，不校验值是否与 `ADMIN_TOKEN` 一致，任何非空值均可通过；这是当前实现的实际行为，需要更强鉴权时应修改 `deps.py`。
- 前端 `core.js` 的请求封装只发送 `Accept`/`Content-Type`/`X-Request-ID`，**不发送** `X-Admin-Token`；生产环境下从页面触发管理类接口会被 401/403 拒绝，需用 `curl` 或自行补充头。
- 前端「数据源开关」提交的是 `{"action": "enable"|"disable"|"unlock"}`，而 `ProviderUpdateRequest` 只有 `enabled`/`priority`/`locked`/`reason`，pydantic 默认忽略多余字段，因此该 UI 操作不会真正改变 Provider 状态。
- `PATCH /system/providers/{name}` 与 `POST /system/backfill` 的修改只写入数据库/注册表，进程重启后未配置 `PROVIDER_OVERRIDES` 时以注册表默认顺序为准。
- 所有响应为扁平结构，不含 `data` 信封；`platformctl status` 中按 `data.summary` 解析的分支因此不会输出（详见「30 · 运维命令参考」）。
- `POST /system/backfill` 的 `end_date` 字段已在 `BackfillRequest` 中定义但未参与执行。

> 本文只对代码事实负责；如代码变更后本文过期，请以代码为准。
