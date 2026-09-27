# -*- coding: utf-8 -*-
"""规则的读写、校验与版本化。

设计要点（对应需求第三十六、三十八、四十一条）：

* 规则是一棵「树」：Rule 挂若干个 ConditionGroup，Group 里又能挂 Group 或 Condition。
  因此 `(A AND B) OR (C AND D AND E)` 这种结构是原生支持的，不是靠字符串拼出来的。
* 校验必须发生在**写库之前**，并且要能明确告诉用户「哪个字段、错在哪」，
  而不是写完再报 500。
* 每次实质修改都会留一份快照（alert_rule_versions），
  这样历史触发记录永远能对应到「当时那条规则长什么样」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.models import (
    AlertCondition,
    AlertConditionGroup,
    AlertRule,
    AlertRuleVersion,
)
from .catalog import CATALOG, get_spec, has_metric
from .engine import (
    CHANGE_WINDOWS,
    QUALITY_CHOICES,
    VALID_OPERATORS,
    Condition,
    ConditionGroup,
)

# 规则级别的枚举
SEVERITIES = ["INFO", "WARNING", "HIGH", "CRITICAL"]
SEVERITY_CN = {"INFO": "提示", "WARNING": "注意", "HIGH": "重要", "CRITICAL": "紧急"}
RULE_STATES = ["NORMAL", "TRIGGERED", "COOLDOWN", "RECOVERED"]

# 频率下限：防止用户填 1 秒把数据库打爆（需求三十八「过高检测频率」）
MIN_CHECK_INTERVAL_SECONDS = 30
MAX_CHECK_INTERVAL_SECONDS = 86400 * 7
# 冷却下限同理：防止「重复邮件 / 邮件风暴」
MIN_COOLDOWN_SECONDS = 60
MAX_CONDITIONS_PER_RULE = 50


class RuleValidationError(ValueError):
    """规则的校验错误。message 是给人看的，field 用于前端定位。"""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field

    def to_dict(self) -> dict[str, Any]:
        return {"message": self.message, "field": self.field}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any, default: int = 0) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ 校验

def validate_condition(raw: dict[str, Any], *, index: int = 0) -> dict[str, Any]:
    """校验并归一化单个条件。返回干净的可写入字典。

    覆盖需求三十八里提到的：不存在指标、错误阈值、单位错误、时间范围错误、
    缺少必要参数等。
    """
    where = f"第 {index + 1} 个条件"

    code = str(raw.get("metric_code") or "").strip()
    if not code:
        raise RuleValidationError(f"{where}：没有选择监测对象（metric_code 为空）", "metric_code")
    if not has_metric(code):
        raise RuleValidationError(
            f"{where}：指标「{code}」不在指标目录中。请先在指标目录里登记，再用于规则。",
            "metric_code",
        )

    operator = str(raw.get("operator") or "").strip()
    if operator not in VALID_OPERATORS:
        raise RuleValidationError(
            f"{where}：不支持的条件类型「{operator}」。可选：{'、'.join(sorted(VALID_OPERATORS))}",
            "operator",
        )

    spec = get_spec(code)
    meta = VALID_OPERATORS[operator]
    arity = meta.get("arity", 1)

    threshold = _as_float(raw.get("threshold"))
    threshold_high = _as_float(raw.get("threshold_high"))
    compare_metric = (raw.get("compare_metric") or "").strip() or None
    change_window = (raw.get("change_window") or "").strip() or None
    percentile_window_days = raw.get("percentile_window_days")
    expected_state = (raw.get("expected_state") or "").strip() or None

    # --- 按运算符校验必备参数
    if arity == 1 and not meta.get("needs_compare"):
        if operator != "state_change" and threshold is None:
            raise RuleValidationError(f"{where}：缺少阈值。", "threshold")

    if arity == 2:
        if threshold is None or threshold_high is None:
            raise RuleValidationError(f"{where}：区间条件必须同时给出下界和上界。", "threshold")
        if threshold == threshold_high:
            raise RuleValidationError(f"{where}：区间上下界不能相同，否则条件永远不成立。", "threshold_high")

    if meta.get("needs_compare"):
        if not compare_metric and threshold is None:
            raise RuleValidationError(
                f"{where}：跨越类条件需要指定「被跨越的指标」或一个固定数值。", "compare_metric"
            )
        if compare_metric and not has_metric(compare_metric):
            raise RuleValidationError(
                f"{where}：被比较的指标「{compare_metric}」不在指标目录中。", "compare_metric"
            )
        if compare_metric and compare_metric == code:
            raise RuleValidationError(f"{where}：不能拿自己和自己做交叉比较。", "compare_metric")

    if meta.get("needs_window"):
        if not change_window:
            raise RuleValidationError(
                f"{where}：变化率条件必须指定时间窗口，可选：{'、'.join(CHANGE_WINDOWS)}", "change_window"
            )
        if change_window not in CHANGE_WINDOWS:
            raise RuleValidationError(
                f"{where}：变化率窗口「{change_window}」不支持。可选：{'、'.join(CHANGE_WINDOWS)}",
                "change_window",
            )
        if not spec.supports_change:
            raise RuleValidationError(
                f"{where}：指标「{spec.name_cn}」不支持变化率条件（它没有可比的历史序列）。",
                "metric_code",
            )

    if meta.get("needs_state"):
        if not expected_state:
            raise RuleValidationError(f"{where}：状态类条件必须指定目标状态值。", "expected_state")

    if operator in ("percentile_gt", "percentile_lt"):
        if threshold is None:
            raise RuleValidationError(f"{where}：分位条件必须给出百分位阈值。", "threshold")
        if not 0 <= threshold <= 100:
            raise RuleValidationError(
                f"{where}：历史分位阈值必须在 0~100 之间，当前为 {threshold}。", "threshold"
            )
        if percentile_window_days is not None:
            days = _as_int(percentile_window_days, 0)
            if days <= 0:
                raise RuleValidationError(f"{where}：分位回看天数必须大于 0。", "percentile_window_days")
            if days > 4000:
                raise RuleValidationError(
                    f"{where}：分位回看天数过大（{days} 天），最多支持 4000 天。", "percentile_window_days"
                )

    # --- 单位/量纲提醒：明显不合理的阈值直接拦掉，避免「价格 > 100000000」
    # 注意：只有「价格本身」才要求为正。资金流（etf_netflow / exchange_netflow）
    # 的负数是有意义的（净流出），不能被当成「填错了」。
    _PRICE_LIKE = {"price", "close", "ma20", "ma50", "ma200", "ema50",
                   "bb_upper", "bb_lower", "ath"}
    if threshold is not None and operator in ("gt", "gte", "lt", "lte"):
        if spec.unit == "USD" and code in _PRICE_LIKE:
            if threshold <= 0:
                raise RuleValidationError(
                    f"{where}：价格类阈值为 {threshold}，美元价格必须大于 0。", "threshold"
                )
            if threshold > 100_000_000:
                raise RuleValidationError(
                    f"{where}：阈值为 {threshold}，对美元价格来说明显不合理（单位可能填错了）。",
                    "threshold",
                )

    if threshold is not None and spec.unit == "%" and abs(threshold) > 100_000:
        raise RuleValidationError(
            f"{where}：百分比类阈值为 {threshold}%，量级明显异常（单位可能填错了）。", "threshold"
        )

    # --- 时间修饰
    duration_seconds = _as_int(raw.get("duration_seconds"), 0)
    if duration_seconds < 0:
        raise RuleValidationError(f"{where}：持续时间不能为负数。", "duration_seconds")
    if duration_seconds > 86400 * 30:
        raise RuleValidationError(
            f"{where}：持续时间过长（{duration_seconds} 秒），最多 30 天。", "duration_seconds"
        )

    consecutive_count = _as_int(raw.get("consecutive_count"), 1)
    if consecutive_count < 1:
        raise RuleValidationError(f"{where}：连续次数至少为 1。", "consecutive_count")
    if consecutive_count > 1000:
        raise RuleValidationError(f"{where}：连续次数过大（{consecutive_count}），最多 1000。", "consecutive_count")

    min_quality = (raw.get("min_quality") or "").strip().upper() or None
    if min_quality and min_quality not in QUALITY_CHOICES + ["MISSING", "STALE"]:
        raise RuleValidationError(
            f"{where}：数据质量要求「{min_quality}」不合法。可选：{'、'.join(QUALITY_CHOICES)}",
            "min_quality",
        )

    return {
        "metric_code": code,
        "operator": operator,
        "threshold": threshold,
        "threshold_high": threshold_high,
        "compare_metric": compare_metric,
        "change_window": change_window,
        "percentile_window_days": _as_int(percentile_window_days, 0) or None,
        "expected_state": expected_state,
        "duration_seconds": duration_seconds,
        "consecutive_count": consecutive_count,
        "min_quality": min_quality,
        "position": _as_int(raw.get("position"), 0),
    }


def validate_group(raw: dict[str, Any], *, index: int = 0, depth: int = 0) -> dict[str, Any]:
    """递归校验一个条件分组。"""
    if depth > 5:
        raise RuleValidationError("条件分组嵌套超过 5 层，请简化规则结构。", "groups")

    where = f"第 {index + 1} 个分组"
    operator = str(raw.get("operator") or "AND").strip().upper()
    if operator not in ("AND", "OR", "NOT"):
        raise RuleValidationError(f"{where}：组合方式「{operator}」不支持，只能是 AND / OR / NOT。", "operator")

    conditions = [validate_condition(c, index=i) for i, c in enumerate(raw.get("conditions") or [])]
    groups = [
        validate_group(g, index=i, depth=depth + 1) for i, g in enumerate(raw.get("groups") or [])
    ]

    if not conditions and not groups:
        raise RuleValidationError(f"{where}：分组里没有任何条件。", "conditions")

    return {
        "operator": operator,
        "label": (raw.get("label") or "").strip() or None,
        "position": _as_int(raw.get("position"), 0),
        "conditions": conditions,
        "groups": groups,
    }


def validate_rule(payload: dict[str, Any], *, existing: AlertRule | None = None) -> dict[str, Any]:
    """校验整条规则的请求体。返回归一化后的字段。

    需求三十八要求的全部校验项都在这里落地。
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise RuleValidationError("规则名称不能为空。", "name")
    if len(name) > 128:
        raise RuleValidationError("规则名称过长（最多 128 个字符）。", "name")

    logic = str(payload.get("logic") or "AND").strip().upper()
    if logic not in ("AND", "OR"):
        raise RuleValidationError("根层组合方式只能是 AND 或 OR。", "logic")

    severity = str(payload.get("severity") or "WARNING").strip().upper()
    if severity not in SEVERITIES:
        raise RuleValidationError(
            f"提醒等级「{severity}」不支持。可选：{'、'.join(SEVERITIES)}", "severity"
        )

    # 默认 SINGLE_SOURCE 而非 VERIFIED：
    # 单源真实数据本身就是可信数据（来源与质量都会被如实记录进事件），
    # 而 VERIFIED 需要同一时刻有 2 个以上数据源同时应答 —— 这在真实网络里并不总能满足。
    # 若默认 VERIFIED，用户按默认建的第一条规则会「条件成立却永远收不到邮件」。
    # 「重要规则要求 ≥2 源确认」是可选增强（require_multi_source），不是默认门槛。
    # 真正必须默认守住的是新鲜度：require_fresh 默认 True，过期数据一律不触发。
    min_quality = str(payload.get("min_quality") or "SINGLE_SOURCE").strip().upper()
    if min_quality not in QUALITY_CHOICES:
        raise RuleValidationError(
            f"最低数据质量「{min_quality}」不支持。可选：{'、'.join(QUALITY_CHOICES)}", "min_quality"
        )

    check_interval = _as_int(payload.get("check_interval_seconds"), 60)
    if check_interval < MIN_CHECK_INTERVAL_SECONDS:
        raise RuleValidationError(
            f"检测周期过短（{check_interval} 秒）。最短 {MIN_CHECK_INTERVAL_SECONDS} 秒，"
            "过于频繁会拖垮数据库而且没有实际意义。",
            "check_interval_seconds",
        )
    if check_interval > MAX_CHECK_INTERVAL_SECONDS:
        raise RuleValidationError(
            f"检测周期过长（{check_interval} 秒），最多 7 天。", "check_interval_seconds"
        )

    cooldown = _as_int(payload.get("cooldown_seconds"), 86400)
    if cooldown < MIN_COOLDOWN_SECONDS:
        raise RuleValidationError(
            f"冷却时间过短（{cooldown} 秒）。最短 {MIN_COOLDOWN_SECONDS} 秒，否则会造成重复邮件。",
            "cooldown_seconds",
        )
    if cooldown > 86400 * 365:
        raise RuleValidationError("冷却时间过长（最多 365 天）。", "cooldown_seconds")

    # 条件树
    raw_groups = payload.get("groups")
    if raw_groups is None:
        # 兼容「只有一个根条件、没有分组」的简单写法
        if payload.get("condition"):
            raw_groups = [{"operator": "AND", "conditions": [payload["condition"]]}]
        else:
            raw_groups = []

    groups = [validate_group(g, index=i) for i, g in enumerate(raw_groups)]

    total = _count_conditions(groups)
    if total == 0:
        raise RuleValidationError("规则至少要有一个条件，否则它永远不会触发。", "groups")
    if total > MAX_CONDITIONS_PER_RULE:
        raise RuleValidationError(
            f"单条规则的条件数过多（{total} 个），最多 {MAX_CONDITIONS_PER_RULE} 个。", "groups"
        )

    # 重复条件检测：完全相同的条件没有意义，通常是误操作
    seen: set[tuple] = set()
    for c in _iter_conditions(groups):
        key = (
            c["metric_code"], c["operator"], c["threshold"], c["threshold_high"],
            c["compare_metric"], c["change_window"], c["expected_state"],
        )
        if key in seen:
            spec = get_spec(c["metric_code"])
            raise RuleValidationError(
                f"存在完全重复的条件：「{spec.name_cn if spec else c['metric_code']}」。"
                "请删除多余的那一条。",
                "groups",
            )
        seen.add(key)

    # 逻辑上永远为假的组合：AND 里同时出现 x>10 与 x<5，基本可以断定是填错了
    for g in groups:
        _check_contradiction(g)

    return {
        "name": name,
        "description": (payload.get("description") or "").strip() or None,
        "logic": logic,
        "severity": severity,
        "min_quality": min_quality,
        "require_fresh": bool(payload.get("require_fresh", True)),
        "require_multi_source": bool(payload.get("require_multi_source", False)),
        "notify_on_recover": bool(payload.get("notify_on_recover", False)),
        "check_interval_seconds": check_interval,
        "cooldown_seconds": cooldown,
        "enabled": bool(payload.get("enabled", True)),
        "paused": bool(payload.get("paused", False)),
        "plan_id": _as_int(payload.get("plan_id"), 0) or None,
        "template_code": (payload.get("template_code") or "").strip() or None,
        "groups": groups,
    }


