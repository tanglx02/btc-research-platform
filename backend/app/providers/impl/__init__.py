# -*- coding: utf-8 -*-
"""Provider 实现包。

注册机制：`ProviderRegistry.autodiscover()` 会自动扫描本包下的所有模块并实例化
其中定义的 :class:`DataProvider` 子类 —— **新增数据源不需要修改任何业务代码，
删除/禁用也不需要修改任何业务代码**。

模块命名约定：
- `market_*.py`      行情类数据源
- `derivatives*.py`  衍生品数据源
- `onchain*.py`      链上数据源
- `macro*.py`        宏观数据源
- `sentiment*.py`    情绪数据源
- `etf*.py`          ETF 数据源
- `_*.py`            非数据源的公共基类（自动跳过）
- `mock_*.py`        测试隔离用 Mock（默认跳过，仅测试环境载入）
"""

__all__: list[str] = []
