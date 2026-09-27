# -*- coding: utf-8 -*-
"""运行时配置中心测试：后台改的配置必须真的生效，敏感值不许泄露。

这一组用例覆盖的是「配置可视化」这条链路最容易出问题的地方：
  1. 元数据与 Settings 字段脱节 —— spec 里写了 Settings 根本不存在的 key，
     保存时静默失败，用户以为配好了其实没配；
  2. 敏感值明文回传 —— 后台页面把密钥原文发给前端；
  3. 保存后运行时没变 —— 库里写了但 settings 单例还是旧值，必须重启才生效；
  4. 恢复默认后没还原 —— setattr 已经把原值冲掉，删了库记录也变不回去
     （这是本次开发中真实踩到并修复的缺陷，见 test_reset_restores_baseline_value）。
"""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.core.settings_store import (
    CATEGORIES,
    SETTINGS_SPEC,
    SPEC_BY_KEY,
    SettingsStore,
    coerce,
    get_settings_store,
    mask,
)


# ---------------------------------------------------------------- 元数据


def test_every_spec_key_exists_on_settings():
    """spec 里的每个 key 都必须能在 Settings 上找到。

    否则保存时会写进库、界面上也显示成功，但没有任何代码会去读它 —— 典型的假配置。
    """
    s = get_settings()
    missing = [spec.key for spec in SETTINGS_SPEC if not hasattr(s, spec.key)]
    assert not missing, f"以下配置项在 Settings 中不存在，保存后不会生效：{missing}"


def test_spec_keys_are_unique():
    keys = [s.key for s in SETTINGS_SPEC]
    assert len(keys) == len(set(keys)), f"存在重复的配置项：{[k for k in keys if keys.count(k) > 1]}"


def test_every_item_belongs_to_a_declared_category():
    codes = {c["code"] for c in CATEGORIES}
    orphan = sorted({s.category for s in SETTINGS_SPEC} - codes)
    assert not orphan, f"以下分类没有在 CATEGORIES 中声明，前端不会渲染：{orphan}"


def test_database_url_is_not_exposed():
    """DATABASE_URL 绝不出现在可配置项里：改错会直接导致服务起不来。"""
    assert "DATABASE_URL" not in SPEC_BY_KEY
    assert "HOST" not in SPEC_BY_KEY


def test_sensitive_items_are_marked():
    """密钥与口令必须标记为 sensitive，否则会被明文回传。"""
    for key in ("SMTP_PASSWORD", "GLASSNODE_API_KEY", "CRYPTOQUANT_API_KEY",
                "COINGLASS_API_KEY", "FRED_API_KEY", "OPENAI_API_KEY",
                "SECRET_KEY", "ADMIN_TOKEN"):
        assert key in SPEC_BY_KEY, f"缺少配置项 {key}"
        assert SPEC_BY_KEY[key].sensitive is True, f"{key} 属于敏感信息但未被标记"


# ---------------------------------------------------------------- 值处理


def test_coerce_types():
    assert coerce(SPEC_BY_KEY["SMTP_PORT"], "587") == 587
    assert coerce(SPEC_BY_KEY["SMTP_PORT"], "not-a-number") == 465  # 回退默认值而不是抛异常
    assert coerce(SPEC_BY_KEY["ALERTS_ENABLED"], "true") is True
    assert coerce(SPEC_BY_KEY["ALERTS_ENABLED"], "false") is False
    assert coerce(SPEC_BY_KEY["CROSS_VALIDATION_TOLERANCE_PCT"], "0.5") == 0.5


def test_mask_hides_the_middle():
    assert mask("") == ""
    assert mask("abc") == "***"
    m = mask("sk-abcdefghij1234")
    assert "abcdefghij" not in m
    assert m.startswith("sk-") and m.endswith("234")
    assert "*" in m


# ---------------------------------------------------------------- 读写


async def _fresh_store() -> SettingsStore:
    store = SettingsStore()
    await store.load(force=True)
    return store