def _count_conditions(groups: list[dict[str, Any]]) -> int:
    total = 0
    for g in groups:
        total += len(g["conditions"])
        total += _count_conditions(g["groups"])
    return total


def _iter_conditions(groups: list[dict[str, Any]]):
    for g in groups:
        yield from g["conditions"]
        yield from _iter_conditions(g["groups"])


def _check_contradiction(group: dict[str, Any]) -> None:
    """同一 AND 分组内，同一个指标的下界比上界还高 —— 永远不成立。"""
    if group["operator"] == "AND":
        bounds: dict[str, list[tuple[float, str]]] = {}
        for c in group["conditions"]:
            if c["operator"] in ("gt", "gte", "lt", "lte") and c["threshold"] is not None:
                bounds.setdefault(c["metric_code"], []).append(
                    (c["threshold"], "lower" if c["operator"] in ("gt", "gte") else "upper")
                )
        for code, items in bounds.items():
            lowers = [v for v, kind in items if kind == "lower"]
            uppers = [v for v, kind in items if kind == "upper"]
            if lowers and uppers and min(lowers) >= max(uppers):
                spec = get_spec(code)
                raise RuleValidationError(
                    f"条件互相矛盾：「{spec.name_cn if spec else code}」的下界（{min(lowers)}）"
                    f"不低于上界（{max(uppers)}），这个组合永远不会成立。",
                    "groups",
                )
    for sub in group["groups"]:
        _check_contradiction(sub)


