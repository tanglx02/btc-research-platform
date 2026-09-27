# 08 · 存储层：Raw 与 Normalized 双存储及幂等写入

本文描述数据落库的两层结构（原始留档 / 规范化表）、幂等 upsert 的实现与作用、
原始留档的清理策略，以及全系统统一的时间戳口径。
结论均来自源码，行号以当前代码为准。

## 0. 双存储的设计动因

`backend/app/db/models.py:7-9` 的模型文件头写明：

> Raw 数据与 Normalized 数据双存储：`raw_*` 表保留原始响应，
> Provider 停用或更换后可重新解析历史。

配合 `backend/app/collectors/market_collector.py:5` 的原则：
「抓一次，永久保存在自己的数据库；以后看历史只读本地，不再依赖第三方。」

| 层 | 表 | 定位 | 是否参与计算 |
|---|---|---|---|
| Raw | `raw_market_data` | 第三方返回的**原文**留档，用于追源与重新解析 | 否 |
| Normalized | `candles` / `market_prices` / `onchain_metrics` / … | 结构化后的业务数据 | 是 |

## 1. Raw 层：`raw_market_data`

模型：`backend/app/db/models.py:157-171`（类名 `RawMarketData`）。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | PK_BIGINT 自增 | 主键 |
| `provider` | String(64)，index | 数据源名称 |
| `endpoint` | String(255) | 逻辑端点，如 `ohlcv:1d`、`price` |
| `category` | String(64)，index | 数据类别，如 `ohlcv` / `market_price` |
| `request_params` | JSON | 请求参数（如 `start` / `end` / `interval`） |
| `payload` | Text | 原始响应正文 |
| `http_status` | Integer | HTTP 状态码 |
| `latency_ms` | Float | 请求耗时 |
| `unit` | String(32) | 单位（当前写入路径未赋值） |
| `version` | String(32) | 版本标记（当前写入路径未赋值） |
| `fetch_time` | DateTime(tz)，index | 抓取时间，清理任务按此字段判断 |

写入函数 `insert_raw`：`backend/app/db/repo.py:118-148`。

| 行为 | 行号 | 说明 |
|---|---|---|
| 序列化 | 133 | 非字符串用 `json.dumps(..., ensure_ascii=False, default=str)` |
| 截断 | 140 | `payload[:200000]`，单条留档上限 20 万字符 |
| 失败安全 | 146-148 | 异常时 `logger.event("db.raw_insert_failed")` + `rollback`，**绝不中断业务写入** |

### 1.1 谁会写 Raw（如实说明覆盖范围）

全仓库 `insert_raw` 的调用点只有两处，均在行情采集器：

| 调用点 | 场景 |
|---|---|
| `backend/app/collectors/market_collector.py:92` | 实时 tick（`collect_tick`），留档 `result.trace.raw_payload`，无 trace 时退化为 `{"price": price}` |
| `backend/app/collectors/market_collector.py:295` | 回填/增量（`_fetch_and_store`），留档 `c.to_list()` 列表，最多 2000 根 |

即：**链上、衍生品、情绪、宏观、ETF 这几类采集器（见 `backend/app/collectors/data_collectors.py`）只调用 `upsert_rows`，不写 Raw 留档。**
因此「浏览器可见即可追源到原始 JSON」对这几类数据**未实现**（详见 `12-问答助手与可追溯设计.md`）。

## 2. Normalized 层：规范化表

### 2.1 公共可追溯字段（`SourceMixin`）

`backend/app/db/models.py:46-55`，被 `MarketPrice` / `Candle` / `OnchainMetric` / `ExchangeFlow` / `EtfFlow` 继承。

| 字段 | 默认 | 语义 |
|---|---|---|
| `source_id` | 必填，index | 数据源 Provider 名称 |
| `quality_status` | `SINGLE_SOURCE` | 质量标记（单源 / 已交叉验证 / stale 等） |
| `observation_time` | None，index | **数据本身的时间** |
| `fetch_time` | `utcnow`，index | **抓取时间** |
| `created_at` | `utcnow` | 入库时间 |
| `updated_at` | `utcnow`，`onupdate` | 更新时间 |

`observation_time` 与 `fetch_time` 严格分离是防未来数据泄漏与 stale 判定的基础；
宏观序列更进一步区分 `observation_ts` 与 `release_ts`（`models.py:290-309`）。

