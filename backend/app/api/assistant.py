# -*- coding: utf-8 -*-
"""AI 金融解释助手。

职责边界：
- **解释**：解释数据、指标、模型、历史、回测、当前市场状态；
- **禁止**：编造数据、给出确定性买卖指令、承诺收益。

没有配置 LLM Key 时，使用「基于真实数据的规则解释引擎」回答，
并且明确告知这是规则生成的结果，不假装有模型在思考。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter

from ..core.config import get_settings
from ..core.logging import get_logger
from ..db.base import get_session_factory
from ..db.models import IndicatorDefinition
from ..services import MarketService
from .deps import RequestIdDep
from .schemas import AssistantAskRequest

router = APIRouter()
logger = get_logger(__name__)
settings = get_settings()


async def _build_context() -> dict[str, Any]:
    """收集回答所需的真实上下文（全部带来源）。"""
    service = MarketService()
    ctx: dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        ctx["price"] = await service.current_price()
        ctx["indicators"] = await service.indicators(limit=500)
        ctx["analysis"] = await service.analysis()
        ctx["sentiment"] = await service.sentiment()
        ctx["derivatives"] = await service.derivatives()
    except Exception as exc:  # noqa: BLE001 - 上下文部分缺失不应导致助手不可用
        logger.event("assistant.context_error", error=str(exc)[:200])
    return ctx


def _rule_based_answer(question: str, ctx: dict[str, Any]) -> dict[str, Any]:
    """基于真实数据的规则解释（不编造）。"""
    q = question.strip()
    price_block = ctx.get("price") or {}
    analysis = (ctx.get("analysis") or {}).get("data", ctx.get("analysis")) or {}
    indicators = ctx.get("indicators") or {}
    latest = indicators.get("latest", {})
    risk = analysis.get("risk", {}) if isinstance(analysis, dict) else {}
    valuation = analysis.get("valuation", {}) if isinstance(analysis, dict) else {}
    cycle = analysis.get("cycle", {}) if isinstance(analysis, dict) else {}
    regime = analysis.get("regime", {}) if isinstance(analysis, dict) else {}

    answer_parts: list[str] = []
    facts: list[dict[str, Any]] = []
    judgments: list[str] = []
    counter_evidence: list[str] = []
    uncertainties: list[str] = []

    price = price_block.get("price")
    if price is not None:
        facts.append(
            {
                "label": "当前价格",
                "value": price,
                "source": (price_block.get("source") or {}).get("provider", "unknown"),
                "status": "STALE（数据源暂时不可用，使用最后一次可信数据）" if price_block.get("stale")
                else "实时",
            }
        )
        answer_parts.append(f"当前 BTC 价格约 {price:,.0f} 美元。")

    # ---- 风险相关问题
    if any(k in q for k in ("风险", "为什么风险", "risk", "危险")):
        if risk:
            answer_parts.append(
                f"系统当前综合风险评分 {risk.get('overall_risk')}（等级：{risk.get('level_cn')}）。"
            )
            dims = risk.get("dimensions", {})
            for code, dim in dims.items():
                if not dim.get("available"):
                    uncertainties.append(f"{code} 维度数据不可用，未参与加权")
                    continue
                facts.append({"label": f"风险维度 {code}", "value": dim.get("score"),
                              "detail": "；".join(dim.get("reasons", []))})
            for dim in dims.values():
                counter_evidence.extend(dim.get("reasons", [])[:1])
        else:
            answer_parts.append("当前无法计算完整风险评分（历史数据或依赖模块不足）。")

    # ---- 估值
    if any(k in q for k in ("估值", "贵", "便宜", "valuation")):
        if valuation:
            answer_parts.append(
                f"估值状态：{valuation.get('state_cn')}（评分 {valuation.get('score')}/100，置信度 {valuation.get('confidence')}）。"
            )
            for item in valuation.get("contributions", []):
                facts.append({"label": item["factor"], "value": item["value"], "detail": item["explain"]})
            if valuation.get("missing_factors"):
                uncertainties.extend(valuation["missing_factors"])
            judgments.append("估值描述的是「相对历史的位置」，不是涨跌预测。")

    # ---- 周期
    if any(k in q for k in ("周期", "阶段", "cycle", "现在什么阶段")):
        if cycle:
            answer_parts.append(
                f"系统判断当前处于「{cycle.get('phase_cn')}」，置信度 {cycle.get('confidence')}。"
            )
            answer_parts.append(cycle.get("advice", ""))
            for item in cycle.get("evidence", [])[:6]:
                facts.append({"label": f"{item['dim']}（支持）", "value": item["point"]})
            for item in cycle.get("counter_evidence", [])[:6]:
                counter_evidence.append(item["point"])

    # ---- 综合状态
    if any(k in q for k in ("市场状态", "综合", "现在怎么样", "regime", "行情")) or not answer_parts:
        if regime:
            answer_parts.append(f"综合市场状态：{regime.get('regime')}。{regime.get('summary_cn', '')}")
            if regime.get("unavailable_modules"):
                uncertainties.append("以下模块数据不可用：" + "、".join(regime["unavailable_modules"]))
        for key, label in (("change_24h", "24 小时"), ("change_7d", "7 天"), ("change_30d", "30 天")):
            if latest.get(key) is not None:
                facts.append({"label": f"{label}涨跌", "value": f"{latest[key]:.2f}%"})

    if not facts:
        uncertainties.append("当前没有足够的真实数据支撑详细解释，请以数据源中心的状态为准。")

    judgments.append("以上是系统根据真实数据生成的事实与统计结论，不构成投资建议。")

    return {
        "mode": "rule_based",
        "answer": " ".join(part for part in answer_parts if part) or "暂无法回答，请使用更具体的表述。",
        "facts": facts,
        "judgments": judgments,
        "counter_evidence": counter_evidence,
        "uncertainties": uncertainties,
        "disclaimer": "AI 助手只解释数据与模型，不预测价格、不提供买卖指令。",
    }


async def _llm_answer(question: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """LLM 解释（配置了 Key 才启用）。严格限制只能引用提供的真实上下文。"""
    if not settings.OPENAI_API_KEY or not settings.AI_ASSISTANT_ENABLED:
        return None
    try:
        import json

        import httpx

        payload = {
            "model": settings.OPENAI_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是 BTC 数据研究平台的解释助手。规则："
                        "1) 只能使用用户提供的 JSON 上下文中的真实数据，禁止编造任何数字；"
                        "2) 必须区分「事实」「统计结果」「模型判断」「不确定性」四类信息；"
                        "3) 禁止给出确定性买卖指令或收益承诺；"
                        "4) 数据缺失时必须说明缺失；"
                        "5) 使用简体中文，面向非金融专业用户，语言通俗。"
                    ),
                },
                {"role": "user", "content": f"上下文：{json.dumps(ctx, ensure_ascii=False)[:12000]}\n\n问题：{question}"},
            ],
            "temperature": 0.2,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.OPENAI_BASE_URL}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        return {
            "mode": "llm",
            "answer": content,
            "disclaimer": "答案基于系统提供的真实数据生成，不构成投资建议。",
        }
    except Exception as exc:  # noqa: BLE001 - LLM 不可用时降级到规则引擎
        logger.event("assistant.llm_failed", error=str(exc)[:200])
        return None


@router.post("/ask", summary="提问（解释数据/指标/模型/历史/回测）")
async def ask(payload: AssistantAskRequest, rid: RequestIdDep) -> dict[str, Any]:
    ctx = await _build_context()
    llm = await _llm_answer(payload.question, ctx)
    if llm:
        return llm
    result = _rule_based_answer(payload.question, ctx)
    result["note"] = (
        "当前未配置大模型 API Key，使用「基于真实数据的规则解释引擎」回答。"
        "该模式只复述系统里真实存在的数据与已计算结论，不做任何推测。"
    )
    return result


@router.get("/indicator-help/{code}", summary="指标通俗解释")
async def indicator_help(code: str) -> dict[str, Any]:
    from sqlalchemy import select

    factory = get_session_factory()
    async with factory() as session:
        row = (
            await session.execute(select(IndicatorDefinition).where(IndicatorDefinition.code == code))
        ).scalars().first()
    if not row:
        return {"available": False, "message": f"未找到指标 {code}"}
    return {
        "available": True,
        "code": row.code,
        "name_cn": row.name_cn,
        "meaning_cn": row.meaning_cn,
        "how_to_use": row.how_to_use,
        "bullish_hint": row.bullish_hint,
        "bearish_hint": row.bearish_hint,
        "requires_paid_provider": row.requires_paid_provider,
    }