# ------------------------------------------------------------------ 读写

def _to_condition(raw: dict[str, Any], rule_id: int, group_id: int | None) -> AlertCondition:
    return AlertCondition(rule_id=rule_id, group_id=group_id, **raw)


async def create_rule(session: AsyncSession, payload: dict[str, Any]) -> AlertRule:
    """新建规则（含条件树），并写入 v1 快照。"""
    data = validate_rule(payload)
    groups_data = data.pop("groups")

    rule = AlertRule(**data, version=1, state="NORMAL")
    session.add(rule)
    await session.flush()  # 需要拿到 rule.id

    await _write_groups(session, rule.id, groups_data)

    await session.flush()
    await _snapshot(session, rule, note="创建规则")
    await session.flush()
    return rule


async def update_rule(session: AsyncSession, rule_id: int, payload: dict[str, Any]) -> AlertRule:
    """更新规则。条件树用「整体替换」的方式写，避免增量 diff 出隐性 bug。"""
    rule = await get_rule(session, rule_id)
    if rule is None:
        raise RuleValidationError(f"规则 {rule_id} 不存在。", "id")

    merged = {
        "name": payload.get("name", rule.name),
        "description": payload.get("description", rule.description),
        "logic": payload.get("logic", rule.logic),
        "severity": payload.get("severity", rule.severity),
        "min_quality": payload.get("min_quality", rule.min_quality),
        "require_fresh": payload.get("require_fresh", rule.require_fresh),
        "require_multi_source": payload.get("require_multi_source", rule.require_multi_source),
        "notify_on_recover": payload.get("notify_on_recover", rule.notify_on_recover),
        "check_interval_seconds": payload.get("check_interval_seconds", rule.check_interval_seconds),
        "cooldown_seconds": payload.get("cooldown_seconds", rule.cooldown_seconds),
        "enabled": payload.get("enabled", rule.enabled),
        "paused": payload.get("paused", rule.paused),
        "plan_id": payload.get("plan_id", rule.plan_id),
        "template_code": payload.get("template_code", rule.template_code),
    }
    if "groups" in payload:
        merged["groups"] = payload["groups"]
    else:
        # 没传条件树就沿用现有的（把库里的读出来再走一遍校验，保证一致性）
        merged["groups"] = await _dump_groups(session, rule_id, as_payload=True)

    data = validate_rule(merged, existing=rule)
    groups_data = data.pop("groups")

    for key, value in data.items():
        setattr(rule, key, value)

    await session.execute(delete(AlertCondition).where(AlertCondition.rule_id == rule_id))
    await session.execute(delete(AlertConditionGroup).where(AlertConditionGroup.rule_id == rule_id))
    await session.flush()
    await _write_groups(session, rule_id, groups_data)

    rule.version = (rule.version or 1) + 1
    rule.updated_at = _now()
    await session.flush()
    await _snapshot(session, rule, note=payload.get("change_note") or "更新规则")
    await session.flush()
    return rule