### 2.2 主要规范化表与唯一约束

| 表 | 唯一约束 | 出处 |
|---|---|---|
| `candles` | `("symbol","interval","ts","source_id")` → `uq_candle` | `models.py:187-192` |
| `market_prices` | `("symbol","source_id","observation_time")` → `uq_market_price` | `models.py:174-179` |
| `onchain_metrics` | `("metric","symbol","observation_ts","source_id")` | `models.py:221-226` |
| `exchange_flows` | `("metric","observation_ts","source_id")` | `models.py:235-237` |
| `etf_flows` | `("issuer","observation_ts","source_id")` | `models.py:244-246` |
| `macro_series` | `("series_id","observation_ts","source_id")` | `models.py:294-297` |
| `sentiment` | `("metric","observation_ts","source_id")` | `models.py:312-314` |
| `indicator_values` | `("code","symbol","interval","ts")` | `models.py:353-358` |

所有唯一约束都把 `source_id` 包含在内 —— 同一观测点可以来自不同 Provider 各存一行，
既不互相覆盖，也为交叉验证保留了原始分歧（见 `12-问答助手与可追溯设计.md` 的 cross validation 部分）。

### 2.3 主键类型的方言适配

`models.py:37-39`：

```python
PK_BIGINT = BigInteger().with_variant(Integer, "sqlite")
```

注释给出原因：SQLite 只对 `INTEGER PRIMARY KEY` 自动生成 rowid，
写成 BIGINT 会导致自增失效（NOT NULL 约束失败）。PostgreSQL 侧保持 `BIGSERIAL`。

## 3. 为什么「本地历史优先、原则上不再请求第三方」

| 证据 | 位置 |
|---|---|
| 函数 docstring：「读取本地历史 K 线 —— 查看历史优先读自己的数据库，不依赖第三方」 | `repo.py:198` |
| `MarketService.candles` 只读库，返回的 `source.mode = "local_database"` 并附本地覆盖范围 | `services/market_service.py:97-119` |
| `MarketService.indicators` 从本地 `latest_candles` 计算 | `services/market_service.py:122-130` |
| `latest_price` 的 docstring：「断网降级运行的基础」 | `repo.py:169` |
| 回填原则注释 | `collectors/market_collector.py:4-6` |

只有「刷新」（tick / incremental / backfill）会访问第三方；
**展示历史、计算指标、跑回测、做回放，全部只读本地库。**

## 4. 幂等写入：实现与作用

### 4.1 方言适配的 INSERT 构造器

`repo.py:24-31`：

```python
def dialect_insert(session, model):
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        return pg_insert(model)
    return sqlite_insert(model)
```

一份代码同时支持 SQLite 与 PostgreSQL 的 `ON CONFLICT` 语法。

### 4.2 `upsert_candles`

`repo.py:34-57`。

| 要素 | 值 | 行号 |
|---|---|---|
| 冲突列 | `["symbol","interval","ts","source_id"]`，与 `uq_candle` 完全一致 | 51 |
| 更新列 | `open, high, low, close, volume, quote_volume, trades, fetch_time, updated_at, quality_status` | 43-45 |
| 分批 | `CHUNK = 500`（`repo.py:21`） | 47-48 |
| 提交 | 所有批次写完后统一 `commit` | 56 |

注释（`repo.py:36-38`）强调冲突目标**必须与模型 UniqueConstraint 逐字一致**，
因此用列名 `source_id` 而不是 `provider`。

### 4.3 `upsert_rows`（通用幂等写入）

`repo.py:60-115`。这是链上/衍生品/情绪/宏观/ETF 采集器使用的入口。

关键作用写在 docstring（`repo.py:68-70`）：

> 采集任务会被调度器反复执行（例如每小时一次），同一个观测点可能被重复抓取，
> 因此所有批量写入都必须是 upsert 而不是 insert —— 否则第二次运行直接撞唯一约束而失败。

实现要点：

