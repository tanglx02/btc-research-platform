# -*- coding: utf-8 -*-
"""邮件正文构造。

需求里反复强调的一点：不能只写「BTC Alert!」。
打开邮件的人（也就是用户本人）应该不用回到网站就能知道：发生了什么、为什么触发、
实际数据是多少、阈值是多少、历史位置在哪、数据来自哪个 Provider、有没有发生故障切换。

因此这里做三件事：
1. 把 condition_result 逐条翻译成「你的条件 / 实际数值 / 结论」的对照表；
2. 把当时的市场上下文（价格、回撤、MVRV、风险、周期阶段）一起带上；
3. 如实标注数据来源、数据时间、数据质量、是否使用了备用源。
   数据不可用或质量不足时，邮件里**必须**写明，而不是回避。

同时严格区分「事实」与「系统判断」：绝不出现「现在必须买/卖」这类措辞。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .catalog import get_spec

SEVERITY_CN = {
    "INFO": "信息",
    "WARNING": "提醒",
    "HIGH": "重要",
    "CRITICAL": "严重",
}

SEVERITY_COLOR = {
    "INFO": "#2b6cb0",
    "WARNING": "#b7791f",
    "HIGH": "#c05621",
    "CRITICAL": "#c53030",
}


def _fmt_num(v: Any, unit: str = "") -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if unit == "USD":
        return f"{f:,.2f}"
    if abs(f) >= 10000:
        return f"{f:,.0f}"
    if abs(f) >= 100:
        return f"{f:,.2f}"
    if abs(f) < 1:
        return f"{f:.4f}".rstrip("0").rstrip(".")
    return f"{f:.2f}"


def _fmt_dt(dt: datetime | str | None) -> str:
    if dt is None or dt == "":
        return "—"
    # 事件可能来自数据库（datetime），也可能是 API 传入的 ISO 字符串；
    # 两种都要能显示，不能因为类型不同把整封邮件渲染炸掉。
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _condition_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        spec = get_spec(r.get("metric_code", ""))
        unit = r.get("unit") or (spec.unit if spec else "")
        rows.append({
            "name": r.get("metric_name") or r.get("metric_code"),
            "description": r.get("description") or "",
            "unit": unit,
            "actual": _fmt_num(r.get("actual"), unit),
            "threshold": _threshold_text(r, unit),
            "satisfied": bool(r.get("satisfied")),
            "available": bool(r.get("available", True)),
            "reason": r.get("reason") or "",
        })
    return rows


def _threshold_text(r: dict[str, Any], unit: str) -> str:
    op = r.get("operator")
    lo, hi = r.get("threshold"), r.get("threshold_high")
    suffix = f" {unit}".rstrip()
    if op in ("between", "enter_range", "exit_range"):
        return f"{_fmt_num(lo)} ~ {_fmt_num(hi)}{suffix}"
    if op in ("change_gt", "change_lt"):
        return f"{_fmt_num(lo)}%"
    if op in ("percentile_gt", "percentile_lt"):
        return f"{_fmt_num(lo)}%"
    if op in ("cross_above", "cross_below"):
        return str(r.get("compare_metric") or lo or "—")
    if op == "state_change":
        return "任何变化"
    if op == "state_is":
        return str(r.get("expected_state") or "—")
    return f"{_fmt_num(lo)}{suffix}"


def build_subject(rule_name: str, severity: str, kind: str = "triggered") -> str:
    prefix = {"triggered": "触发", "recovered": "已恢复", "quality_warning": "数据质量异常"}.get(kind, "提醒")
    level = SEVERITY_CN.get(severity, severity)
    return f"【BTC智能预警·{level}】{rule_name} 已{prefix}"


def _context_lines(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    """市场上下文：只在真的有值时列出，取不到就写「不可用」，不编。"""
    out: list[tuple[str, str]] = []

    def add(label: str, key: str, unit: str = "", fmt: str = "num") -> None:
        v = ctx.get(key)
        if v is None:
            return
        out.append((label, _fmt_num(v, unit) if fmt == "num" else str(v)))

    add("当前 BTC 价格", "price", "USD")
    dd = ctx.get("drawdown_from_ath")
    if dd is not None:
        out.append(("距 ATH 回撤", f"{_fmt_num(dd)}%"))
    add("24H 涨跌幅", "change_24h", "%")
    add("7D 涨跌幅", "change_7d", "%")
    add("MVRV", "mvrv")
    add("SOPR", "sopr")
    add("资金费率", "funding_rate", "%")
    add("未平仓合约量", "open_interest", "USD")
    add("恐惧贪婪指数", "fear_greed")
    add("ETF 单日净流入", "etf_netflow", "USD")
    add("综合风险评分", "risk_score")
    add("风险等级", "risk_level", fmt="str")
    add("估值状态", "valuation_state", fmt="str")
    add("市场周期阶段", "cycle_phase", fmt="str")
    add("综合市场状态", "regime", fmt="str")
    return out


def _source_lines(ctx: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    providers = ctx.get("providers") or []
    if providers:
        out.append(("主要数据源", ", ".join(providers[:3])))
    chain = ctx.get("provider_chain")
    if chain:
        out.append(("实际数据源链", chain))
    if ctx.get("used_fallback"):
        out.append(("故障切换", "是 —— 主数据源当前不可用，本次判断使用的是备用数据源"))
    else:
        out.append(("故障切换", "否"))
    q = ctx.get("data_quality")
    if q:
        out.append(("数据质量", _quality_cn(q)))
    if ctx.get("data_updated_at"):
        out.append(("数据更新时间", _fmt_dt(ctx["data_updated_at"])))
    if ctx.get("data_freshness_note"):
        out.append(("数据新鲜度", str(ctx["data_freshness_note"])))
    unavailable = ctx.get("unavailable_metrics") or []
    if unavailable:
        names = [get_spec(c).name_cn if get_spec(c) else c for c in unavailable]
        out.append(("本次不可用的数据", ", ".join(names) + "（系统未用估算值代替，相关条件未参与判断）"))
    return out


_QUALITY_CN = {
    "CROSS_VERIFIED": "已交叉验证（多个独立数据源一致）",
    "VERIFIED": "已验证（单一权威源）",
    "SINGLE_SOURCE": "单一数据源（未经交叉验证）",
    "STALE": "数据已过期",
    "MISSING": "数据缺失",
    "ANY": "未标注",
}


def _quality_cn(q: str) -> str:
    return _QUALITY_CN.get(str(q).upper(), str(q))


def build_body(event: dict[str, Any], *, site_url: str = "") -> tuple[str, str]:
    """返回 (纯文本, HTML)。两者内容一致，只是排版不同。"""
    rule_name = event.get("rule_name") or "未命名规则"
    severity = event.get("severity") or "WARNING"
    kind = event.get("event_type") or "triggered"
    ctx = event.get("market_context") or {}
    results = (event.get("condition_result") or {}).get("results") or []
    rows = _condition_rows(results)
    ctx_lines = _context_lines(ctx)
    src_lines = _source_lines(ctx)

    headline = {
        "triggered": "你设置的监测条件已经成立",
        "recovered": "此前成立的监测条件已恢复",
        "quality_warning": "关键数据质量不足，条件未能可靠判断",
    }.get(kind, "监测提醒")

    # ------------------------------------------------ 纯文本
    lines: list[str] = []
    lines.append(f"【{rule_name}】{headline}")
    lines.append("=" * 58)
    lines.append(f"触发时间：{_fmt_dt(event.get('trigger_time'))}")
    lines.append(f"提醒等级：{SEVERITY_CN.get(severity, severity)}（{severity}）")
    if event.get("description"):
        lines.append(f"规则说明：{event['description']}")
    lines.append("")
    lines.append("触发条件与实际数值")
    lines.append("-" * 58)
    if rows:
        for r in rows:
            mark = "已满足" if r["satisfied"] else ("未满足" if r["available"] else "数据不可用")
            lines.append(f"· {r['description']}")
            lines.append(f"    实际值：{r['actual']}    阈值：{r['threshold']}    结论：{mark}")
            if r["reason"]:
                lines.append(f"    判定依据：{r['reason']}")
    else:
        lines.append("（本次事件未附带逐条条件明细）")
    lines.append("")

    if ctx_lines:
        lines.append("当时的市场状态")
        lines.append("-" * 58)
        for label, val in ctx_lines:
            lines.append(f"· {label}：{val}")
        lines.append("")

    lines.append("数据来源与可靠性")
    lines.append("-" * 58)
    for label, val in src_lines:
        lines.append(f"· {label}：{val}")
    lines.append("")

    lines.append("系统说明")
    lines.append("-" * 58)
    lines.append("· 本次提醒只表示你设置的监测条件已经成立，不代表未来价格一定上涨或下跌。")
    lines.append("· 系统不提供买卖指令，也不会自动交易；是否操作由你自己决定。")
    lines.append("· 数据取不到时系统不会用估算值触发提醒，宁可提示「数据不足」。")
    if site_url:
        lines.append("")
        lines.append(f"查看完整分析：{site_url}")
    lines.append("")
    lines.append(f"（本邮件由 BTC 智能监测平台自动发送 · {_fmt_dt(datetime.now(timezone.utc))}）")

    text_body = "\n".join(lines)

    # ------------------------------------------------ HTML
    color = SEVERITY_COLOR.get(severity, "#2b6cb0")
    html_rows = "".join(
        f"""<tr>
              <td style="padding:8px 10px;border-bottom:1px solid #edf2f7">{_esc(r['description'])}</td>
              <td style="padding:8px 10px;border-bottom:1px solid #edf2f7;text-align:right;font-variant-numeric:tabular-nums">{_esc(r['actual'])}</td>
              <td style="padding:8px 10px;border-bottom:1px solid #edf2f7;text-align:right;color:#718096">{_esc(r['threshold'])}</td>
              <td style="padding:8px 10px;border-bottom:1px solid #edf2f7;text-align:center;color:{'#276749' if r['satisfied'] else '#a0aec0'}">{'已满足' if r['satisfied'] else ('未满足' if r['available'] else '数据不可用')}</td>
            </tr>"""
        for r in rows
    )
    ctx_rows = "".join(
        f'<tr><td style="padding:5px 0;color:#718096">{_esc(k)}</td><td style="padding:5px 0;text-align:right;font-weight:600">{_esc(v)}</td></tr>'
        for k, v in ctx_lines
    )
    src_rows = "".join(
        f'<tr><td style="padding:5px 0;color:#718096">{_esc(k)}</td><td style="padding:5px 0;text-align:right">{_esc(v)}</td></tr>'
        for k, v in src_lines
    )

    html_body = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f7fafc;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1a202c;line-height:1.65">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:10px;overflow:hidden;border:1px solid #e2e8f0">
    <div style="background:{color};color:#fff;padding:18px 22px">
      <div style="font-size:12px;letter-spacing:.08em;opacity:.9">BTC 智能监测 · {_esc(SEVERITY_CN.get(severity, severity))}</div>
      <div style="font-size:20px;font-weight:700;margin-top:4px">{_esc(rule_name)}</div>
      <div style="font-size:14px;margin-top:6px;opacity:.95">{_esc(headline)}</div>
    </div>
    <div style="padding:20px 22px">
      <table style="width:100%;font-size:13px;margin-bottom:18px">
        {ctx_rows}
      </table>

      <h3 style="font-size:15px;margin:18px 0 8px">触发条件与实际数值</h3>
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f7fafc;color:#4a5568">
          <th style="padding:8px 10px;text-align:left">你的条件</th>
          <th style="padding:8px 10px;text-align:right">实际值</th>
          <th style="padding:8px 10px;text-align:right">阈值</th>
          <th style="padding:8px 10px;text-align:center">结论</th>
        </tr></thead>
        <tbody>{html_rows or '<tr><td colspan="4" style="padding:12px;color:#718096">本次事件未附带逐条条件明细</td></tr>'}</tbody>
      </table>

      <h3 style="font-size:15px;margin:22px 0 8px">数据来源与可靠性</h3>
      <table style="width:100%;font-size:13px">{src_rows}</table>

      <div style="margin-top:22px;padding:14px 16px;background:#fffaf0;border-left:3px solid #d69e2e;font-size:13px;color:#744210">
        <b>系统说明</b><br>
        本次提醒只表示你设置的监测条件已经成立，不代表未来价格一定上涨或下跌。<br>
        系统不提供买卖指令，也不会自动交易；是否操作由你自己决定。<br>
        数据取不到时系统不会用估算值触发提醒，宁可提示「数据不足」。
      </div>
      {'<p style="margin-top:18px"><a href="' + _esc(site_url) + '" style="color:' + color + '">查看完整分析与可追溯的原始数据 →</a></p>' if site_url else ''}
      <p style="margin-top:18px;font-size:12px;color:#a0aec0">
        本邮件由 BTC 智能监测平台自动发送 · {_fmt_dt(datetime.now(timezone.utc))}
      </p>
    </div>
  </div>
</body></html>"""

    return text_body, html_body