async def _write_groups(session: AsyncSession, rule_id: int, groups: list[dict[str, Any]],
                        parent_id: int | None = None) -> None:
    """把校验后的分组树写进数据库，并回填 rule.root_group_id。"""
    for g in groups:
        group = AlertConditionGroup(
            rule_id=rule_id,
            parent_group_id=parent_id,
            operator=g["operator"],
            label=g["label"],
            position=g["position"],
        )
        session.add(group)
        await session.flush()

        if parent_id is None:
            rule = await session.get(AlertRule, rule_id)
            if rule is not None and rule.root_group_id is None:
                rule.root_group_id = group.id

        for c in g["conditions"]:
            session.add(_to_condition(c, rule_id, group.id))

        if g["groups"]:
            await _write_groups(session, rule_id, g["groups"], parent_id=group.id)


async def _snapshot(session: AsyncSession, rule: AlertRule, note: str | None = None) -> None:
    payload = await _dump_groups(session, rule.id, as_payload=True)
    snapshot = {
        "rule": {
            "id": rule.id,
            "name": rule.name,
            "description": rule.description,
            "logic": rule.logic,
            "severity": rule.severity,
            "min_quality": rule.min_quality,
            "require_fresh": rule.require_fresh,
            "require_multi_source": rule.require_multi_source,
            "notify_on_recover": rule.notify_on_recover,
            "check_interval_seconds": rule.check_interval_seconds,
            "cooldown_seconds": rule.cooldown_seconds,
            "enabled": rule.enabled,
            "paused": rule.paused,
            "plan_id": rule.plan_id,
            "template_code": rule.template_code,
            "version": rule.version,
        },
        "groups": payload,
    }
    session.add(AlertRuleVersion(rule_id=rule.id, version=rule.version or 1,
                                 snapshot=snapshot, change_note=note))


