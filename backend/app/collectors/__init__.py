# -*- coding: utf-8 -*-
"""采集器集合。

所有采集任务都具备：运行 / 暂停 / 恢复 / 重试 / 跳过 / 手动执行 的能力，
并通过 `SystemJob` 与 `SyncCheckpoint` 两张表统一治理。
"""

from .base import BaseCollector
from .data_collectors import (
    DerivativesCollector,
    EtfCollector,
    MacroCollector,
    OnchainCollector,
    SentimentCollector,
)
from .market_collector import (
    DataQualityScanner,
    GapRepairCollector,
    MarketCollector,
)

__all__ = [
    "BaseCollector",
    "MarketCollector",
    "GapRepairCollector",
    "DataQualityScanner",
    "DerivativesCollector",
    "OnchainCollector",
    "SentimentCollector",
    "MacroCollector",
    "EtfCollector",
]