def _esc(s: Any) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def build_daily_digest_subject(kind: str = "daily") -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    label = {"daily": "每日摘要", "weekly": "周报"}.get(kind, "摘要")
    return f"【BTC {label}】{today} 市场状态与你需要关注的条件"


def build_digest_body(digest: dict[str, Any], *, site_url: str = "", kind: str = "daily") -> tuple[str, str]:
    """每日摘要 / 周报正文。同样坚持「有就说，没有就说没有」。"""
    title = {"daily": "每日 BTC 市场摘要", "weekly": "BTC 周报"}.get(kind, "BTC 摘要")
    lines: list[str] = [f"{title} · {_fmt_dt(datetime.now(timezone.utc))}", "=" * 58, ""]

    def section(name: str, items: list[tuple[str, str]]) -> None:
        if not items:
            return
        lines.append(name)
        lines.append("-" * 58)
        for k, v in items:
            lines.append(f"· {k}：{v}")
        lines.append("")

    section("价格", _context_lines(digest.get("market") or {}))
    section("市场状态", [(k, v) for k, v in _context_lines(digest.get("state") or {})
                     if k in ("风险等级", "估值状态", "市场周期阶段", "综合市场状态")])

    changes = digest.get("changes") or []
    if changes:
        lines.append("过去 24 小时的重要变化")
        lines.append("-" * 58)
        for c in changes:
            lines.append(f"· {c}")
        lines.append("")

    plans = digest.get("plans") or []
    if plans:
        lines.append("我的资金计划")
        lines.append("-" * 58)
        for p in plans:
            lines.append(f"· {p}")
        lines.append("")

    watch = digest.get("watch") or []
    if watch:
        lines.append("当前需要关注的条件")
        lines.append("-" * 58)
        for w in watch:
            lines.append(f"· {w}")
        lines.append("")

    unavailable = digest.get("unavailable") or []
    if unavailable:
        lines.append("本次未能获取的数据")
        lines.append("-" * 58)
        for u in unavailable:
            lines.append(f"· {u}")
        lines.append("")

    lines.append("说明：本摘要只汇总事实数据与系统状态，不构成投资建议，系统不会自动交易。")
    if site_url:
        lines.append(f"查看详情：{site_url}")

    text_body = "\n".join(lines)
    html_body = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#f7fafc;font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;color:#1a202c;line-height:1.7">