async def get_rule(session: AsyncSession, rule_id: int) -> AlertRule | None:
    return await session.get(AlertRule, rule_id)


async def list_rules(session: AsyncSession, *, enabled_only: bool = False,
                     limit: int = 500, offset: int = 0) -> list[AlertRule]:
    stmt = select(AlertRule).order_by(AlertRule.id.desc()).limit(limit).offset(offset)
    if enabled_only:
        stmt = stmt.where(AlertRule.enabled.is_(True), AlertRule.paused.is_(False))
    return list((await session.execute(stmt)).scalars().all())


async def delete_rule(session: AsyncSession, rule_id: int) -> dict[str, Any]:
    """删除规则及其条件树。事件与历史留痕**不删**（审计需要）。"""
    rule = await get_rule(session, rule_id)
    if rule is None:
        return {"deleted": False, "reason": "rule_not_found"}
    conds = await session.execute(
        select(AlertCondition.id).where(AlertCondition.rule_id == rule_id)
    )
    n_cond = len(list(conds.scalars().all()))
    groups = await session.execute(
        select(AlertConditionGroup.id).where(AlertConditionGroup.rule_id == rule_id)
    )
    n_group = len(list(groups.scalars().all()))

    await session.execute(delete(AlertCondition).where(AlertCondition.rule_id == rule_id))
    await session.execute(delete(AlertConditionGroup).where(AlertConditionGroup.rule_id == rule_id))
    await session.delete(rule)
    await session.flush()
    return {
        "deleted": True,
        "conditions_removed": n_cond,
        "groups_removed": n_group,
    }