| 机制 | 行号 | 说明 |
|---|---|---|
| 冲突列过滤 | 79-80 | 用 `model.__table__.columns.keys()` 求交集，剔除模型里不存在的列 |
| 更新列过滤 | 81 | 剔除冲突列本身，以及模型里不存在的列 |
| **逐行剔除未知键** | 92-97 | 对每一行做 `{k: v for k, v in row.items() if k in existing}` |
| 丢弃留痕 | 86-87、99-100 | `db.upsert_conflict_key_missing` / `db.upsert_dropped_columns` 两个事件，最多列 20 个列名 |
| 无更新列时退化 | 104-106 | `on_conflict_do_nothing`，同样保证幂等 |
| 空行保护 | 101-102 | `if not clean_rows or not clean_rows[0]: continue` |

**为什么要过滤不存在的列**（注释在 `repo.py:76-78` 与 `90-91`）：

- 冲突列/更新列里有模型不存在的字段，会让整批 SQL 直接报错；
- 单行多出一个厂商新增字段，不该让整批历史数据写不进去。

这正是需求中要求如实写入的事实：`upsert_rows` 会过滤模型不存在的列以避免整批采集被打挂。
代价是**这些字段被静默丢弃**（只在日志里留痕），调用方不会收到异常。

### 4.4 `upsert_market_price`

`repo.py:151-165`。冲突列 `["symbol","source_id","observation_time"]`，
更新 `price / fetch_time / updated_at / quality_status / cross_validation / confidence`。

注意它更新了 `updated_at`，但**没有**把它放进 `set_` 之外的位置 —— 即重复写入同一观测点会刷新 `updated_at`，
而 `created_at` 保持首次入库时间。

### 4.5 幂等保证了什么、不保证什么（如实说明）

| 保证 | 说明 |
|---|---|
| ✅ 不产生重复行 | 唯一约束 + `ON CONFLICT` |
| ✅ 重复同步不报错 | 第二次运行不会撞唯一约束而整批失败 |
| ✅ 后抓到的值覆盖先抓到的值 | `on_conflict_do_update` |
| ❌ **不保证计数不重复** | `BaseCollector.save_checkpoint` 的 `total_rows += max(0, rows_delta)`（`collectors/base.py:105`），重复同步会让 `total_rows` 持续累加 |

即：幂等消除的是**数据行的重复**，不是**统计口径的重复计数**。
`sync_checkpoints.total_rows` 应理解为「累计写入行数（次）」，而不是「库里实际有多少行」。

## 5. Raw 留档的清理策略

### 5.1 清理函数

`repo.py:242-249`：

```python
cutoff = datetime.now(timezone.utc) - timedelta(days=days)
stmt = delete(RawMarketData).where(RawMarketData.fetch_time < cutoff)
```

按 `fetch_time`（抓取时间）而非 `observation_time` 判断，返回删除行数。

### 5.2 调度任务

`backend/app/scheduler/manager.py:83-88`：

| 属性 | 值 |
|---|---|
| 任务 id | `raw_retention` |
| 优先级 | 12 |
| 周期 | `max(3600, JOB_RETENTION_SECONDS)`，默认 `JOB_RETENTION_SECONDS=86400`（每天一次） |
| 保留天数 | `RAW_RETENTION_DAYS`，默认 **3650**（`core/config.py:55` 注释「原始数据保留 10 年」） |

任务实现 `SchedulerManager._purge_raw`：`scheduler/manager.py:90-101`，
异常时记 `scheduler.retention_failed` 并返回 `ok=False`，**不拖垮调度器**。

### 5.3 清理边界（注释原文，`scheduler/manager.py:83-84`）

> 原始响应留档表会持续增长，必须定期清理；清理的只是 raw 原始报文，
> 标准化后的历史数据（candles 等）永不删除。

即：删 `raw_market_data` 不影响任何规范化表，历史 K 线、指标、回测结果都不会丢。

## 6. 时区与时间戳口径统一

### 6.1 三条统一约定

| 约定 | 实现 | 出处 |
|---|---|---|
| 入库时间一律 UTC | `models.py:33-34` 的 `utcnow()` 返回 `datetime.now(timezone.utc)` | 全表 `DateTime(timezone=True)` |
| 时间戳字段一律 UTC 秒 | `Candle.ts` 注释「开盘时间，UTC 秒」（`models.py:195`） | `market_collector.py:285` 用 `datetime.fromtimestamp(c.ts, tz=timezone.utc)` 反解 |
| 展示层再转本地时区 | `core/timeutils.py:2` 注释「所有入库时间统一 UTC，展示层再转本地时区」 | `fmt_local`（CLI 使用，`btcctl.py:158`） |

