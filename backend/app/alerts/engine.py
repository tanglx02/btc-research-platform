# -*- coding: utf-8 -*-
"""通用条件规则引擎。

结构：Rule -> ConditionGroup(嵌套, AND/OR/NOT) -> Condition(叶子)

支持的运算符（全部在 _OPS 里实现，新增运算符只需加一条）：
    比较：gt / gte / lt / lte / eq / neq
    区间：between（在区间内）/ enter_range（进入区间，只在跨越瞬间成立）
          exit_range（离开区间，只在跨越瞬间成立）
    变化率：change_gt / change_lt （配合 change_window: 1h/4h/12h/24h/7d/30d）
    交叉：cross_above / cross_below （上穿/下穿，可用 compare_metric 指定右值如 ma200）
    分位：percentile_gt / percentile_lt （对 metric_code 自己的历史序列求分位）
    状态：state_change（状态变化）/ state_is（处于某状态）

叶子之上还叠加三类「时间维度」修饰（与运算符正交）：
    duration_seconds  需持续满足的秒数（避免瞬时异常数据误报）
    consecutive_count 需连续满足的检测次数（避免单次抖动误报）
    这两者由 engine 维护状态（condition_since / consecutive_hits），而不是在运算符里硬编码。

**因果性**：所有「进入区间 / 交叉 / 变化率」判断只使用「当前值」与「上一期的已知值」，
历史序列一律按时间升序只取到最后两个点，不使用任何未来数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from .catalog import CATALOG, MetricContext, MetricUnavailable, get_spec

Operator = str
GroupOperator = Literal["AND", "OR", "NOT"]

# 需要「上一个值」才能判断的运算符
_TRANSITION_OPS = {"enter_range", "exit_range", "cross_above", "cross_below", "state_change"}

VALID_OPERATORS: dict[str, dict[str, Any]] = {
    "gt": {"label": "大于", "arity": 1, "unit_aware": True},
    "gte": {"label": "大于等于", "arity": 1},
    "lt": {"label": "小于", "arity": 1},
    "lte": {"label": "小于等于", "arity": 1},
    "eq": {"label": "等于", "arity": 1},
    "neq": {"label": "不等于", "arity": 1},
    "between": {"label": "在区间内", "arity": 2},
    "enter_range": {"label": "进入区间", "arity": 2, "transition": True},
    "exit_range": {"label": "离开区间", "arity": 2, "transition": True},
    "change_gt": {"label": "变化超过", "arity": 1, "needs_window": True},
    "change_lt": {"label": "变化低于", "arity": 1, "needs_window": True},
    "cross_above": {"label": "上穿", "arity": 0, "transition": True, "needs_compare": True},
    "cross_below": {"label": "下穿", "arity": 0, "transition": True, "needs_compare": True},
    "percentile_gt": {"label": "历史分位高于", "arity": 1},
    "percentile_lt": {"label": "历史分位低于", "arity": 1},
    "state_change": {"label": "状态发生变化", "arity": 0, "transition": True},
    "state_is": {"label": "处于指定状态", "arity": 0, "needs_state": True},
}

CHANGE_WINDOWS = {"1h": 1, "4h": 4, "12h": 12, "24h": 24, "7d": 7 * 24, "30d": 30 * 24}


# ------------------------------------------------------------------ 数据结构

@dataclass(slots=True)
class Condition:
    """叶子条件。"""

    id: int | None
    metric_code: str
    operator: str
    threshold: float | None = None
    threshold_high: float | None = None
    compare_metric: str | None = None
    change_window: str | None = None
    percentile_window_days: int | None = None
    expected_state: str | None = None
    duration_seconds: int = 0
    consecutive_count: int = 1
    min_quality: str | None = None
    group_id: int | None = None
    position: int = 0

    def describe(self) -> str:
        spec = get_spec(self.metric_code)
        name = spec.name_cn if spec else self.metric_code
        unit = spec.unit if spec else ""
        op = VALID_OPERATORS.get(self.operator, {})
        label = op.get("label", self.operator)
        if self.operator in ("between", "enter_range", "exit_range"):
            return f"{name} {label} [{self.threshold} ~ {self.threshold_high}] {unit}".strip()
        if self.operator in ("cross_above", "cross_below"):
            other = get_spec(self.compare_metric or "")
            other_name = other.name_cn if other else (self.compare_metric or "")
            return f"{name} {label} {other_name}"
        if self.operator in ("change_gt", "change_lt"):
            return f"{name} {label} {self.threshold}% ({self.change_window or '未指定窗口'})"
        if self.operator in ("percentile_gt", "percentile_lt"):
            return f"{name} {label} {self.threshold}%"
        if self.operator == "state_change":
            return f"{name} 状态发生变化"
        if self.operator == "state_is":
            return f"{name} 处于「{self.expected_state}」"
        return f"{name} {label} {self.threshold} {unit}".strip()

    def to_dict(self) -> dict[str, Any]:
        spec = get_spec(self.metric_code)
        return {
            "id": self.id,
            "group_id": self.group_id,
            "metric_code": self.metric_code,
            "metric_name": spec.name_cn if spec else self.metric_code,
            "unit": spec.unit if spec else "",
            "operator": self.operator,
            "operator_label": VALID_OPERATORS.get(self.operator, {}).get("label", self.operator),
            "threshold": self.threshold,
            "threshold_high": self.threshold_high,
            "compare_metric": self.compare_metric,
            "change_window": self.change_window,
            "percentile_window_days": self.percentile_window_days,
            "expected_state": self.expected_state,
            "duration_seconds": self.duration_seconds,
            "consecutive_count": self.consecutive_count,
            "min_quality": self.min_quality,
            "position": self.position,
            "description": self.describe(),
        }


@dataclass(slots=True)
class ConditionGroup:
    """条件分组（可嵌套）。"""

    id: int | None
    operator: GroupOperator = "AND"
    label: str | None = None
    parent_group_id: int | None = None
    position: int = 0
    conditions: list[Condition] = field(default_factory=list)
    groups: list["ConditionGroup"] = field(default_factory=list)

    def children(self) -> list[Any]:
        out: list[Any] = []
        for c in sorted(self.conditions, key=lambda x: x.position):
            out.append(c)
        for g in sorted(self.groups, key=lambda x: x.position):
            out.append(g)
        return out


@dataclass(slots=True)
class ConditionResult:
    """单个条件的求值结果 —— 邮件里「为什么触发」就是把这些如实列出来。"""

    condition: Condition
    satisfied: bool
    actual: Any = None
    available: bool = True
    reason: str = ""
    previous: Any = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "condition_id": self.condition.id,
            "metric_code": self.condition.metric_code,
            "operator": self.condition.operator,
            "threshold": self.condition.threshold,
            "threshold_high": self.condition.threshold_high,
            "actual": self.actual,
            "previous": self.previous,
            "satisfied": self.satisfied,
            "available": self.available,
            "reason": self.reason,
            "description": self.condition.describe(),
        }
        spec = get_spec(self.condition.metric_code)
        if spec:
            d["metric_name"] = spec.name_cn
            d["unit"] = spec.unit
        return d


@dataclass(slots=True)
class EvalOutcome:
    """整条规则的求值结果。"""

    satisfied: bool
    results: list[ConditionResult] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    reason: str = ""

    def metric_values(self) -> dict[str, Any]:
        return {r.condition.metric_code: r.actual for r in self.results if r.actual is not None}

    def thresholds(self) -> dict[str, Any]:
        return {
            f"{r.condition.metric_code}.{r.condition.operator}": {
                "threshold": r.condition.threshold,
                "threshold_high": r.condition.threshold_high,
            }
            for r in self.results
        }


# ------------------------------------------------------------------ 取值工具

def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)  # 允许 "1.5" 这类字符串数值
    except (TypeError, ValueError):
        return None


def _prev_series_value(ctx: MetricContext, code: str) -> Any:
    """取历史序列的倒数第二个值（即「上一期」）。没有历史时返回 None。"""
    series = ctx.series(code)
    if len(series) >= 2:
        return series[-2]
    return None


def _percentile(code: str, current: float, series: list[float]) -> float | None:
    if not series:
        return None
    below = sum(1 for v in series if v <= current)
    return below / len(series) * 100.0


# ------------------------------------------------------------------ 运算符实现

def _eval_simple(cond: Condition, current: Any) -> tuple[bool, str]:
    """比较类运算符（无状态）。"""
    cur = _num(current)
    thr = _num(cond.threshold)
    op = cond.operator

    if op == "eq" or op == "neq":
        if cur is None or thr is None:
            # 状态类字符串比较
            equal = str(current) == str(cond.threshold)
            return (equal if op == "eq" else not equal), f"当前值 {current}"
        equal = cur == thr
        return (equal if op == "eq" else not equal), f"{cur} {'==' if equal else '!='} {thr}"

    if cur is None or thr is None:
        return False, "数值缺失，无法比较"

    if op == "gt":
        return cur > thr, f"{cur} {'>' if cur > thr else '<='} {thr}"
    if op == "gte":
        return cur >= thr, f"{cur} {'>=' if cur >= thr else '<'} {thr}"
    if op == "lt":
        return cur < thr, f"{cur} {'<' if cur < thr else '>='} {thr}"
    if op == "lte":
        return cur <= thr, f"{cur} {'<=' if cur <= thr else '>'} {thr}"
    if op == "between":
        high = _num(cond.threshold_high)
        if high is None:
            return False, "区间上界缺失"
        lo, hi = min(thr, high), max(thr, high)
        inside = lo <= cur <= hi
        return inside, f"{cur} {'在' if inside else '不在'} [{lo} ~ {hi}] 区间内"
    raise ValueError(f"未知运算符：{op}")


def _eval_transition(cond: Condition, current: Any, previous: Any,
                     in_range_prev: bool | None = None,
                     ctx: MetricContext | None = None) -> tuple[bool, str]:
    """跨越类运算符：只在「状态切换的那一刻」成立，避免持续满足时反复触发。"""
    op = cond.operator
    cur = _num(current)
    prev = _num(previous)

    if op in ("enter_range", "exit_range"):
        thr = _num(cond.threshold)
        high = _num(cond.threshold_high)
        if cur is None or thr is None or high is None:
            return False, "区间参数不完整"
        lo, hi = min(thr, high), max(thr, high)
        now_in = lo <= cur <= hi
        if prev is None:
            return False, f"缺少上一期数据，无法判断是否「{'进入' if op == 'enter_range' else '离开'}区间」"
        was_in = lo <= prev <= hi
        if op == "enter_range":
            hit = (not was_in) and now_in
            return hit, f"上一期 {prev}（区间外）→ 当前 {cur}（区间内）" if hit else f"未进入区间（上一期 {prev}，当前 {cur}）"
        hit = was_in and (not now_in)
        return hit, f"上一期 {prev}（区间内）→ 当前 {cur}（区间外）" if hit else f"未离开区间（上一期 {prev}，当前 {cur}）"

    if op in ("cross_above", "cross_below"):
        right_code = cond.compare_metric
        # 先尝试当作固定阈值处理
        right_val = _num(cond.threshold)
        right_label = str(cond.threshold)
        if right_code:
            # 修复说明：Condition 是 slots dataclass，没有 __dict__，
            # 早期把 ctx 塞进 cond.__dict__ 的写法在这里必然抛 AttributeError。
            # 改为显式参数传递。
            right_val = None
            if ctx is not None:
                try:
                    right_val = _num(ctx.value(right_code))
                except MetricUnavailable:
                    return False, f"对比指标 {right_code} 不可用，无法判断交叉"
            right_label = get_spec(right_code).name_cn if get_spec(right_code) else str(right_code)
        if cur is None or right_val is None:
            return False, "交叉判断的任一侧数值缺失"
        if prev is None:
            # 用对比序列的上一期做判断需要对齐时间轴，缺数据时如实返回不成立
            return False, "缺少上一期数据，无法判断是否发生交叉"
        if op == "cross_above":
            hit = prev <= right_val < cur
            return hit, f"{prev} → {cur} 上穿 {right_label}" if hit else f"未上穿 {right_label}（{prev} → {cur}）"
        hit = prev >= right_val > cur
        return hit, f"{prev} → {cur} 下穿 {right_label}" if hit else f"未下穿 {right_label}（{prev} → {cur}）"

    if op == "state_change":
        if previous is None:
            return False, "没有上一期的状态记录，无法判断状态变化"
        changed = str(current) != str(previous)
        return changed, f"状态由「{previous}」变为「{current}」" if changed else f"状态仍为「{current}」"

    raise ValueError(f"未知跨越运算符：{op}")


# 变化率窗口 -> 该窗口在**日线**上相当于多少期。
# 1h/4h/12h 在 `change_1h` 等指标里由小时线实现；但如果用户对某个只有日线的指标
# 直接用 1h 窗口，引擎不能假装能算出来（那是编数据），而要如实说「做不到」。
_CHANGE_PERIODS_DAILY: dict[str, int] = {
    "1h": 0, "4h": 0, "12h": 0,   # 0 表示「日线无法表达，需要小时线」
    "24h": 1, "7d": 7, "30d": 30,
}


def _eval_change_rate(cond: Condition, ctx: MetricContext) -> tuple[bool, str, Any]:
    """变化率条件。用指标自己的历史序列算，窗口不足时明确报不可用而不是外推。"""
    op = cond.operator
    window = (cond.change_window or "24h").lower()
    if window not in CHANGE_WINDOWS:
        return False, f"不支持的变化率窗口：{window}", None

    code = cond.metric_code
    series = ctx.series(code)
    if not series:
        return False, f"{code} 没有历史序列，无法计算 {window} 变化率", None

    periods = _CHANGE_PERIODS_DAILY.get(window, 0)
    if periods <= 0:
        # 小时级窗口：调用方应当使用 change_1h / change_4h / change_12h 这类
        # 由小时线实现的指标。这里不能拿日线点数去凑，否则是伪造结论。
        hourly = {c.get("close") for c in (ctx.intraday or [])}
        if not hourly:
            return False, (
                f"{window} 属于小时级窗口，但本地没有 1 小时 K 线，无法计算。"
                "请先用 change_1h/change_4h/change_12h 这类指标，或等待小时线采集。"
            ), None
        bar = CHANGE_WINDOWS[window]
        intraday_closes = [float(c["close"]) for c in ctx.intraday if c.get("close")]
        if len(intraday_closes) <= bar:
            return False, f"小时线只有 {len(intraday_closes)} 根，不足 {bar} 根", None
        base, cur = intraday_closes[-1 - bar], intraday_closes[-1]
        if not base:
            return False, "基准值为 0，变化率无意义", cur
        pct = (cur - base) / base * 100.0
        return _change_verdict(op, window, pct, cond)

    if len(series) <= periods:
        return False, f"历史序列只有 {len(series)} 个点，不足 {periods} 期，拒绝外推", None

    base = series[-1 - periods]
    cur = series[-1]
    if not base:
        return False, "基准值为 0，变化率无意义", cur
    pct = (cur - base) / base * 100.0
    return _change_verdict(op, window, pct, cond)


def _change_verdict(op: str, window: str, pct: float, cond: Condition) -> tuple[bool, str, Any]:
    thr = _num(cond.threshold) or 0.0
    if op == "change_gt":
        hit = pct > thr
        return hit, f"{window} 变化 {pct:+.2f}%（阈值 >{thr}%）", round(pct, 4)
    hit = pct < thr
    return hit, f"{window} 变化 {pct:+.2f}%（阈值 <{thr}%）", round(pct, 4)


def _eval_percentile(cond: Condition, ctx: MetricContext, current: Any) -> tuple[bool, str]:
    code = cond.metric_code
    series = ctx.series(code)
    if cond.percentile_window_days:
        series = series[-cond.percentile_window_days:] if cond.percentile_window_days > 0 else series
    if not series:
        return False, f"{code} 没有历史序列，无法计算历史分位"
    cur = _num(current)
    if cur is None:
        return False, "当前值不是数值，无法求分位"
    pct = _percentile(code, cur, series)
    if pct is None:
        return False, "分位计算失败"
    thr = _num(cond.threshold) or 0.0
    if cond.operator == "percentile_gt":
        hit = pct > thr
        return hit, f"当前值处于历史 {pct:.1f}% 分位（阈值 >{thr}%）"
    hit = pct < thr
    return hit, f"当前值处于历史 {pct:.1f}% 分位（阈值 <{thr}%）"


# ------------------------------------------------------------------ 引擎

class RuleEngine:
    """无状态求值器；规则的状态（持续时间/连续次数）由上层持久化后传入。"""

    def evaluate_condition(
        self,
        cond: Condition,
        ctx: MetricContext,
        *,
        prev_state: dict[str, Any] | None = None,
    ) -> ConditionResult:
        prev_state = prev_state or {}

        # 单条条件可以有自己的数据质量要求
        if cond.min_quality:
            q = ctx.metric_quality.get(cond.metric_code, "")
            if q and not _quality_ok(q, cond.min_quality):
                return ConditionResult(cond, False, None, False,
                                       f"数据质量为 {q}，未达到本条要求的 {cond.min_quality}")

        # 特殊：交叉条件的右值支持固定阈值或另一指标（ctx 通过参数传给 _eval_transition）
        try:
            # 变化率用序列自己算，不走 value()
            if cond.operator in ("change_gt", "change_lt"):
                hit, reason, actual = _eval_change_rate(cond, ctx)
                return ConditionResult(cond, hit, actual, True, reason)

            current = ctx.value(cond.metric_code)

            if cond.operator in ("percentile_gt", "percentile_lt"):
                hit, reason = _eval_percentile(cond, ctx, current)
                return ConditionResult(cond, hit, current, True, reason)

            if cond.operator == "state_is":
                want = str(cond.expected_state or "").strip()
                if not want:
                    return ConditionResult(cond, False, current, True, "未指定目标状态")
                hit = str(current).lower() == want.lower()
                return ConditionResult(cond, hit, current, True,
                                       f"当前状态「{current}」，目标「{want}」")

            if cond.operator in _TRANSITION_OPS:
                prev = prev_state.get(f"prev_{cond.id}")
                if prev is None:
                    prev = _prev_series_value(ctx, cond.metric_code)
                hit, reason = _eval_transition(cond, current, prev, ctx=ctx)
                return ConditionResult(cond, hit, current, True, reason, previous=prev)

            hit, reason = _eval_simple(cond, current)
            return ConditionResult(cond, hit, current, True, reason)
        except MetricUnavailable as exc:
            return ConditionResult(cond, False, None, False, exc.reason)

    def evaluate(
        self,
        groups: list[ConditionGroup],
        root_logic: GroupOperator,
        ctx: MetricContext,
        *,
        prev_state: dict[str, Any] | None = None,
    ) -> EvalOutcome:
        """求值整条规则。groups 为根层的分组列表。"""
        results: list[ConditionResult] = []
        unavailable: list[str] = []

        if not groups:
            return EvalOutcome(False, [], [], "规则没有任何条件")

        group_hits: list[bool] = []
        for g in groups:
            hit = self._eval_group(g, ctx, results, unavailable, prev_state)
            group_hits.append(hit)

        satisfied = all(group_hits) if root_logic == "AND" else any(group_hits)
        reason = f"根层 {root_logic}：{group_hits}"
        return EvalOutcome(satisfied, results, unavailable, reason)

    def _eval_group(
        self,
        group: ConditionGroup,
        ctx: MetricContext,
        results: list[ConditionResult],
        unavailable: list[str],
        prev_state: dict[str, Any] | None,
    ) -> bool:
        child_hits: list[bool] = []

        # 分组自己的持续时间 / 连续次数：用组内条件的整体满足情况来计算
        for cond in sorted(group.conditions, key=lambda c: c.position):
            r = self.evaluate_condition(cond, ctx, prev_state=prev_state)
            results.append(r)
            if not r.available:
                unavailable.append(r.condition.metric_code)
            child_hits.append(self._apply_time_modifier(r, prev_state, ctx))

        for sub in sorted(group.groups, key=lambda g: g.position):
            hit = self._eval_group(sub, ctx, results, unavailable, prev_state)
            child_hits.append(hit)

        if not child_hits:
            return False

        op = group.operator
        if op == "AND":
            return all(child_hits)
        if op == "OR":
            return any(child_hits)
        if op == "NOT":
            # NOT 语义：本组内的所有子项都不满足
            return not any(child_hits)
        raise ValueError(f"未知分组运算符：{op}")

    @staticmethod
    def _apply_time_modifier(r: ConditionResult, prev_state: dict[str, Any] | None,
                             ctx: MetricContext) -> bool:
        """把「持续时长」与「连续次数」叠加到单条条件的结论上。"""
        cond = r.condition
        if not r.available or not r.satisfied:
            return False
        if cond.consecutive_count > 1:
            hits = int((prev_state or {}).get(f"hits_{cond.id}", 0)) + 1
            r.reason += f"（连续第 {hits} 次，要求 {cond.consecutive_count} 次）"
            if hits < cond.consecutive_count:
                r.satisfied = False
                return False
        if cond.duration_seconds > 0:
            since = (prev_state or {}).get(f"since_{cond.id}")
            now = datetime.now(timezone.utc)
            if since is None:
                r.reason += f"（本轮开始计时，需持续 {cond.duration_seconds}s）"
                r.satisfied = False
                return False
            if isinstance(since, str):
                try:
                    since = datetime.fromisoformat(since)
                except ValueError:
                    since = now
            elapsed = (now - since).total_seconds()
            r.reason += f"（已持续 {int(elapsed)}s，要求 {cond.duration_seconds}s）"
            if elapsed < cond.duration_seconds:
                r.satisfied = False
                return False
        return True

    # -------------------------------------------------- 规则测试（不改状态）
    def dry_run(self, groups: list[ConditionGroup], root_logic: GroupOperator,
                ctx: MetricContext) -> EvalOutcome:
        """只按当前数据立刻判断一次，不涉持续时间与连续次数（用于页面上的「测试规则」）。"""
        results: list[ConditionResult] = []
        unavailable: list[str] = []

        def walk(g: ConditionGroup) -> bool:
            hits: list[bool] = []
            for cond in sorted(g.conditions, key=lambda c: c.position):
                r = self.evaluate_condition(cond, ctx)
                # 测试时忽略时间修饰，给出「当前是否满足」的直接答案
                results.append(r)
                if not r.available:
                    unavailable.append(r.condition.metric_code)
                hits.append(r.satisfied)
            for sub in sorted(g.groups, key=lambda x: x.position):
                hits.append(walk(sub))
            if not hits:
                return False
            if g.operator == "AND":
                return all(hits)
            if g.operator == "OR":
                return any(hits)
            return not any(hits)

        top = [walk(g) for g in groups]
        if not top:
            return EvalOutcome(False, [], [], "规则没有任何条件")
        satisfied = all(top) if root_logic == "AND" else any(top)
        return EvalOutcome(satisfied, results, unavailable, f"根层 {root_logic}：{top}")


def _quality_ok(actual: str, required: str) -> bool:
    """数据质量等级比较。数值越高越好：ANY < SINGLE_SOURCE < VERIFIED < CROSS_VERIFIED。"""
    from ..providers.types import QualityStatus

    order = ["ANY", "MISSING", "STALE", "SINGLE_SOURCE", "VERIFIED", "CROSS_VERIFIED"]
    a = str(actual or "ANY").upper()
    r = str(required or "ANY").upper()
    if r == "ANY":
        return a != "MISSING"
    if r in ("CROSS_VERIFIED", "VERIFIED"):
        # 严格要求：STALE 与 SINGLE_SOURCE 都不达标
        if a == "STALE":
            return False
    try:
        return order.index(a) >= order.index(r)
    except ValueError:
        return False


def quality_rank(value: str) -> int:
    order = ["ANY", "MISSING", "STALE", "SINGLE_SOURCE", "VERIFIED", "CROSS_VERIFIED"]
    try:
        return order.index(str(value or "ANY").upper())
    except ValueError:
        return 0


# 供外部直接引用的枚举（避免各层重复定义字符串）
QUALITY_CHOICES = ["ANY", "SINGLE_SOURCE", "VERIFIED", "CROSS_VERIFIED"]