async def set_paused(session: AsyncSession, rule_id: int, paused: bool) -> AlertRule:
    rule = await get_rule(session, rule_id)
    if rule is None:
        raise RuleValidationError(f"规则 {rule_id} 不存在。", "id")
    rule.paused = paused
    rule.updated_at = _now()
    await session.flush()
    return rule


async def duplicate_rule(session: AsyncSession, rule_id: int, new_name: str | None = None) -> AlertRule:
    """复制规则。这是「基于模板再微调」最常用的操作。"""
    src = await get_rule(session, rule_id)
    if src is None:
        raise RuleValidationError(f"规则 {rule_id} 不存在。", "id")
    groups = await _dump_groups(session, rule_id, as_payload=True)
    payload = {
        "name": new_name or f"{src.name}（副本）",
        "description": src.description,
        "logic": src.logic,
        "severity": src.severity,
        "min_quality": src.min_quality,
        "require_fresh": src.require_fresh,
        "require_multi_source": src.require_multi_source,
        "notify_on_recover": src.notify_on_recover,
        "check_interval_seconds": src.check_interval_seconds,
        "cooldown_seconds": src.cooldown_seconds,
        "enabled": src.enabled,
        "paused": src.paused,
        "plan_id": src.plan_id,
        "template_code": src.template_code,
        "groups": groups,
    }
    return await create_rule(session, payload)