<div style="max-width:640px;margin:0 auto;background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:22px">
  <h2 style="margin:0 0 4px;font-size:19px">{_esc(title)}</h2>
  <div style="font-size:12px;color:#718096;margin-bottom:16px">{_fmt_dt(datetime.now(timezone.utc))}</div>
  <pre style="white-space:pre-wrap;font-family:inherit;font-size:13px;margin:0">{_esc(text_body)}</pre>
  <p style="margin-top:20px;font-size:12px;color:#a0aec0">本邮件由 BTC 智能监测平台自动发送</p>
</div></body></html>"""
    return text_body, html_body


# ------------------------------------------------------------------ 条件模板
#
# 需求三十：模板只是「把一组条件预先填好」，底层走的仍然是同一个 Rule Engine。
# 加模板不需要改引擎；加指标也不需要改模板体系。

BUILTIN_TEMPLATES: list[dict[str, Any]] = [
    {
        "code": "price_above",
        "name": "价格突破某个价位",
        "description": "当 BTC 价格高于你设定的价位时提醒。适合「涨到某个位置我想看一眼」。",
        "severity": "WARNING",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "price", "operator": "gt", "threshold": 100000}],
        }],
        "hint": "把阈值改成你关心的价位即可。",
    },
    {
        "code": "price_below",
        "name": "价格跌破某个价位",
        "description": "当 BTC 价格低于你设定的价位时提醒。适合关注「跌到某个位置」的情况。",
        "severity": "WARNING",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "price", "operator": "lt", "threshold": 50000}],
        }],
        "hint": "把阈值改成你关心的价位即可。",
    },
    {
        "code": "big_drawdown",
        "name": "从历史最高点大幅回撤",
        "description": "当价格距离历史最高点回落超过一定幅度时提醒。回撤加深通常意味着市场情绪转弱。",
        "severity": "HIGH",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{
                "metric_code": "drawdown_from_ath", "operator": "lt",
                "threshold": -30, "duration_seconds": 21600,
            }],
        }],
        "hint": "阈值是负数百分比，-30 表示「从最高点回落 30% 以上」；默认要求持续 6 小时，避免瞬时插针误报。",
    },
    {
        "code": "extreme_fear",
        "name": "市场进入极度恐慌",
        "description": "当恐慌贪婪指数低于某个值时提醒。极低值通常出现在市场情绪最差的时候。",
        "severity": "WARNING",
        "cooldown_seconds": 43200,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{
                "metric_code": "fear_greed", "operator": "lt",
                "threshold": 15, "duration_seconds": 43200,
            }],
        }],
        "hint": "默认 15（极度恐慌）。要求持续 12 小时，避免一日情绪波动就打扰你。",
    },
    {
        "code": "valuation_low",
        "name": "市场估值进入低位",
        "description": "当 MVRV 低于 1.5 时提醒。MVRV 是「市值 / 已实现市值」，数值越低通常代表持有者整体浮盈越小、估值越低。",
        "severity": "INFO",
        "cooldown_seconds": 172800,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{
                "metric_code": "mvrv", "operator": "lt",
                "threshold": 1.5, "duration_seconds": 86400,
            }],
        }],
        "hint": "MVRV 低于 1.5 一般被视为估值偏低区域，但不代表不会更低。",
    },
    {
        "code": "valuation_high",
        "name": "市场估值进入高位",
        "description": "当 MVRV 高于 3.5 时提醒。数值越高通常代表整体浮盈越大、风险随之上升。",
        "severity": "HIGH",
        "cooldown_seconds": 172800,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{
                "metric_code": "mvrv", "operator": "gt",
                "threshold": 3.5, "duration_seconds": 86400,
            }],
        }],
        "hint": "MVRV 高于 3.5 一般对应历史上偏热的区间。",
    },
    {
        "code": "leverage_risk",
        "name": "杠杆过热风险",
        "description": "当资金费率明显偏高且未平仓合约处于高位时提醒。这通常代表多头过于拥挤。",
        "severity": "HIGH",
        "cooldown_seconds": 43200,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [
                {"metric_code": "funding_annualized", "operator": "gt", "threshold": 50},
                {"metric_code": "open_interest", "operator": "percentile_gt", "threshold": 90},
            ],
        }],
        "hint": "资金费率年化 > 50% 且持仓量处历史 90% 分位以上才提醒，两个条件同时成立。",
    },
    {
        "code": "etf_outflow",
        "name": "ETF 资金异常流出",
        "description": "当 ETF 单日净流出超过一定金额时提醒。大额流出通常代表机构资金在减仓。",
        "severity": "WARNING",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "etf_netflow", "operator": "lt", "threshold": -300000000}],
        }],
        "hint": "阈值单位是美元，-3 亿表示净流出超过 3 亿美元。",
    },
    {
        "code": "phase_change",
        "name": "市场阶段发生变化",
        "description": "当系统识别的周期阶段与上一次不同时提醒。适合只想知道「拐点来了没」。",
        "severity": "INFO",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "cycle_phase", "operator": "state_change"}],
        }],
        "hint": "只在阶段切换的那一刻提醒一次，不会持续发。",
    },
    {
        "code": "overall_risk_high",
        "name": "综合风险进入高位",
        "description": "当系统的综合风险评分进入高位、或风险等级恶化时提醒。",
        "severity": "HIGH",
        "cooldown_seconds": 43200,
        "logic": "OR",
        "groups": [{
            "operator": "OR",
            "conditions": [
                {"metric_code": "risk_score", "operator": "gt", "threshold": 70},
                {"metric_code": "risk_level", "operator": "state_change"},
            ],
        }],
        "hint": "风险评分超过 70 分，或者风险等级发生变化，任一成立即提醒。",
    },
    {
        "code": "dca_due",
        "name": "下次定投到点",
        "description": "当距离下次计划的定投日不足 1 天时提醒。只提醒，不会自动下单。",
        "severity": "INFO",
        "cooldown_seconds": 86400,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "plan_days_to_next", "operator": "lt", "threshold": 1}],
        }],
        "hint": "需要先创建资金计划。系统默认只提醒、不自动交易。",
    },
    {
        "code": "holding_loss",
        "name": "持仓浮亏超过容忍度",
        "description": "当当前持仓的浮亏百分比低于你设定的值时提醒。",
        "severity": "HIGH",
        "cooldown_seconds": 43200,
        "logic": "AND",
        "groups": [{
            "operator": "AND",
            "conditions": [{"metric_code": "plan_holding_pnl_pct", "operator": "lt", "threshold": -20}],
        }],
        "hint": "需要先创建资金计划并录入交易记录。阈值是负数百分比。",
    },
]
