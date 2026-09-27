# -*- coding: utf-8 -*-
"""智能监测与条件预警模块。

组成：
    catalog.py    可监测指标目录（新增指标只需在这里登记一条）
    resolvers.py  指标取值实现（只消费已有数据，不新建数据源）
    engine.py     通用条件规则引擎（AND/OR/NOT 嵌套、变化率、区间、持续、连续、交叉、分位、状态变化）
    rules.py      规则 CRUD 与校验
    notifier.py   通知抽象（EmailProvider 已实现，Telegram/Webhook 预留）
    scheduler.py  与现有 SchedulerManager 的集成入口

设计原则（与主项目一致）：
* 增量：不改动任何已有表、API 行为与技术栈。
* 复用：数据来自采集层，指标来自 engines，状态来自 valuation/cycle/risk/regime。
* 诚实：取不到数据就不触发，并在事件里写明原因；绝不用假数据或旧数据凑出一次「提醒」。
"""

from __future__ import annotations

# 关键：导入 catalog 之后必须导入 resolvers，否则指标只有「定义」没有「实现」，
# 规则会在运行期才发现指标不可用。放在包初始化里保证任何入口都会完成注册。
from . import catalog, engine, notifier, resolvers, rules, templates  # noqa: F401

__all__ = ["catalog", "resolvers", "engine", "rules", "notifier", "pipeline", "templates"]