# ------------------------------------------------------------------ 读成树

async def load_tree(session: AsyncSession, rule_id: int) -> list[ConditionGroup]:
    """把库里的条件树读回内存结构，交给 RuleEngine 求值。"""
    rows_g = list(
        (await session.execute(
            select(AlertConditionGroup).where(AlertConditionGroup.rule_id == rule_id)
        )).scalars().all()
    )
    rows_c = list(
        (await session.execute(
            select(AlertCondition).where(AlertCondition.rule_id == rule_id)
        )).scalars().all()
    )

    by_parent: dict[int | None, list[AlertConditionGroup]] = {}
    for g in rows_g:
        by_parent.setdefault(g.parent_group_id, []).append(g)
    cond_by_group: dict[int | None, list[AlertCondition]] = {}
    for c in rows_c:
        cond_by_group.setdefault(c.group_id, []).append(c)

    def build(parent: int | None) -> list[ConditionGroup]:
        out: list[ConditionGroup] = []
        for g in sorted(by_parent.get(parent, []), key=lambda x: (x.position, x.id or 0)):
            node = ConditionGroup(
                id=g.id,
                operator=g.operator,  # type: ignore[arg-type]
                label=g.label,
                parent_group_id=g.parent_group_id,
                position=g.position or 0,
            )
            node.conditions = [
                _row_to_condition(c)
                for c in sorted(cond_by_group.get(g.id, []), key=lambda x: (x.position, x.id or 0))
            ]
            node.groups = build(g.id)
            out.append(node)
        return out

    roots = build(None)
    if not roots:
        # 兜底：条件直接挂在规则上（group_id 为 NULL）
        loose = cond_by_group.get(None) or []
        if loose:
            node = ConditionGroup(id=None, operator="AND")
            node.conditions = [_row_to_condition(c) for c in loose]
            roots = [node]
    return roots


def _row_to_condition(c: AlertCondition) -> Condition:
    return Condition(
        id=c.id,
        metric_code=c.metric_code,
        operator=c.operator,
        threshold=c.threshold,
        threshold_high=c.threshold_high,
        compare_metric=c.compare_metric,
        change_window=c.change_window,
        percentile_window_days=c.percentile_window_days,
        expected_state=c.expected_state,
        duration_seconds=c.duration_seconds or 0,
        consecutive_count=c.consecutive_count or 1,
        min_quality=c.min_quality,
        group_id=c.group_id,
        position=c.position or 0,
    )


async def _dump_groups(session: AsyncSession, rule_id: int, *,
                       as_payload: bool = False) -> list[dict[str, Any]]:
    tree = await load_tree(session, rule_id)
    if as_payload:
        return [_group_payload(g) for g in tree]
    return [_group_dict(g) for g in tree]


def _group_payload(g: ConditionGroup) -> dict[str, Any]:
    """转成「可以再喂给 validate_group」的形状（不含 id）。"""
    return {
        "operator": g.operator,
        "label": g.label,
        "position": g.position,
        "conditions": [
            {
                "metric_code": c.metric_code,
                "operator": c.operator,
                "threshold": c.threshold,
                "threshold_high": c.threshold_high,
                "compare_metric": c.compare_metric,
                "change_window": c.change_window,
                "percentile_window_days": c.percentile_window_days,
                "expected_state": c.expected_state,
                "duration_seconds": c.duration_seconds,
                "consecutive_count": c.consecutive_count,
                "min_quality": c.min_quality,
                "position": c.position,
            }
            for c in g.conditions
        ],
        "groups": [_group_payload(sub) for sub in g.groups],
    }


def _group_dict(g: ConditionGroup) -> dict[str, Any]:
    """转成「给前端渲染」的形状（含 id、含中文说明）。"""
    return {
        "id": g.id,
        "operator": g.operator,
        "operator_label": {"AND": "并且", "OR": "或者", "NOT": "都不满足"}.get(g.operator, g.operator),
        "label": g.label,
        "position": g.position,
        "conditions": [c.to_dict() for c in g.conditions],
        "groups": [_group_dict(sub) for sub in g.groups],
    }


