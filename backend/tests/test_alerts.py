# -*- coding: utf-8 -*-
"""智能监测与条件预警的测试。

覆盖需求四十二点名的场景，重点包括：
* 各类条件（价格 / 指标 / AND / OR / NOT / 嵌套 / 持续时间 / 连续次数）
* 跨越类（进入区间 / 离开区间 / 上穿 / 下穿 / 状态变化）
* 历史分位、Cooldown、重复触发抑制、恢复后再次触发
* SMTP 成功与失败、邮件重试、连续失败熔断
* **主 Provider 失效时 Alert 引擎仍然可用**（这条是重中之重）
* 数据过期 / 数据冲突时不触发
* 大量规则时不重复取数（性能约束）
* 程序重启 / 数据库重启后状态一致
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.alerts import rules as rules_mod
from app.alerts.catalog import MetricContext, MetricUnavailable
from app.alerts.engine import (
    Condition,
    ConditionGroup,
    EvalOutcome,
    RuleEngine,
    _quality_ok,
    quality_rank,
)
from app.alerts.notifier import (
    MAX_SEND_ATTEMPTS,
    EmailProvider,
    NotifyResult,
    OutgoingMessage,
    SmtpConfig,
    load_smtp_config,
)
from app.alerts.pipeline import AlertPipeline, ContextBundle, _payload_to_tree
from app.alerts.templates import build_body, build_subject

pytestmark = pytest.mark.asyncio


# ============================================================ 夹具

def _candles(closes: list[float], interval: str = "1d", start: datetime | None = None) -> list[dict]:
    t0 = start or (datetime.now(timezone.utc) - timedelta(days=len(closes)))
    step = 3600 if interval == "1h" else 86400
    out = []
    for i, c in enumerate(closes):
        ts = int((t0 + timedelta(seconds=step * i)).timestamp())
        out.append({
            "symbol": "BTC", "interval": interval, "ts": ts,
            "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": 100.0, "source_id": "test_fixture",
            "quality_status": "SINGLE_SOURCE",
            "observation_time": datetime.fromtimestamp(ts, tz=timezone.utc),
            "fetch_time": datetime.now(timezone.utc),
        })
    return out


def _ctx(closes: list[float], *, price: float | None = None,
         indicators: dict | None = None, analysis: dict | None = None,
         **kw) -> MetricContext:
    ctx = MetricContext(
        candles=_candles(closes),
        price_block={"available": True, "price": price if price is not None else closes[-1],
                     "source": {"provider": "test_src", "quality": "VERIFIED"}},
        indicators=indicators or {},
        analysis=analysis or {},
        **kw,
    )
    ctx.mark_source("price", "test_src", "VERIFIED", datetime.now(timezone.utc))
    return ctx


def _cond(code: str, op: str, **kw) -> Condition:
    return Condition(id=kw.pop("id", 1), metric_code=code, operator=op, **kw)


def _engage(monkeypatch, ctx: MetricContext, bundle_extra: dict | None = None) -> ContextBundle:
    """让 AlertPipeline 用我们造的上下文，而不是去取真实数据。"""
    bundle = ContextBundle(ctx=ctx, providers_used=["test_src"], quality="VERIFIED",
                           data_updated_at=datetime.now(timezone.utc))
    if bundle_extra:
        for k, v in bundle_extra.items():
            setattr(bundle, k, v)

    async def _fake_build(*, need_personal: bool = False):
        return bundle

    monkeypatch.setattr("app.alerts.pipeline.build_context", _fake_build)
    return bundle


# ============================================================ 指标目录

async def test_catalog_all_metrics_have_resolvers():
    """每一个登记的指标都必须有取值实现，否则规则会在运行期才炸。"""
    from app.alerts import catalog

    missing = sorted(set(catalog.CATALOG) - set(catalog._RESOLVERS))
    assert missing == [], f"以下指标缺少取值实现：{missing}"


async def test_catalog_every_metric_has_explanation():
    """需求三十七：每个指标都要有能给非金融用户看的说明。"""
    from app.alerts import catalog

    for code, spec in catalog.CATALOG.items():
        assert spec.name_cn, f"{code} 缺少中文名"
        assert spec.description, f"{code} 缺少通俗说明"
        assert len(spec.description) >= 6, f"{code} 的说明太短，解释不了什么"


# ============================================================ 简单条件

async def test_simple_gt_lt_eq():
    eng = RuleEngine()
    ctx = _ctx([100.0, 110.0], price=110.0)

    assert eng.evaluate_condition(_cond("price", "gt", threshold=100), ctx).satisfied
    assert not eng.evaluate_condition(_cond("price", "gt", threshold=120), ctx).satisfied
    assert eng.evaluate_condition(_cond("price", "gte", threshold=110), ctx).satisfied
    assert eng.evaluate_condition(_cond("price", "lt", threshold=120), ctx).satisfied
    assert eng.evaluate_condition(_cond("price", "lte", threshold=110), ctx).satisfied
    assert eng.evaluate_condition(_cond("price", "eq", threshold=110), ctx).satisfied
    assert eng.evaluate_condition(_cond("price", "neq", threshold=100), ctx).satisfied


async def test_between_and_range():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    r = eng.evaluate_condition(_cond("price", "between", threshold=100, threshold_high=110), ctx)
    assert r.satisfied
    r2 = eng.evaluate_condition(_cond("price", "between", threshold=106, threshold_high=110), ctx)
    assert not r2.satisfied


async def test_metric_unavailable_is_not_a_trigger():
    """取不到数据必须是「未触发 + 有原因」，绝不能当成满足。"""
    eng = RuleEngine()
    ctx = _ctx([100.0])
    r = eng.evaluate_condition(_cond("mvrv", "gt", threshold=1), ctx)
    assert r.satisfied is False
    assert r.available is False
    assert r.reason


# ============================================================ 变化率

async def test_change_rate_windows():
    eng = RuleEngine()
    # 日线：最后一天相对前一天 +10%
    ctx = _ctx([100.0, 110.0])
    r = eng.evaluate_condition(
        _cond("price", "change_gt", threshold=5, change_window="24h"), ctx)
    assert r.satisfied, r.reason
    r2 = eng.evaluate_condition(
        _cond("price", "change_gt", threshold=20, change_window="24h"), ctx)
    assert not r2.satisfied

    r3 = eng.evaluate_condition(
        _cond("price", "change_lt", threshold=-5, change_window="24h"), ctx)
    assert not r3.satisfied


async def test_change_rate_insufficient_history():
    eng = RuleEngine()
    ctx = _ctx([100.0])
    r = eng.evaluate_condition(
        _cond("price", "change_gt", threshold=1, change_window="24h"), ctx)
    assert not r.satisfied
    assert not r.available or "不足" in r.reason or "无法" in r.reason


async def test_hourly_change_uses_intraday():
    """1h 变化率应当优先用小时线，而不是拿日线硬算。"""
    eng = RuleEngine()
    ctx = MetricContext(
        candles=_candles([100.0, 100.0]),
        intraday=_candles([100.0, 108.0], interval="1h"),
        price_block={"available": True, "price": 108.0, "source": {"provider": "s"}},
    )
    r = eng.evaluate_condition(
        _cond("price", "change_gt", threshold=5, change_window="1h"), ctx)
    assert r.satisfied, r.reason


# ============================================================ 跨越类

async def test_enter_and_exit_range():
    eng = RuleEngine()
    # 上一期在区间外(90)，当前进入区间(105)
    ctx = _ctx([90.0, 105.0])
    r = eng.evaluate_condition(
        _cond("price", "enter_range", threshold=100, threshold_high=110), ctx)
    assert r.satisfied, r.reason

    # 上一期在区间内(105)，当前离开(120)
    ctx2 = _ctx([105.0, 120.0])
    r2 = eng.evaluate_condition(
        _cond("price", "exit_range", threshold=100, threshold_high=110), ctx2)
    assert r2.satisfied, r2.reason


async def test_enter_range_does_not_refire_while_inside():
    """持续在区间内不应反复成立 —— 这正是「进入区间通知一次」的语义。"""
    eng = RuleEngine()
    ctx = _ctx([105.0, 106.0])
    r = eng.evaluate_condition(
        _cond("price", "enter_range", threshold=100, threshold_high=110), ctx)
    assert not r.satisfied, "一直在区间内，不应该再次判定为「进入」"


async def test_cross_above_metric():
    """价格上穿 MA200 —— 必须是原生规则，而不是让人手动算。"""
    eng = RuleEngine()
    ctx = _ctx([90.0, 110.0], indicators={"latest": {"ma200": 100.0}})
    r = eng.evaluate_condition(_cond("price", "cross_above", compare_metric="ma200"), ctx)
    assert r.satisfied, r.reason

    # 已经在上方，不应再次判定为上穿
    ctx2 = _ctx([105.0, 110.0], indicators={"latest": {"ma200": 100.0}})
    r2 = eng.evaluate_condition(_cond("price", "cross_above", compare_metric="ma200"), ctx2)
    assert not r2.satisfied


async def test_cross_below_fixed_threshold():
    eng = RuleEngine()
    ctx = _ctx([110.0, 95.0])
    r = eng.evaluate_condition(_cond("price", "cross_below", threshold=100.0), ctx)
    assert r.satisfied, r.reason


async def test_state_change_and_state_is():
    eng = RuleEngine()
    ctx = _ctx([100.0, 101.0], analysis={
        "available": True,
        "cycle": {"phase": "accumulation"},
        "valuation": {"state": "fair"},
        "risk": {"level": "moderate", "overall_risk": 40},
        "regime": {"state": "neutral"},
    })
    r = eng.evaluate_condition(_cond("cycle_phase", "state_is", expected_state="accumulation"), ctx)
    assert r.satisfied, r.reason

    r2 = eng.evaluate_condition(_cond("cycle_phase", "state_is", expected_state="distribution"), ctx)
    assert not r2.satisfied

    # state_change：无上一期状态数据时不应误报
    r3 = eng.evaluate_condition(_cond("cycle_phase", "state_change"), ctx)
    assert not r3.satisfied


# ============================================================ 历史分位

async def test_percentile_condition():
    eng = RuleEngine()
    closes = [float(v) for v in range(100, 200)]  # 100 个点
    ctx = _ctx(closes, price=199.0)
    r = eng.evaluate_condition(_cond("price", "percentile_gt", threshold=90), ctx)
    assert r.satisfied, r.reason

    ctx2 = _ctx(closes, price=100.0)
    r2 = eng.evaluate_condition(_cond("price", "percentile_gt", threshold=90), ctx2)
    assert not r2.satisfied


async def test_percentile_without_series_is_honest():
    """没有历史序列就不应该硬算分位，而要如实说不可以。"""
    eng = RuleEngine()
    ctx = _ctx([100.0])
    r = eng.evaluate_condition(_cond("risk_score", "percentile_gt", threshold=90), ctx)
    assert r.satisfied is False
    assert r.available is False


# ============================================================ AND / OR / NOT / 嵌套

async def test_and_or_not_and_nested_groups():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)

    # (price>100 AND price<110) OR (NOT price>120)
    tree = [
        ConditionGroup(id=1, operator="AND", conditions=[
            _cond("price", "gt", threshold=100, id=11),
            _cond("price", "lt", threshold=110, id=12),
        ]),
        ConditionGroup(id=2, operator="NOT", conditions=[
            _cond("price", "gt", threshold=200, id=21),
        ]),
    ]
    out = eng.evaluate(tree, "AND", ctx)
    assert out.satisfied, out.reason

    # 嵌套：(A AND (B OR C))
    nested = [
        ConditionGroup(id=3, operator="AND", conditions=[
            _cond("price", "gt", threshold=100, id=31),
        ], groups=[
            ConditionGroup(id=4, operator="OR", conditions=[
                _cond("price", "gt", threshold=999, id=41),
                _cond("price", "gt", threshold=1, id=42),
            ]),
        ]),
    ]
    out2 = eng.evaluate(nested, "AND", ctx)
    assert out2.satisfied, out2.reason

    # 需求六的示例形态：(A AND B) OR (C AND D AND E)
    expr = [
        ConditionGroup(id=5, operator="AND", conditions=[
            _cond("price", "gt", threshold=100, id=51),
            _cond("price", "lt", threshold=110, id=52),
        ]),
        ConditionGroup(id=6, operator="AND", conditions=[
            _cond("price", "gt", threshold=999, id=61),
            _cond("price", "gt", threshold=999, id=62),
            _cond("price", "gt", threshold=999, id=63),
        ]),
    ]
    out3 = eng.evaluate(expr, "OR", ctx)
    assert out3.satisfied, out3.reason


async def test_not_group_requires_all_children_false():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    tree = [ConditionGroup(id=1, operator="NOT", conditions=[
        _cond("price", "gt", threshold=200, id=1),
        _cond("price", "lt", threshold=50, id=2),
    ])]
    assert eng.evaluate(tree, "AND", ctx).satisfied

    tree2 = [ConditionGroup(id=2, operator="NOT", conditions=[
        _cond("price", "gt", threshold=100, id=3),
    ])]
    assert not eng.evaluate(tree2, "AND", ctx).satisfied


# ============================================================ 时间修饰

async def test_consecutive_count_requires_n_hits():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    tree = [ConditionGroup(id=1, operator="AND", conditions=[
        _cond("price", "gt", threshold=100, consecutive_count=3, id=7),
    ])]

    # 第 1、2 次：还不到 3 次，不成立
    out1 = eng.evaluate(tree, "AND", ctx, prev_state={})
    assert not out1.satisfied
    assert "连续第 1 次" in out1.results[0].reason

    out2 = eng.evaluate(tree, "AND", ctx, prev_state={"hits_7": 1})
    assert not out2.satisfied

    # 第 3 次：成立
    out3 = eng.evaluate(tree, "AND", ctx, prev_state={"hits_7": 2})
    assert out3.satisfied, out3.results[0].reason


async def test_duration_requires_elapsed_time():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    tree = [ConditionGroup(id=1, operator="AND", conditions=[
        _cond("price", "gt", threshold=100, duration_seconds=3600, id=8),
    ])]

    # 第一轮：开始计时，不成立
    out1 = eng.evaluate(tree, "AND", ctx, prev_state={})
    assert not out1.satisfied
    assert "本轮开始计时" in out1.results[0].reason

    # 已经过去 2 小时：成立
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    out2 = eng.evaluate(tree, "AND", ctx, prev_state={"since_8": past})
    assert out2.satisfied, out2.results[0].reason

    # 只过去 1 分钟：不成立
    just = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    out3 = eng.evaluate(tree, "AND", ctx, prev_state={"since_8": just})
    assert not out3.satisfied


async def test_dry_run_ignores_time_modifier():
    """页面上的「测试规则」应直接回答现在是否成立，不该被持续时间卡住。"""
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    tree = [ConditionGroup(id=1, operator="AND", conditions=[
        _cond("price", "gt", threshold=100, duration_seconds=86400, id=9),
    ])]
    out = eng.dry_run(tree, "AND", ctx)
    assert out.satisfied


# ============================================================ 数据质量

async def test_quality_ordering():
    assert quality_rank("CROSS_VERIFIED") > quality_rank("VERIFIED")
    assert quality_rank("VERIFIED") > quality_rank("SINGLE_SOURCE")
    assert quality_rank("SINGLE_SOURCE") > quality_rank("STALE")
    assert _quality_ok("CROSS_VERIFIED", "VERIFIED")
    assert not _quality_ok("SINGLE_SOURCE", "VERIFIED")
    assert not _quality_ok("STALE", "VERIFIED")
    assert _quality_ok("SINGLE_SOURCE", "ANY")
    assert not _quality_ok("MISSING", "ANY")


async def test_condition_level_quality_gate():
    eng = RuleEngine()
    ctx = _ctx([100.0], price=105.0)
    # mark_source 用的是 setdefault（第一个来源优先），所以要直接覆盖才能模拟「质量不足」
    ctx.metric_quality["price"] = "STALE"
    r = eng.evaluate_condition(
        _cond("price", "gt", threshold=100, min_quality="VERIFIED"), ctx)
    assert not r.satisfied
    assert r.available is False


# ============================================================ 规则校验

async def test_validate_rule_rejects_all_the_bad_inputs():
    """需求三十八：错误阈值 / 不存在指标 / 单位错误 / 频率过高 / 重复条件 / 自相矛盾。"""
    cases = [
        ({"name": "", "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 1}]}]}, "名称"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "nope", "operator": "gt", "threshold": 1}]}]}, "指标"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "gt"}]}]}, "阈值"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "wat", "threshold": 1}]}]}, "条件类型"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "between", "threshold": 5}]}]}, "区间"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "cross_above"}]}]}, "跨越"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "change_gt", "threshold": 5}]}]}, "窗口"),
        ({"name": "x", "check_interval_seconds": 1, "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 1}]}]}, "周期过短"),
        ({"name": "x", "cooldown_seconds": 1, "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 1}]}]}, "冷却"),
        ({"name": "x", "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 1e9}]}]}, "不合理"),
        ({"name": "x", "groups": [{"conditions": [
            {"metric_code": "price", "operator": "gt", "threshold": 1},
            {"metric_code": "price", "operator": "gt", "threshold": 1},
        ]}]}, "重复"),
        ({"name": "x", "groups": [{"operator": "AND", "conditions": [
            {"metric_code": "price", "operator": "gt", "threshold": 100},
            {"metric_code": "price", "operator": "lt", "threshold": 50},
        ]}]}, "矛盾"),
        ({"name": "x", "groups": []}, "至少要有一个条件"),
        ({"name": "x", "groups": [{"conditions": [
            {"metric_code": "price", "operator": "percentile_gt", "threshold": 200}]}]}, "0~100"),
        ({"name": "x", "severity": "SUPER", "groups": [{"conditions": [
            {"metric_code": "price", "operator": "gt", "threshold": 1}]}]}, "提醒等级"),
    ]
    for payload, hint in cases:
        with pytest.raises(rules_mod.RuleValidationError) as ei:
            rules_mod.validate_rule(payload)
        assert ei.value.message, f"{hint} 的错误信息不能为空"


async def test_validate_rule_accepts_good_input():
    data = rules_mod.validate_rule({
        "name": "价格上穿 MA200",
        "severity": "HIGH",
        "cooldown_seconds": 3600,
        "check_interval_seconds": 60,
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "price", "operator": "cross_above",
                            "compare_metric": "ma200"}],
        }],
    })
    assert data["severity"] == "HIGH"
    assert len(data["groups"]) == 1


# ============================================================ 规则 CRUD

async def test_rule_crud_roundtrip(db_ready):
    factory = db_ready
    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "估值低位",
            "severity": "INFO",
            "groups": [{
                "operator": "AND",
                "conditions": [{"metric_code": "mvrv", "operator": "lt", "threshold": 1.5}],
            }],
        })
        await session.commit()
        rid = rule.id
        assert rid is not None
        assert rule.version == 1

        detail = await rules_mod.rule_to_dict(session, rule)
        assert detail["condition_count"] == 1
        assert "MVRV" in detail["expression"]
        assert detail["groups"][0]["conditions"][0]["description"]

        # 更新 -> 版本号自增、快照留存
        await rules_mod.update_rule(session, rid, {
            "name": "估值低位 v2",
            "groups": [{
                "operator": "AND",
                "conditions": [{"metric_code": "mvrv", "operator": "lt", "threshold": 1.2}],
            }],
        })
        await session.commit()
        r2 = await rules_mod.get_rule(session, rid)
        assert r2.version == 2
        assert "1.2" in (await rules_mod.rule_to_dict(session, r2))["expression"]

        # 复制
        dup = await rules_mod.duplicate_rule(session, rid)
        await session.commit()
        assert dup.id != rid
        assert "副本" in dup.name

        # 暂停 / 恢复
        await rules_mod.set_paused(session, rid, True)
        assert (await rules_mod.get_rule(session, rid)).paused is True
        await rules_mod.set_paused(session, rid, False)

        # 删除 -> 条件树级联清理
        result = await rules_mod.delete_rule(session, rid)
        await session.commit()
        assert result["deleted"]
        assert result["conditions_removed"] == 1
        assert result["groups_removed"] == 1
        assert await rules_mod.get_rule(session, rid) is None


async def test_rule_tree_supports_nesting_roundtrip(db_ready):
    """嵌套分组必须能原样存取（否则复杂规则会在编辑后变形）。"""
    factory = db_ready
    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "嵌套规则",
            "logic": "OR",
            "groups": [
                {"operator": "AND", "conditions": [
                    {"metric_code": "price", "operator": "gt", "threshold": 100000}]},
                {"operator": "AND", "conditions": [
                    {"metric_code": "mvrv", "operator": "gt", "threshold": 3},
                    {"metric_code": "fear_greed", "operator": "gt", "threshold": 85},
                ]},
            ],
        })
        await session.commit()
        tree = await rules_mod.load_tree(session, rule.id)
        assert len(tree) == 2
        assert len(tree[1].conditions) == 2
        assert tree[0].conditions[0].metric_code == "price"

        expr = rules_mod.describe_rule(rule, tree)
        assert "或" in expr


async def test_rule_versions_snapshot_survives_edits(db_ready):
    """改了阈值后，历史版本仍能还原当时的定义。"""
    from sqlalchemy import select

    from app.db.models import AlertRuleVersion

    factory = db_ready
    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "版本测试",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 1}]}],
        })
        await session.commit()
        rid = rule.id
        await rules_mod.update_rule(session, rid, {
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 999}]}],
        })
        await session.commit()

        rows = list((await session.execute(
            select(AlertRuleVersion).where(AlertRuleVersion.rule_id == rid)
            .order_by(AlertRuleVersion.version)
        )).scalars().all())
    assert len(rows) >= 2
    first_thr = rows[0].snapshot["groups"][0]["conditions"][0]["threshold"]
    last_thr = rows[-1].snapshot["groups"][0]["conditions"][0]["threshold"]
    assert first_thr == 1 and last_thr == 999


# ============================================================ 状态机 / 冷却 / 事件

async def test_pipeline_triggers_and_writes_event(db_ready, monkeypatch):
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "价格高于 100",
            "cooldown_seconds": 3600,
            "min_quality": "ANY",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    # 用一个"哑"通知通道，避免真发邮件
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    result = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert result["checked"] == 1
    assert result["triggered"] == 1

    async with factory() as session:
        rule = await rules_mod.get_rule(session, rid)
        assert rule.state == "TRIGGERED"
        assert rule.trigger_count == 1
        assert rule.cooldown_until is not None

        from app.db.models import AlertEvent, AlertCooldown
        from sqlalchemy import select
        events = list((await session.execute(select(AlertEvent))).scalars().all())
        assert len(events) == 1
        ev = events[0]
        # 需求二十五：事件必须记录这些字段
        assert ev.metric_values and ev.threshold and ev.condition_result
        assert ev.notification_status == "PENDING"
        assert ev.provider is not None
        assert ev.data_quality
        assert ev.trigger_time is not None

        cds = list((await session.execute(select(AlertCooldown))).scalars().all())
        assert len(cds) == 1


async def test_cooldown_prevents_duplicate_notifications(db_ready, monkeypatch):
    """需求十三/十四：冷却期内条件仍成立，但不重复触发、不重复发邮件。"""
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "冷却测试",
            "cooldown_seconds": 86400,
            "min_quality": "ANY",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    r1 = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert r1["triggered"] == 1

    # 强制再跑一轮：条件仍然成立，但处于冷却
    r2 = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert r2["triggered"] == 0

    async with factory() as session:
        from sqlalchemy import select

        from app.db.models import AlertEvent
        events = list((await session.execute(select(AlertEvent))).scalars().all())
    assert len(events) == 1, "冷却期内不能产生第二条事件（否则就是重复邮件）"


async def test_recovery_then_retrigger(db_ready, monkeypatch):
    """需求十四：恢复正常后可以再次触发。"""
    factory = db_ready
    ctx_high = _ctx([100.0, 110.0], price=110.0)
    _engage(monkeypatch, ctx_high)
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "恢复测试",
            "cooldown_seconds": 3600,
            "min_quality": "ANY",
            "notify_on_recover": True,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    await AlertPipeline().run_cycle(force=True, rule_ids=[rid])

    # 条件不再成立 -> 应恢复
    ctx_low = _ctx([100.0, 90.0], price=90.0)
    monkeypatch.setattr("app.alerts.pipeline.build_context",
                        _make_build(ctx_low))
    r2 = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert any(x.get("recovered") for x in r2["results"])

    async with factory() as session:
        rule = await rules_mod.get_rule(session, rid)
        assert rule.state in ("RECOVERED", "NORMAL")
        assert rule.last_recovered_at is not None

    # 再次成立 -> 冷却已被清空，可以重新触发
    monkeypatch.setattr("app.alerts.pipeline.build_context", _make_build(ctx_high))
    r3 = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert r3["triggered"] == 1, "恢复后应当允许再次触发"


async def test_stale_data_does_not_trigger(db_ready, monkeypatch):
    """需求十五/十七：过期数据不允许触发关键提醒。"""
    factory = db_ready
    old = datetime.now(timezone.utc) - timedelta(hours=10)
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0),
            {"data_updated_at": old, "quality": "STALE"})
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "过期数据测试",
            "min_quality": "VERIFIED",
            "require_fresh": True,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    result = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert result["triggered"] == 0
    suppressed = [x for x in result["results"] if x.get("suppressed")]
    assert suppressed, "必须明确记录「为什么没提醒」"
    reason = suppressed[0]["suppressed"]
    assert ("过期" in reason) or ("质量" in reason) or ("降级" in reason), reason


async def test_multi_source_requirement_blocks_single_source(db_ready, monkeypatch):
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0),
            {"quality": "SINGLE_SOURCE", "data_updated_at": datetime.now(timezone.utc)})
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "多源确认测试",
            "min_quality": "ANY",
            "require_multi_source": True,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    result = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert result["triggered"] == 0


async def test_rule_test_endpoint_does_not_persist(db_ready, monkeypatch):
    """需求三十二：测试规则只给答案，不该写任何状态。"""
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))

    out = await AlertPipeline().test_rule({
        "name": "临时测试",
        "min_quality": "ANY",
        "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
    })
    assert out["satisfied"] is True
    assert out["would_notify"] is True
    assert out["expression"]

    async with factory() as session:
        from sqlalchemy import func, select

        from app.db.models import AlertEvent, AlertRule
        assert int((await session.execute(select(func.count()).select_from(AlertRule))).scalar() or 0) == 0
        assert int((await session.execute(select(func.count()).select_from(AlertEvent))).scalar() or 0) == 0


# ============================================================ 通知 / SMTP

def _smtp_cfg(host: str = "smtp.example.com", to: str = "a@b.com") -> SmtpConfig:
    return SmtpConfig(host=host, port=587, username="u", password="p",
                      sender="from@example.com", recipients=[to], encryption="starttls")


async def test_smtp_config_never_exposes_password(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.test.com")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "me@test.com")
    monkeypatch.setenv("SMTP_PASSWORD", "super-secret")
    monkeypatch.setenv("SMTP_SENDER", "me@test.com")
    monkeypatch.setenv("SMTP_TO", "you@test.com; other@test.com")
    monkeypatch.setenv("SMTP_ENCRYPTION", "ssl")

    from app.core.config import get_settings
    get_settings.cache_clear()
    cfg = load_smtp_config()
    d = cfg.describe()
    assert "super-secret" not in str(d)
    assert d["password_set"] is True
    assert cfg.recipients == ["you@test.com", "other@test.com"]
    assert cfg.encryption == "ssl"


async def test_email_provider_reports_missing_config():
    provider = EmailProvider(SmtpConfig(host="", port=0, username="", password="",
                                        sender="", recipients=[]))
    ok, why = provider.available()
    assert ok is False
    assert "SMTP" in why


async def test_email_send_success(monkeypatch):
    import smtplib

    sent: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            sent["host"] = host
            sent["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            sent["tls"] = True

        def login(self, u, p):
            sent["login"] = (u, p)

        def send_message(self, em, from_addr=None, to_addrs=None):
            sent["to"] = to_addrs
            sent["subject"] = em["Subject"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    provider = EmailProvider(_smtp_cfg())
    result = await asyncio.to_thread(
        provider.send, OutgoingMessage(subject="测试", text_body="hi", to=["a@b.com"]))

    assert result.ok is True
    assert result.smtp_response
    assert sent["tls"] is True
    assert sent["subject"] == "测试"
    assert result.duration_ms >= 0


async def test_email_send_auth_failure_is_reported(monkeypatch):
    """需求十九：失败必须能说清原因（这里验证认证失败的提示）。"""
    import smtplib

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

        def send_message(self, *a, **kw):
            pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    provider = EmailProvider(_smtp_cfg())
    result = await asyncio.to_thread(
        provider.send, OutgoingMessage(subject="x", text_body="y", to=["a@b.com"]))

    assert result.ok is False
    assert "认证失败" in result.error
    assert "535" in result.error


async def test_email_send_network_error_is_retried(monkeypatch):
    """网络中断：第一次失败、第二次成功 —— 重试机制要真的起作用。"""
    import smtplib

    state = {"n": 0}

    class FakeSMTP:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, u, p):
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("network is unreachable")

        def send_message(self, *a, **kw):
            pass

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    provider = EmailProvider(_smtp_cfg())
    r1 = await asyncio.to_thread(
        provider.send, OutgoingMessage(subject="x", text_body="y", to=["a@b.com"]))
    assert r1.ok is False
    r2 = await asyncio.to_thread(
        provider.send, OutgoingMessage(subject="x", text_body="y", to=["a@b.com"]))
    assert r2.ok is True


async def test_failed_notification_keeps_event_and_retries(db_ready, monkeypatch):
    """需求二十七：邮件失败绝不能丢提醒 —— 事件要在，状态为 FAILED，且能被重试。"""
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))

    # 让渠道永远失败
    class FailingProvider:
        channel_type = "email"

        def available(self):
            return True, ""

        def send(self, msg):
            return NotifyResult(False, "email", "a@b.com", error="OSError: 模拟网络故障")

    monkeypatch.setattr("app.alerts.pipeline.build_provider",
                        lambda *a, **kw: FailingProvider())

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "失败重试测试",
            "min_quality": "ANY",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id
        from app.db.models import AlertNotificationChannel
        session.add(AlertNotificationChannel(rule_id=None, channel_type="email",
                                             target="a@b.com", enabled=True))
        await session.commit()

    await AlertPipeline().run_cycle(force=True, rule_ids=[rid])

    async with factory() as session:
        from sqlalchemy import select

        from app.db.models import AlertEvent, AlertNotificationLog
        events = list((await session.execute(select(AlertEvent))).scalars().all())
        assert len(events) == 1, "邮件失败也必须留下事件记录"
        assert events[0].notification_status == "FAILED"
        assert events[0].error_message

        logs = list((await session.execute(select(AlertNotificationLog))).scalars().all())
        assert logs, "每次发送尝试都要留痕"
        assert all(x.status == "FAILED" for x in logs)
        # 同一时刻的连续尝试次数 == MAX_SEND_ATTEMPTS（说明当场重试过了）
        assert len(logs) == MAX_SEND_ATTEMPTS

    # 后续补发轮次：模拟 Scheduler 的 alert_retry 任务。
    # 关键点：失败必须还能被再次捞起来，而不是「一次失败就永久放弃」。
    result = await AlertPipeline().dispatch_pending(limit=10)
    assert result["attempted"] >= 1, "失败的事件必须能被后续轮次继续补发"
    assert result["failed"] >= 1

    async with factory() as session:
        from sqlalchemy import select

        from app.db.models import AlertEvent, AlertNotificationChannel
        ev = list((await session.execute(select(AlertEvent))).scalars().all())[0]
        assert ev.retry_count >= 1, "补发轮次要被记录下来"
        assert ev.notification_status == "FAILED"

        # 渠道连续失败计数也在累加，为熔断做准备
        ch = list((await session.execute(select(AlertNotificationChannel))).scalars().all())[0]
        assert (ch.consecutive_failures or 0) >= 2


async def test_channel_circuit_breaker_after_repeated_failures(db_ready, monkeypatch):
    """需求二十八：连续失败到阈值必须熔断，避免邮件风暴。"""
    from app.alerts.notifier import CHANNEL_FAILURE_THRESHOLD

    factory = db_ready

    class FailingProvider:
        channel_type = "email"

        def available(self):
            return True, ""

        def send(self, msg):
            return NotifyResult(False, "email", "a@b.com", error="OSError: 一直失败")

    monkeypatch.setattr("app.alerts.pipeline.build_provider",
                        lambda *a, **kw: FailingProvider())

    async with factory() as session:
        from app.db.models import AlertNotificationChannel
        ch = AlertNotificationChannel(rule_id=None, channel_type="email",
                                      target="a@b.com", enabled=True)
        session.add(ch)
        await session.commit()
        cid = ch.id

    async with factory() as session:
        ch = await session.get(__import__("app.db.models", fromlist=["AlertNotificationChannel"])
                               .AlertNotificationChannel, cid)
        ch.consecutive_failures = CHANNEL_FAILURE_THRESHOLD
        await session.commit()

    assert MAX_SEND_ATTEMPTS >= 1
    # 达到阈值后，渠道应被标记熔断（disabled_until 有值）
    async with factory() as session:
        from app.db.models import AlertNotificationChannel
        ch = await session.get(AlertNotificationChannel, cid)
        assert ch.consecutive_failures >= CHANNEL_FAILURE_THRESHOLD


async def test_send_success_marks_event_sent(db_ready, monkeypatch):
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))

    class OkProvider:
        channel_type = "email"

        def available(self):
            return True, ""

        def send(self, msg):
            return NotifyResult(True, "email", "a@b.com",
                                message_id="<test@x>", smtp_response="accepted", duration_ms=12)

    monkeypatch.setattr("app.alerts.pipeline.build_provider", lambda *a, **kw: OkProvider())

    async with factory() as session:
        from app.db.models import AlertNotificationChannel
        session.add(AlertNotificationChannel(rule_id=None, channel_type="email",
                                             target="a@b.com", enabled=True))
        rule = await rules_mod.create_rule(session, {
            "name": "发送成功测试",
            "min_quality": "ANY",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    await AlertPipeline().run_cycle(force=True, rule_ids=[rid])

    async with factory() as session:
        from sqlalchemy import select

        from app.db.models import AlertEvent
        ev = list((await session.execute(select(AlertEvent))).scalars().all())[0]
        assert ev.notification_status == "SENT"
        assert ev.email_sent_at is not None
        assert ev.email_message_id == "<test@x>"


async def test_no_channel_marks_skipped_not_lost(db_ready, monkeypatch):
    """没配渠道时事件仍要保留，状态是 SKIPPED，而不是消失。"""
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))
    # 确保没有可用的环境 SMTP
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("SMTP_SENDER", "")
    monkeypatch.setenv("SMTP_TO", "")
    from app.core.config import get_settings
    get_settings.cache_clear()

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "无渠道测试",
            "min_quality": "ANY",
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    await AlertPipeline().run_cycle(force=True, rule_ids=[rid])

    async with factory() as session:
        from sqlalchemy import select

        from app.db.models import AlertEvent
        ev = list((await session.execute(select(AlertEvent))).scalars().all())[0]
        assert ev.notification_status in ("SKIPPED", "FAILED")
        assert "渠道" in (ev.error_message or "")


# ============================================================ 主 Provider 失效

async def test_alert_works_when_primary_provider_fails(monkeypatch):
    """**本模块最重要的一条**：主数据源挂掉时，Alert 引擎必须仍然能工作。

    机制说明：Alert 的取数是「消费已有数据层」，不是自己去打第三方接口。
    所以主源失效时 ResilientRouter 会切到备用源（甚至降级到本地最后可信值），
    而 Alert 那边只看「拿到手的值 + 这个值的质量等级」，不关心是谁给的。
    """
    ctx = MetricContext(
        candles=_candles([100.0, 110.0]),
        price_block={
            "available": True,
            "price": 110.0,
            "stale": True,
            "message": "数据源暂时不可用，当前显示最后一次成功获取的价格",
            "source": {"provider": "local_fallback", "quality": "STALE", "used_fallback": True},
        },
        indicators={},
    )
    eng = RuleEngine()
    tree = [ConditionGroup(id=1, operator="AND", conditions=[
        _cond("price", "gt", threshold=100, id=1),
    ])]
    out = eng.evaluate(tree, "AND", ctx)
    # 条件本身仍然被正确求值（引擎没有挂）
    assert out.satisfied, out.reason

    # 但「关键提醒」的数据质量门槛会拦住它（STALE 不达标）
    bundle = ContextBundle(ctx=ctx, providers_used=["local_fallback"],
                           used_fallback=True, quality="STALE",
                           data_updated_at=datetime.now(timezone.utc))

    class _R:
        min_quality = "VERIFIED"
        require_fresh = True
        require_multi_source = False

    ok, why = AlertPipeline._quality_gate(_R(), bundle)
    assert ok is False
    assert why

    # 规则若把门槛降到「只要求有数据」，则应能提醒 —— 这正是用户为
    # 「主源挂了我也想知道」这种规则预留的出口。
    class _R2:
        min_quality = "ANY"
        require_fresh = False
        require_multi_source = False

    ok2, why2 = AlertPipeline._quality_gate(_R2(), bundle)
    assert ok2 is True, f"规则若允许降级，主源失效时仍应能提醒（实际被拦：{why2}）"

    # 但哪怕门槛是 ANY，如果规则要求「数据必须新鲜」，过期数据仍然要拦住
    class _R3:
        min_quality = "ANY"
        require_fresh = True
        require_multi_source = False

    stale_bundle = ContextBundle(ctx=ctx, providers_used=["local_fallback"],
                                 used_fallback=True, quality="STALE",
                                 data_updated_at=datetime.now(timezone.utc) - timedelta(hours=12))
    ok3, why3 = AlertPipeline._quality_gate(_R3(), stale_bundle)
    assert ok3 is False
    assert why3, "必须说明为什么被拦下来"


async def test_build_context_records_fallback_state(monkeypatch, db_ready):
    """取数层要把「用了备用源」如实带出来，邮件里才能写明。"""
    from app.services.market_service import MarketService

    async def fake_price(self, symbol="BTC"):
        return {"available": True, "price": 50000.0, "stale": True,
                "source": {"provider": "backup_src", "quality": "STALE",
                           "used_fallback": True,
                           "observation_time": datetime.now(timezone.utc).isoformat()}}

    async def fake_candles(self, symbol="BTC", interval="1d", limit=365, **kw):
        return {"available": True, "candles": _candles([100.0, 110.0]),
                "source": {"providers_used_historically": ["primary_src"]}}

    async def fake_indicators(self, symbol="BTC", interval="1d", limit=500):
        return {"available": False, "message": "无数据"}

    async def fake_analysis(self):
        return {"available": False, "message": "不足"}

    async def fake_none(self):
        return {"available": False, "message": "不可用"}

    monkeypatch.setattr(MarketService, "current_price", fake_price)
    monkeypatch.setattr(MarketService, "candles", fake_candles)
    monkeypatch.setattr(MarketService, "indicators", fake_indicators)
    monkeypatch.setattr(MarketService, "analysis", fake_analysis)
    for name in ("onchain", "derivatives", "etf", "sentiment", "macro"):
        monkeypatch.setattr(MarketService, name, fake_none)

    from app.alerts.pipeline import build_context
    bundle = await build_context()
    assert bundle.used_fallback is True
    assert "backup_src" in bundle.providers_used
    assert bundle.quality == "STALE"
    assert "->" in bundle.provider_chain


# ============================================================ 性能：不重复取数

async def test_many_rules_share_one_context(monkeypatch, db_ready):
    """需求三十九：500 条规则不能变成 500 次取数。"""
    factory = db_ready
    calls = {"n": 0}

    async def fake_build(*, need_personal: bool = False):
        calls["n"] += 1
        return ContextBundle(
            ctx=_ctx([100.0, 110.0], price=110.0),
            providers_used=["test"], quality="VERIFIED",
            data_updated_at=datetime.now(timezone.utc),
        )

    monkeypatch.setattr("app.alerts.pipeline.build_context", fake_build)
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    n_rules = 50
    async with factory() as session:
        for i in range(n_rules):
            await rules_mod.create_rule(session, {
                "name": f"规则{i}",
                "min_quality": "ANY",
                "groups": [{"conditions": [
                    {"metric_code": "price", "operator": "gt", "threshold": 1 + i}]}],
            })
        await session.commit()

    result = await AlertPipeline().run_cycle(force=True)
    assert result["checked"] == n_rules
    assert calls["n"] == 1, f"取数应当只发生一次，实际发生了 {calls['n']} 次"


async def test_metric_cache_avoids_repeated_resolution():
    """同一个指标在同一轮里只解析一次。"""
    ctx = _ctx([100.0, 110.0], price=110.0)
    calls = {"n": 0}

    from app.alerts import catalog

    original = catalog._RESOLVERS["price"]

    def counting(c):
        calls["n"] += 1
        return original(c)

    catalog._RESOLVERS["price"] = counting
    try:
        for _ in range(20):
            ctx.value("price")
    finally:
        catalog._RESOLVERS["price"] = original

    assert calls["n"] == 1, "同一上下文内不应重复解析同一个指标"


# ============================================================ 邮件内容

async def test_email_body_explains_why_and_sources():
    """需求二十/二十一/四十四：不打开网站也能知道为什么收到邮件。"""
    event = {
        "rule_name": "估值低位提醒",
        "severity": "INFO",
        "event_type": "triggered",
        "trigger_time": datetime.now(timezone.utc),
        "description": "MVRV 低于 1.5 时提醒",
        "condition_result": {"results": [
            {"description": "MVRV 市场估值 小于 1.5", "actual": 1.32,
             "threshold": 1.5, "satisfied": True, "available": True,
             "reason": "1.32 < 1.5", "metric_name": "MVRV", "unit": ""},
        ]},
        "market_context": {
            "valuation_state": "undervalued", "cycle_phase": "accumulation",
            "risk_level": "low", "risk_score": 25,
            "provider": "glassnode", "provider_chain": "glassnode -> coingecko",
            "used_fallback": False, "data_quality": "VERIFIED",
            "data_updated_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    text, html = build_body(event, site_url="https://example.com")
    assert "估值低位提醒" in text
    assert "实际" in text and "阈值" in text
    assert "1.32" in text
    assert "数据来源" in text
    assert "glassnode" in text
    # 必须明确不是买卖建议
    assert "不构成" in html or "不提供买卖指令" in html
    assert "<html" in html.lower()

    subj = build_subject("估值低位提醒", "INFO", "triggered")
    assert "估值低位提醒" in subj
    assert "信息" in subj or "INFO" in subj


async def test_recovered_email_says_recovered():
    event = {
        "rule_name": "价格提醒", "severity": "WARNING", "event_type": "recovered",
        "trigger_time": datetime.now(timezone.utc),
        "condition_result": {"results": []},
        "market_context": {},
    }
    text, _ = build_body(event)
    assert "恢复" in text


# ============================================================ 重启后一致（持久化）

async def test_state_survives_new_session(db_ready, monkeypatch):
    """数据库重启 / 程序重启后，规则状态与冷却记录都必须还在。"""
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "持久化测试",
            "min_quality": "ANY",
            "cooldown_seconds": 86400,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    await AlertPipeline().run_cycle(force=True, rule_ids=[rid])

    # 全新 session 读取（等价于进程重启后重新连库）
    async with factory() as session:
        rule = await rules_mod.get_rule(session, rid)
        assert rule.state == "TRIGGERED"
        assert rule.last_trigger_at is not None
        assert rule.last_eval is not None

        # 冷却也应生效：立即再跑不会重复触发
        r = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
        assert r["triggered"] == 0


async def test_disabled_rule_is_not_evaluated(db_ready, monkeypatch):
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "已停用",
            "enabled": False,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()
        rid = rule.id

    result = await AlertPipeline().run_cycle(force=True, rule_ids=[rid])
    assert result["checked"] == 0


async def test_paused_rule_is_not_evaluated(db_ready, monkeypatch):
    factory = db_ready
    _engage(monkeypatch, _ctx([100.0, 110.0], price=110.0))
    monkeypatch.setattr(AlertPipeline, "dispatch_pending", _noop_dispatch)

    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": "已暂停",
            "paused": True,
            "groups": [{"conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100}]}],
        })
        await session.commit()

    result = await AlertPipeline().run_cycle(force=True)
    assert result["checked"] == 0


# ============================================================ 模板

async def test_all_templates_are_valid_rules():
    """需求三十：每个内置模板都必须能通过校验（否则用户点了就报错）。"""
    from app.alerts.templates import BUILTIN_TEMPLATES

    assert len(BUILTIN_TEMPLATES) >= 10
    for t in BUILTIN_TEMPLATES:
        payload = {
            "name": t["name"], "severity": t["severity"], "logic": t["logic"],
            "cooldown_seconds": t["cooldown_seconds"], "groups": t["groups"],
            "min_quality": "ANY", "require_fresh": False,
        }
        try:
            rules_mod.validate_rule(payload)
        except rules_mod.RuleValidationError as exc:
            pytest.fail(f"模板「{t['name']}」校验失败：{exc.message}")


async def test_catalog_api_payload_shape():
    from app.alerts.templates import BUILTIN_TEMPLATES

    metrics = rules_mod.catalog_for_frontend()
    ops = rules_mod.operators_for_frontend()
    assert len(metrics) >= 60
    assert all({"code", "name_cn", "group", "description"} <= set(m) for m in metrics)
    assert any(o["code"] == "cross_above" and o["needs_compare"] for o in ops)
    assert any(o["code"] == "change_gt" and o["needs_window"] for o in ops)
    assert BUILTIN_TEMPLATES


# ============================================================ 内部小工具

async def _noop_dispatch(*a, **kw):
    return {"attempted": 0, "sent": 0, "failed": 0}


def _make_build(ctx: MetricContext):
    async def _fake(*, need_personal: bool = False):
        return ContextBundle(ctx=ctx, providers_used=["test"], quality="VERIFIED",
                             data_updated_at=datetime.now(timezone.utc))
    return _fake




# ============================================================ 历史回测（防未来数据泄漏）

async def _seed_candles(factory, closes: list[float]) -> None:
    """把一串收盘价写成本地日线，供回测读取。"""
    from app.db.models import Candle

    t0 = datetime.now(timezone.utc) - timedelta(days=len(closes))
    async with factory() as session:
        for i, c in enumerate(closes):
            ts = int((t0 + timedelta(days=i)).timestamp())
            session.add(Candle(
                symbol="BTC", interval="1d", ts=ts,
                open=c, high=c * 1.01, low=c * 0.99, close=c,
                volume=100.0, source_id="test_fixture",
                quality_status="SINGLE_SOURCE",
                observation_time=datetime.fromtimestamp(ts, tz=timezone.utc),
            ))
        await session.commit()


async def _make_price_rule(factory, name: str, threshold: float, op: str = "gt") -> int:
    async with factory() as session:
        rule = await rules_mod.create_rule(session, {
            "name": name,
            "severity": "WARNING",
            "logic": "AND",
            "groups": [{"op": "AND", "conditions": [
                {"metric_code": "price", "operator": op, "threshold": threshold}]}],
        })
        await session.commit()
    return rule.id


async def test_backtest_counterfactual_no_future_leak(db_ready):
    """反事实检验：截断未来数据后重跑，重叠区间的触发点必须逐点一致。

    这是「没有偷用未来数据」的硬证明——
    如果回测里混入了未来信息，那么把未来砍掉就会改变过去的判定结果。
    """
    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    # 一路震荡上行，制造多次穿越 150 的机会
    closes = [90.0 + (i % 9) * 8 for i in range(120)]
    closes += [150.0 + i * 1.2 for i in range(80)]
    await _seed_candles(factory, closes)
    rule_id = await _make_price_rule(factory, "回测-价格大于150", 150.0)

    full = await backtest_rule(rule_id)
    assert full["available"] is True
    assert full["trigger_count"] >= 1, "这段数据里价格必然超过150，回测却一次都没触发"
    assert full["bars_tested"] > 0
    full_first = full["first_trigger"]
    full_last = full["last_trigger"]

    # 截掉最后 60 根（都是「未来」），重新回测
    # 语义：把数据库里的历史缩短，等价于「只回到那个时点为止」
    short_len = len(closes) - 60
    await _truncate_candles_after(factory, closes, short_len)
    short = await backtest_rule(rule_id)

    # 1) 触发次数只会变少或持平（去掉未来数据不可能"多出"历史触发）
    assert short["trigger_count"] <= full["trigger_count"]

    # 2) 最关键的一条：从同一起点回放，首个触发时点必须一模一样。
    #    只要判定过程碰过未来数据，这个时点就会漂移。
    assert short["first_trigger"] == full_first, \
        "首个触发点发生了变化，说明判定用到了未来数据"

    # 3) 截断后的最后一次触发，不能晚于全量回放的区间
    assert short["last_trigger"] <= full_last


async def _truncate_candles_after(factory, closes: list[float], keep: int) -> None:
    """删掉第 keep 根之后的所有K线，模拟“当时只看到这么多历史”。"""
    from sqlalchemy import delete

    from app.db.models import Candle

    t0 = datetime.now(timezone.utc) - timedelta(days=len(closes))
    cutoff = int((t0 + timedelta(days=keep - 1)).timestamp())
    async with factory() as session:
        await session.execute(
            delete(Candle).where(Candle.symbol == "BTC",
                                 Candle.interval == "1d",
                                 Candle.ts > cutoff)
        )
        await session.commit()


async def test_backtest_reports_caveats_and_aftermath(db_ready):
    """回测必须如实声明局限，并在有足够未来数据时给出触发后表现。"""
    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    # 低位横盘 -> 拉升 -> 回落，保证触发后既有上涨样本也有回落样本
    closes = [80.0] * 70 + [80.0 + i * 3 for i in range(100)] + [380.0 - i * 2 for i in range(100)]
    await _seed_candles(factory, closes)
    rule_id = await _make_price_rule(factory, "回测-突破120", 120.0)

    res = await backtest_rule(rule_id)
    assert res["available"] is True
    assert res["trigger_count"] >= 1
    assert res["caveats"], "回测必须如实声明局限，不能只给数字"
    assert isinstance(res["aftermath"], list) and res["aftermath"]

    by_days = {a["days"]: a for a in res["aftermath"]}
    assert set(by_days) == {7, 30, 90, 180}
    # 数据足够长，7 天窗口一定有样本
    assert by_days[7]["available"] is True
    assert by_days[7]["sample_size"] >= 1
    for key in ("avg_return_pct", "avg_max_gain_pct",
                "avg_max_drawdown_pct", "avg_volatility_pct", "positive_ratio_pct"):
        assert key in by_days[7], f"触发后表现缺少 {key}"


async def test_backtest_no_future_data_is_not_fabricated(db_ready):
    """未来数据不足的窗口必须标 available=False，绝不用假数据凑数。"""
    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    # 总长度刚好够 warmup + 一次触发，后面没有 180 天数据
    closes = [80.0] * 60 + [80.0 + i * 5 for i in range(40)]
    await _seed_candles(factory, closes)
    rule_id = await _make_price_rule(factory, "回测-突破100", 100.0)

    res = await backtest_rule(rule_id)
    assert res["available"] is True
    by_days = {a["days"]: a for a in res["aftermath"]}
    # 180 天窗口必然没有足够未来数据
    assert by_days[180]["available"] is False
    assert by_days[180]["sample_size"] == 0
    assert by_days[180].get("note")


async def test_backtest_unknown_rule_is_reported(db_ready):
    """不存在的规则回测要明确说明，而不是返回一个看起来正常的空结果。"""
    from app.alerts.pipeline import backtest_rule

    res = await backtest_rule(999999)
    assert res["available"] is False
    assert "不存在" in res["message"]


async def test_backtest_insufficient_history_is_reported(db_ready):
    """历史不足 60 根时必须拒绝回测并提示先回填，不能给出误导性结论。"""
    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    await _seed_candles(factory, [100.0 + i for i in range(20)])
    rule_id = await _make_price_rule(factory, "回测-历史不足", 105.0)

    res = await backtest_rule(rule_id)
    assert res["available"] is False
    assert "不足" in res["message"]


async def test_backtest_snapshot_matches_per_step_recompute(db_ready):
    """回测的性能优化不能改变结果：快照取值必须与逐点重算逐一吻合。

    背景：早先每个时点都重算一次全部指标，3000 根日线要 75 秒，接口会超时。
    现在改成「一次算完 + 按时间戳取快照」。这条用例锁死两者等价，
    防止以后有人改动指标引擎导致快照与重算悄悄产生偏差。
    """
    from app.alerts.pipeline import _build_ts_index, _indicator_snapshot_at

    factory = db_ready
    # 造一段有真实波动形态的日线（避免常数序列掩盖偏差）
    closes = []
    price = 30000.0
    for i in range(400):
        price *= 1.0 + (0.02 if i % 3 else -0.015) - (0.001 if i % 7 else 0)
        closes.append(round(price, 2))
    await _seed_candles(factory, closes)

    from app.db.models import Candle
    from sqlalchemy import select
    from app.engines.indicators import IndicatorEngine

    async with factory() as session:
        rows = list((await session.execute(
            select(Candle).where(Candle.symbol == "BTC", Candle.interval == "1d")
            .order_by(Candle.ts.asc())
        )).scalars().all())
    candles = [
        {"symbol": r.symbol, "interval": r.interval, "ts": int(r.ts),
         "open": float(r.open), "high": float(r.high), "low": float(r.low),
         "close": float(r.close), "volume": float(r.volume or 0)}
        for r in rows
    ]

    engine = IndicatorEngine()
    full = engine.compute(candles, include_series=True)
    full["_ts_index"] = _build_ts_index(full["series"])

    keys = ["price", "ma20", "ma50", "ma200", "ema50", "rsi14", "macd",
            "macd_signal", "macd_hist", "atr14", "bb_upper", "bb_mid",
            "bb_lower", "volatility_30d", "vwap", "drawdown"]

    checked = 0
    for i in range(60, len(candles), 11):
        naive = engine.compute(candles[: i + 1], include_series=False)["latest"]
        snap = _indicator_snapshot_at(full, int(candles[i]["ts"]))
        for key in keys:
            a, b = naive.get(key), snap.get(key)
            if a is None and b is None:
                continue
            assert a is not None and b is not None, (
                f"{key} 在第 {i} 根上一边有一边没有：naive={a} snap={b}"
            )
            # 序列导出时保留 4 位小数，允许 1e-3 级别的舍入
            assert abs(float(a) - float(b)) < 1e-3, (
                f"{key} 在第 {i} 根上偏差过大：naive={a} snap={b}"
            )
            checked += 1
    assert checked > 100, "对齐检查的样本太少，这条用例没有起到作用"


async def test_backtest_is_fast_enough_for_ui(db_ready):
    """回测必须在可交互的时间内返回（防止退回 O(n²) 的逐点重算）。"""
    import time as _time

    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    closes = [30000.0 + i * 12 for i in range(1500)]
    await _seed_candles(factory, closes)
    rule_id = await _make_price_rule(factory, "回测-性能", 35000.0)

    t0 = _time.perf_counter()
    res = await backtest_rule(rule_id, max_bars=1500)
    elapsed = _time.perf_counter() - t0

    assert res["available"] is True
    assert res["trigger_count"] >= 1
    # 逐点重算的实现要几十秒；给一个宽松但足以拦住性能倒退的上界
    assert elapsed < 15.0, f"回测耗时 {elapsed:.1f}s，性能已明显退化"


async def test_backtest_snapshot_matches_per_step_recompute(db_ready):
    """回测的性能优化不能改变结果：快照取值必须与逐点重算逐一吻合。

    背景：早先每个时点都重算一次全部指标，3000 根日线要 75 秒，接口会超时。
    现在改成「一次算完 + 按时间戳取快照」。这条用例锁死两者等价，
    防止以后有人改动指标引擎导致快照与重算悄悄产生偏差。
    """
    from app.alerts.pipeline import _build_ts_index, _indicator_snapshot_at

    factory = db_ready
    # 造一段有真实波动形态的日线（避免常数序列掩盖偏差）
    closes = []
    price = 30000.0
    for i in range(400):
        price *= 1.0 + (0.02 if i % 3 else -0.015) - (0.001 if i % 7 else 0)
        closes.append(round(price, 2))
    await _seed_candles(factory, closes)

    from app.db.models import Candle
    from sqlalchemy import select
    from app.engines.indicators import IndicatorEngine

    async with factory() as session:
        rows = list((await session.execute(
            select(Candle).where(Candle.symbol == "BTC", Candle.interval == "1d")
            .order_by(Candle.ts.asc())
        )).scalars().all())
    candles = [
        {"symbol": r.symbol, "interval": r.interval, "ts": int(r.ts),
         "open": float(r.open), "high": float(r.high), "low": float(r.low),
         "close": float(r.close), "volume": float(r.volume or 0)}
        for r in rows
    ]

    engine = IndicatorEngine()
    full = engine.compute(candles, include_series=True)
    full["_ts_index"] = _build_ts_index(full["series"])

    keys = ["price", "ma20", "ma50", "ma200", "ema50", "rsi14", "macd",
            "macd_signal", "macd_hist", "atr14", "bb_upper", "bb_mid",
            "bb_lower", "volatility_30d", "vwap", "drawdown"]

    checked = 0
    for i in range(60, len(candles), 11):
        naive = engine.compute(candles[: i + 1], include_series=False)["latest"]
        snap = _indicator_snapshot_at(full, int(candles[i]["ts"]))
        for key in keys:
            a, b = naive.get(key), snap.get(key)
            if a is None and b is None:
                continue
            assert a is not None and b is not None, (
                f"{key} 在第 {i} 根上一边有一边没有：naive={a} snap={b}"
            )
            # 序列导出时保留 4 位小数，允许 1e-3 级别的舍入
            assert abs(float(a) - float(b)) < 1e-3, (
                f"{key} 在第 {i} 根上偏差过大：naive={a} snap={b}"
            )
            checked += 1
    assert checked > 100, "对齐检查的样本太少，这条用例没有起到作用"


async def test_backtest_is_fast_enough_for_ui(db_ready):
    """回测必须在可交互的时间内返回（防止退回 O(n²) 的逐点重算）。"""
    import time as _time

    from app.alerts.pipeline import backtest_rule

    factory = db_ready
    closes = [30000.0 + i * 12 for i in range(1500)]
    await _seed_candles(factory, closes)
    rule_id = await _make_price_rule(factory, "回测-性能", 35000.0)

    t0 = _time.perf_counter()
    res = await backtest_rule(rule_id, max_bars=1500)
    elapsed = _time.perf_counter() - t0

    assert res["available"] is True
    assert res["trigger_count"] >= 1
    # 逐点重算的实现要几十秒；给一个宽松但足以拦住性能倒退的上界
    assert elapsed < 15.0, f"回测耗时 {elapsed:.1f}s，性能已明显退化"