`TIMEZONE` 配置项（默认 `Asia/Shanghai`）目前**仅在 `/api/v1/system/info` 中原样返回**
（`api/system.py:61`），没有参与任何时间转换计算 —— 前端与 CLI 自行按浏览器/系统本地时区渲染。

### 6.2 桶（bucket）取整

所有「按周期对齐」的地方都用同一个公式：`ts // step * step`。

| 位置 | 行号 | 用途 |
|---|---|---|
| `GapRepairCollector.find_gaps` | `market_collector.py:348` | 把已有 K 线归一化到桶，再找缺失桶 |
| `GapRepairCollector.repair` | `market_collector.py:364-365` | 由桶时间戳还原窗口 `[gap_ts, gap_ts+step)` |
| `DataQualityScanner.scan` | `market_collector.py:430`、434 | 计算期望桶数与当日桶 |

`step` 来自 `INTERVAL_SECONDS`（`market_collector.py:30-34`），未登记周期兜底 86400。
取整方式为**向下取整到周期起点**，因此 1d 的桶起点恒为 UTC 00:00。

### 6.3 时钟来源

| 场景 | 来源 |
|---|---|
| 抓取窗口起点 | `datetime.strptime(..., "%Y-%m-%d").replace(tzinfo=timezone.utc)`（`market_collector.py:147`） |
| 抓取窗口终点 | `datetime.now(timezone.utc)`（`market_collector.py:154`） |
| 行级 `fetch_time` / `observation_time` | `market_collector.py:285-286` |
| 断点 `cursor_ts` | `int(cursor_dt.timestamp())`（`market_collector.py:207`） |

## 7. 连接层对存储的影响

`backend/app/db/base.py`：

| 机制 | 行号 | 说明 |
|---|---|---|
| SQLite WAL | 63-71 | `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=30000`、`foreign_keys=ON` |
| SQLite 连接参数 | 62 | `check_same_thread=False`、`timeout=30` |
| PostgreSQL 连接池 | 74-80 | `pool_size=DB_POOL_SIZE`、`max_overflow=DB_MAX_OVERFLOW`、`pool_pre_ping=True`、`pool_recycle=3600` |
| 会话 | 88-93 | `expire_on_commit=False`、`autoflush=False` |
| URL 口令脱敏 | 106-116 | `redact_url` 用于日志，避免口令进日志 |

WAL 模式直接影响备份正确性：`scripts/backup.py:134-151` 明确把 `-wal` 与 `-shm` 一并打包，
注释说明「只复制主库而不复制 WAL 会丢掉尚未 checkpoint 的写入，恢复后数据倒退」。

## 8. 写入链路一览

| 采集器 | 写入函数 | 是否写 Raw |
|---|---|---|
| `MarketCollector.collect_tick` | `upsert_market_price` + `insert_raw` | ✅ |
| `MarketCollector.backfill` / `sync_incremental` | `upsert_candles` + `insert_raw` | ✅ |
| `GapRepairCollector.repair` | `upsert_candles` | ❌ |
| `OnchainCollector` 等（见 `data_collectors.py:117/143/199`） | `upsert_rows` | ❌ |
| `DataQualityScanner.scan` | `session.add(DataQuality)` 后 `commit` | ❌（且每次扫描新增一行，非 upsert） |

## 9. 已知边界与代码不一致（未修改，仅记录）

| # | 位置 | 现象 |
|---|---|---|
| 1 | `data_collectors.py` 全文件 | 非行情类采集器不写 Raw 留档，其数据无法在界面上追源到原始 JSON |
| 2 | `repo.py:92-100` | 未知列被静默丢弃，只在日志留痕；无配置项可改为「严格模式直接报错」 |
| 3 | `collectors/base.py:105` | `total_rows` 累加，重复同步会重复计数 |
| 4 | `models.py:169-170` | `raw_market_data.unit` / `version` 两个字段已建列但写入路径从未赋值 |
| 5 | `core/config.py:41` | `TIMEZONE` 只用于 `/system/info` 回显，未参与任何时间转换 |
| 6 | `repo.py:113` | `written += len(clean_rows)` 统计的是**提交行数**，不是「实际新增/更新行数」，无法据此判断本轮是否真有新数据 |

> 本文只对代码事实负责；如代码变更后本文过期，请以代码为准。