# ------------------------------------------------------------------ 自然语言化

def describe_rule(rule: AlertRule, tree: list[ConditionGroup]) -> str:
    """把整条规则说成一句人话，给邮件标题与非金融用户看。"""
    if not tree:
        return f"规则「{rule.name}」没有任何条件"

    parts = [_describe_group(g) for g in tree]
    joiner = " 并且 " if (rule.logic or "AND") == "AND" else " 或者 "
    return joiner.join(p for p in parts if p)


def _describe_group(g: ConditionGroup) -> str:
    items = [c.describe() for c in g.conditions]
    items += [f"（{_describe_group(sub)}）" for sub in g.groups]
    if not items:
        return ""
    if g.operator == "AND":
        return " 且 ".join(items)
    if g.operator == "OR":
        return " 或 ".join(items)
    return "并非（" + " 或 ".join(items) + "）"


async def rule_to_dict(session: AsyncSession, rule: AlertRule, *,
                       include_tree: bool = True) -> dict[str, Any]:
    """规则的完整 API 表示。"""
    data: dict[str, Any] = {
        "id": rule.id,
        "name": rule.name,
        "description": rule.description,
        "logic": rule.logic,
        "enabled": rule.enabled,
        "paused": rule.paused,
        "severity": rule.severity,
        "severity_cn": SEVERITY_CN.get(rule.severity, rule.severity),
        "cooldown_seconds": rule.cooldown_seconds,
        "notify_on_recover": rule.notify_on_recover,
        "min_quality": rule.min_quality,
        "require_fresh": rule.require_fresh,
        "require_multi_source": rule.require_multi_source,
        "check_interval_seconds": rule.check_interval_seconds,
        "plan_id": rule.plan_id,
        "template_code": rule.template_code,
        "state": rule.state,
        "state_cn": {
            "NORMAL": "正常监测中",
            "TRIGGERED": "已触发",
            "COOLDOWN": "冷却中",
            "RECOVERED": "已恢复",
        }.get(rule.state, rule.state),
        "last_checked_at": rule.last_checked_at.isoformat() if rule.last_checked_at else None,
        "last_trigger_at": rule.last_trigger_at.isoformat() if rule.last_trigger_at else None,
        "last_recovered_at": rule.last_recovered_at.isoformat() if rule.last_recovered_at else None,
        "cooldown_until": rule.cooldown_until.isoformat() if rule.cooldown_until else None,
        "trigger_count": rule.trigger_count or 0,
        "last_error": rule.last_error,
        "version": rule.version or 1,
        "created_at": rule.created_at.isoformat() if rule.created_at else None,
        "updated_at": rule.updated_at.isoformat() if rule.updated_at else None,
    }
    if include_tree:
        tree = await load_tree(session, rule.id)
        data["groups"] = [_group_dict(g) for g in tree]
        data["expression"] = describe_rule(rule, tree)
        data["condition_count"] = sum(_count_group(g) for g in tree)
    return data


def _count_group(g: ConditionGroup) -> int:
    return len(g.conditions) + sum(_count_group(s) for s in g.groups)


# ------------------------------------------------------------------ 指标目录输出

def catalog_for_frontend() -> list[dict[str, Any]]:
    """指标目录给前端用：包含「这个条件代表什么」的通俗说明。"""
    out: list[dict[str, Any]] = []
    for code in sorted(CATALOG):
        spec = CATALOG[code]
        item = {
            "code": spec.code,
            "name_cn": spec.name_cn,
            "group": spec.group,
            "unit": spec.unit,
            "description": spec.description,
            "direction": spec.direction,
            "supports_change": spec.supports_change,
        }
        item.update(spec.extra)
        out.append(item)
    return out


def operators_for_frontend() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "label": meta.get("label", code),
            "arity": meta.get("arity", 1),
            "transition": bool(meta.get("transition")),
            "needs_window": bool(meta.get("needs_window")),
            "needs_compare": bool(meta.get("needs_compare")),
            "needs_state": bool(meta.get("needs_state")),
        }
        for code, meta in VALID_OPERATORS.items()
    ]
