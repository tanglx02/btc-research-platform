# -*- coding: utf-8 -*-
"""宏观数据源。

设计要点（防止未来数据泄漏）：
宏观数据必须区分 **观测期(observation_date)** 与 **发布日(release_date)**。
本 Provider 返回的每条观测都带 ts（观测所属时间），回测引擎只能使用
`ts + release_lag <= 决策时间` 的数据点。
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

from ...core.errors import ProviderError
from ..base import DataProvider
from ..types import DataCategory, SeriesPoint


def _lat(started: datetime) -> float:
    return (datetime.now(timezone.utc) - started).total_seconds() * 1000


class StooqProvider(DataProvider):
    """Stooq CSV：无需 Key，实测国内可达，提供美元指数/股指/收益率/贵金属等。"""

    name = "stooq"
    display_name = "Stooq（免费 CSV）"
    homepage = "https://stooq.com"
    base_url = "https://stooq.com"
    categories = {DataCategory.MACRO_SERIES}
    capabilities = {"macro_series"}
    default_priority = 10
    region_hint = "cn-friendly"
    rate_limit_qps = 0.8
    notes = "无需 Key、实测可达；免费 CSV 覆盖美元指数、主要股指、国债收益率与大宗商品。"

    # 系统内部序列名 -> Stooq 代码
    SYMBOLS: dict[str, str] = {
        "DXY": "dx.f",              # 美元指数
        "SPX": "^spx",              # 标普 500
        "NDX": "^ndq",              # 纳斯达克 100
        "US10Y": "10usy.b",         # 美国 10 年期国债收益率
        "US2Y": "2usy.b",           # 美国 2 年期国债收益率
        "US30Y": "30usy.b",         # 美国 30 年期国债收益率
        "GOLD": "gc.f",             # COMEX 黄金
        "SILVER": "si.f",           # COMEX 白银
        "OIL": "cl.f",              # WTI 原油
        "COPPER": "hg.f",           # COMEX 铜
        "VIX": "^vix",              # 波动率指数
        "CNYUSD": "usdcny",         # 美元兑人民币
        "USTECH": "^ndx",
    }

    async def fetch_macro_series(self, series_id: str = "DXY", limit: int = 720, **_: Any):
        started = datetime.now(timezone.utc)
        code = self.SYMBOLS.get(series_id.upper())
        if not code:
            raise ProviderError(
                f"Stooq 不支持序列 {series_id}", provider=self.name, failure_type="unsupported_input"
            )
        # limit=0 时拿全历史
        path = "/q/d/l/"
        params: dict[str, Any] = {"s": code, "i": "d"}
        text = await self.transport.get_text(path, params=params, label=f"macro_{series_id}")
        points = self._parse(text)
        if not points:
            raise ProviderError(
                f"Stooq {series_id} 解析结果为空（可能接口变更或地区限制）",
                provider=self.name,
                failure_type="data_format_error",
            )
        if limit > 0:
            points = points[-limit:]
        return self.ok(
            [p.__dict__ for p in points],
            DataCategory.MACRO_SERIES,
            latency_ms=_lat(started),
            endpoint=f"{path}?s={code}",
            params=params,
            series_id=series_id,
            unit="index" if series_id in ("DXY", "SPX", "NDX", "VIX") else "percent",
            note="CSV 免费源：观测值为收盘价，发布延迟按 1 个交易日处理",
        )

    @staticmethod
    def _parse(text: str) -> list[SeriesPoint]:
        out: list[SeriesPoint] = []
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            try:
                date = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                close = float(row["Close"])
            except (KeyError, ValueError, TypeError):
                continue
            out.append(SeriesPoint(ts=int(date.timestamp()), value=close))
        return out


class YahooMacroProvider(DataProvider):
    """Yahoo Finance chart API：覆盖面最广的免费宏观源（美元指数、股指、国债收益率、贵金属、VIX）。

    现实约束：Yahoo 对机房 IP / 非浏览器 UA 经常限流或返回 429。
    本 Provider 会带浏览器 UA 请求；若仍被限流，会被健康引擎标记为 RATE_LIMITED 并自动切换到其它宏观源，
    宏观模块降级为「不可用」—— 不会用过期数据填充。
    """

    name = "yahoo_macro"
    display_name = "Yahoo Finance（宏观）"
    homepage = "https://finance.yahoo.com"
    base_url = "https://query1.finance.yahoo.com"
    categories = {DataCategory.MACRO_SERIES}
    capabilities = {"macro_series"}
    default_priority = 15
    region_hint = "global"
    rate_limit_qps = 0.5
    notes = "覆盖美元指数/股指/国债收益率/贵金属/VIX；部分地区或机房 IP 会被限流（429），自动降级。"

    SYMBOLS: dict[str, tuple[str, str]] = {
        "DXY": ("DX-Y.NYB", "index"),
        "SPX": ("^GSPC", "index"),
        "NDX": ("^IXIC", "index"),
        "VIX": ("^VIX", "index"),
        "US10Y": ("^TNX", "percent"),
        "US30Y": ("^TYX", "percent"),
        "US5Y": ("^FVX", "percent"),
        "GOLD": ("GC=F", "usd"),
        "SILVER": ("SI=F", "usd"),
        "OIL": ("CL=F", "usd"),
        "COPPER": ("HG=F", "usd"),
    }
    BROWSER_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )

    async def fetch_macro_series(self, series_id: str = "DXY", limit: int = 720, **_: Any):
        started = datetime.now(timezone.utc)
        key = series_id.upper()
        if key not in self.SYMBOLS:
            raise ProviderError(
                f"Yahoo 不支持序列 {series_id}", provider=self.name, failure_type="unsupported_input"
            )
        symbol, unit = self.SYMBOLS[key]
        params = {"range": "10y", "interval": "1d", "includePrePost": "false"}
        data = await self.transport.get_json(
            f"/v8/finance/chart/{symbol}",
            params=params,
            label=f"macro_{key}",
            headers={"User-Agent": self.BROWSER_UA, "Accept": "application/json"},
        )
        result = ((data or {}).get("chart") or {}).get("result") or []
        if not result:
            raise ProviderError(
                f"Yahoo {key} 返回为空（可能被限流）", provider=self.name, failure_type="empty_data"
            )
        stamps = result[0].get("timestamp") or []
        closes = ((result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        points: list[SeriesPoint] = []
        for ts, val in zip(stamps, closes):
            if val is None:
                continue
            try:
                points.append(SeriesPoint(ts=int(ts), value=float(val)))
            except (TypeError, ValueError):
                continue
        if not points:
            raise ProviderError(f"Yahoo {key} 无有效数据点", provider=self.name, failure_type="data_quality_error")
        if limit > 0:
            points = points[-limit:]
        return self.ok(
            [p.__dict__ for p in points],
            DataCategory.MACRO_SERIES,
            latency_ms=_lat(started),
            endpoint=f"/v8/finance/chart/{symbol}",
            params={"symbol": symbol},
            series_id=key,
            unit=unit,
            note="Yahoo 收盘序列；对宏观数据的发布延迟按 +1 个交易日处理",
        )


class FrankfurterProvider(DataProvider):
    """Frankfurter：欧洲央行官方汇率（无需 Key，实测国内可达）。

    用于提供 USDCNY / EURUSD 等汇率序列。它只有汇率，但作为「宏观模块不全挂」的兜底很有价值：
    即使美元指数取不到，至少汇率维度仍然真实可用。
    """

    name = "frankfurter"
    display_name = "Frankfurter（ECB 官方汇率）"
    homepage = "https://frankfurter.dev"
    base_url = "https://api.frankfurter.dev"
    categories = {DataCategory.MACRO_SERIES}
    capabilities = {"macro_series"}
    default_priority = 30
    region_hint = "cn-friendly"
    rate_limit_qps = 0.5
    notes = "无需 Key、实测国内可达；提供 ECB 官方汇率序列（USDCNY / EURUSD 等）。"

    SYMBOLS: dict[str, tuple[str, str]] = {
        "USDCNY": ("CNY", "rate"),
        "EURUSD": ("EUR", "rate"),
        "USDJPY": ("JPY", "rate"),
    }

    async def fetch_macro_series(self, series_id: str = "USDCNY", limit: int = 720, **_: Any):
        started = datetime.now(timezone.utc)
        key = series_id.upper()
        if key not in self.SYMBOLS:
            raise ProviderError(
                f"Frankfurter 不支持序列 {series_id}", provider=self.name, failure_type="unsupported_input"
            )
        target, unit = self.SYMBOLS[key]
        base = "EUR" if target == "EUR" else "USD"
        params = {"base": base, "symbols": target}
        data = await self.transport.get_json("/v1/latest", params=params, label=f"macro_{key}")
        rates = (data or {}).get("rates") or {}
        date_str = (data or {}).get("date")
        if target not in rates or not date_str:
            raise ProviderError(f"Frankfurter {key} 返回为空", provider=self.name, failure_type="empty_data")
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            value = float(rates[target])
        except (KeyError, ValueError, TypeError) as exc:
            raise ProviderError(
                f"Frankfurter {key} 解析失败", provider=self.name, failure_type="data_format_error"
            ) from exc
        return self.ok(
            [SeriesPoint(ts=int(date.timestamp()), value=value).__dict__],
            DataCategory.MACRO_SERIES,
            latency_ms=_lat(started),
            endpoint="/v1/latest",
            params=params,
            series_id=key,
            unit=unit,
            note="ECB 参考汇率：每个工作日更新一次，仅提供最新值（非长序列）",
        )


class FredProvider(DataProvider):
    """FRED（圣路易斯联储）：官方宏观数据库，需要免费 API Key。

    未配置 FRED_API_KEY 时状态 NOT_CONFIGURED：宏观模块显示「数据源未配置」，
    而不是用别的数字冒充官方数据。
    """

    name = "fred"
    display_name = "FRED（美联储官方）"
    homepage = "https://fred.stlouisfed.org"
    base_url = "https://api.stlouisfed.org"
    categories = {DataCategory.MACRO_SERIES}
    capabilities = {"macro_series"}
    requires_api_key = True
    api_key_env = "FRED_API_KEY"
    default_priority = 5
    region_hint = "global"
    rate_limit_qps = 0.8
    notes = "免费申请 API Key；覆盖利率/通胀/就业/货币总量/美联储资产负债表等官方序列。"

    # 内部序列名 -> (FRED series_id, 单位)
    SERIES: dict[str, tuple[str, str]] = {
        "FEDFUNDS": ("DFF", "percent"),            # 联邦基金利率
        "US10Y": ("DGS10", "percent"),             # 10 年国债收益率
        "US2Y": ("DGS2", "percent"),               # 2 年国债收益率
        "REAL10Y": ("DFII10", "percent"),          # 10 年期实际收益率（TIPS）
        "CPI": ("CPIAUCSL", "index"),              # CPI
        "CPI_YOY": ("CPIAUCSL", "percent"),
        "PCE": ("PCEPI", "index"),                 # PCE 物价指数
        "UNEMPLOYMENT": ("UNRATE", "percent"),     # 失业率
        "NFP": ("PAYEMS", "thousands"),            # 非农就业
        "GDP": ("A191RL1Q225SBEA", "percent"),     # 实际 GDP 同比
        "M2": ("M2SL", "billions"),                # M2 货币供应量
        "FED_BALANCE": ("WALCL", "millions"),      # 美联储资产负债表
        "DXY": ("DTWEXBGS", "index"),              # 广义美元指数
        "HY_SPREAD": ("BAMLH0A0HYM2", "percent"),  # 高收益债利差（风险偏好）
    }

    async def fetch_macro_series(self, series_id: str = "FEDFUNDS", limit: int = 720, **_: Any):
        started = datetime.now(timezone.utc)
        key = series_id.upper()
        if key not in self.SERIES:
            raise ProviderError(
                f"FRED 不支持序列 {series_id}", provider=self.name, failure_type="data_format_error"
            )
        fred_code, unit = self.SERIES[key]
        params = {
            "series_id": fred_code,
            "file_type": "json",
            "api_key": self.api_key(),
            "observation_start": "2000-01-01",
        }
        data = await self.transport.get_json("/fred/series/observations", params=params, label=f"macro_{key}")
        observations = (data or {}).get("observations") or []
        if not observations:
            raise ProviderError(f"FRED {key} 返回为空", provider=self.name, failure_type="empty_data")
        points: list[SeriesPoint] = []
        for obs in observations:
            try:
                date = datetime.strptime(obs["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                value = float(obs["value"])
            except (KeyError, ValueError, TypeError):
                continue
            points.append(SeriesPoint(ts=int(date.timestamp()), value=value))
        if not points:
            raise ProviderError(f"FRED {key} 无有效数据点", provider=self.name, failure_type="data_quality_error")
        if limit > 0:
            points = points[-limit:]
        return self.ok(
            [p.__dict__ for p in points],
            DataCategory.MACRO_SERIES,
            latency_ms=_lat(started),
            endpoint="/fred/series/observations",
            params={"series_id": fred_code},
            series_id=key,
            unit=unit,
            note="官方序列：区分观测期与发布日，使用时请遵循发布延迟",
        )