@pytest.mark.asyncio
async def test_saved_value_is_applied_to_settings_immediately(db_ready):
    """保存后必须立刻写回 settings 单例 —— 这是「不用重启」的关键。"""
    store = await _fresh_store()
    await store.set_many({"SMTP_HOST": "smtp.example.com", "SMTP_PORT": 587})

    s = get_settings()
    assert s.SMTP_HOST == "smtp.example.com"
    assert s.SMTP_PORT == 587

    # 清理，避免污染后续用例
    await store.reset(["SMTP_HOST", "SMTP_PORT"])


@pytest.mark.asyncio
async def test_reset_restores_baseline_value(db_ready):
    """恢复默认后 settings 必须回到原始值。

    历史缺陷：apply_to_settings 只遍历缓存，被删除的键不会还原，
    于是出现「库里删干净了，运行时却还是旧值」。
    """
    store = await _fresh_store()
    s = get_settings()
    original = s.SMTP_HOST

    await store.set_many({"SMTP_HOST": "smtp.temporary.test"})
    assert s.SMTP_HOST == "smtp.temporary.test"

    await store.reset(["SMTP_HOST"])
    assert s.SMTP_HOST == original, "恢复默认后没有回到原始值"


@pytest.mark.asyncio
async def test_describe_never_returns_sensitive_plaintext(db_ready):
    store = await _fresh_store()
    await store.set_many({"SMTP_PASSWORD": "super-secret-value"})

    data = store.describe()
    flat = [item for items in data["groups"].values() for item in items]
    pwd = next(i for i in flat if i["key"] == "SMTP_PASSWORD")

    assert pwd["value"] == "", "敏感值被明文回传了"
    assert pwd["has_value"] is True
    assert "super-secret-value" not in str(pwd["masked"])
    assert pwd["source"] == "database"

    await store.reset(["SMTP_PASSWORD"])


@pytest.mark.asyncio
async def test_describe_reports_where_the_value_comes_from(db_ready):
    """用户必须能看出当前值来自后台、.env 还是默认值，否则会困惑「我明明配了为什么不生效」。"""
    store = await _fresh_store()
    await store.set_many({"SMTP_SENDER_NAME": "后台设置的名称"})
    data = store.describe()
    item = next(i for i in data["groups"]["smtp"] if i["key"] == "SMTP_SENDER_NAME")
    assert item["source"] == "database"
    assert item["overridden"] is True
    await store.reset(["SMTP_SENDER_NAME"])


@pytest.mark.asyncio
async def test_unknown_key_is_rejected(db_ready):
    """未知配置项必须报错，不能静默丢弃 —— 否则用户以为配好了其实没有。"""
    store = await _fresh_store()
    with pytest.raises(KeyError):
        await store.set_many({"TOTALLY_UNKNOWN_KEY": "x"})


@pytest.mark.asyncio
async def test_empty_payload_does_nothing(db_ready):
    store = await _fresh_store()
    result = await store.set_many({})
    assert result["count"] == 0


# ---------------------------------------------------------------- 数据源密钥


@pytest.mark.asyncio
async def test_provider_api_key_prefers_database_value(db_ready):
    """在后台填的密钥必须优先于 .env —— 这是「数据源也能后台配置」的核心。"""
    store = await _fresh_store()
    await store.set_many({"GLASSNODE_API_KEY": "key-from-admin-ui"})

    assert store.get_sync("GLASSNODE_API_KEY") == "key-from-admin-ui"

    await store.reset(["GLASSNODE_API_KEY"])


@pytest.mark.asyncio
async def test_provider_key_flows_into_provider_instance(db_ready):
    """配完之后，Provider 实例应当立即认为自己是「已配置」的。"""
    from app.providers.registry import get_registry

    store = await _fresh_store()
    registry = get_registry()
    if not registry.all():
        registry.autodiscover()
    provider = registry.get("glassnode")
    if provider is None:
        pytest.skip("glassnode 未注册（依赖可选模块），跳过")

    assert provider.is_configured() is False, "未配置时不应认为已配置"

    await store.set_many({"GLASSNODE_API_KEY": "key-from-admin-ui"})
    try:
        assert provider.is_configured() is True, "后台填了密钥之后 Provider 应立即变为已配置"
        assert provider.api_key() == "key-from-admin-ui"
    finally:
        await store.reset(["GLASSNODE_API_KEY"])
