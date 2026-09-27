# -*- coding: utf-8 -*-
"""轻量缓存层：默认进程内 LRU+TTL，配置 REDIS_URL 后自动切换 Redis。

铁律：缓存绝对不能成为唯一数据源 —— 数据库才是长期真相来源。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any

from ..core.config import get_settings
from ..core.logging import get_logger

logger = get_logger(__name__)


class CacheBackend:
    """缓存后端统一接口。

    每个方法都必须真正实现 —— 曾经出现过 Redis 后端下 `invalidate_prefix` 静默空转的情况：
    调用方以为旧数据已清掉，实际一直读到过期值，而且没有任何报错。
    """

    def get(self, key: str) -> Any | None:  # pragma: no cover - interface
        raise NotImplementedError

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete(self, key: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def delete_prefix(self, prefix: str) -> int:  # pragma: no cover - interface
        """删除所有以 prefix 开头的键，返回实际删除数量。"""
        raise NotImplementedError

    def clear(self) -> None:  # pragma: no cover
        raise NotImplementedError


class MemoryCache(CacheBackend):
    """线程安全 TTL 缓存。"""

    def __init__(self, max_entries: int = 5000, default_ttl: int = 60) -> None:
        self._data: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()
        self.max_entries = max_entries
        self.default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if not item:
                return None
            expire, value = item
            if expire < time.time():
                self._data.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                # 简单清理：先删过期，仍满则删最早写入
                now = time.time()
                for k, (exp, _) in list(self._data.items()):
                    if exp < now:
                        self._data.pop(k, None)
                while len(self._data) >= self.max_entries:
                    self._data.pop(next(iter(self._data)), None)
            self._data[key] = (time.time() + (ttl or self.default_ttl), value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            keys = [k for k in list(self._data) if k.startswith(prefix)]
            for k in keys:
                self._data.pop(k, None)
            return len(keys)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class RedisCache(CacheBackend):
    """Redis 缓存（可选依赖，未安装时自动降级到内存）。"""

    def __init__(self, url: str, default_ttl: int = 60) -> None:
        import redis  # type: ignore

        self._r = redis.Redis.from_url(url, decode_responses=True)
        self.default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        raw = self._r.get(key)
        return json.loads(raw) if raw else None

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self._r.setex(key, ttl or self.default_ttl, json.dumps(value, ensure_ascii=False, default=str))

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        # SCAN 而非 KEYS：KEYS 会阻塞线上 Redis，长期运行系统里这是常见事故源
        pattern = prefix if prefix.endswith("*") else f"{prefix}*"
        deleted = 0
        pipeline = self._r.pipeline()
        for key in self._r.scan_iter(match=pattern, count=500):
            pipeline.delete(key)
            deleted += 1
            if deleted % 500 == 0:
                pipeline.execute()
        if deleted % 500:
            pipeline.execute()
        return deleted

    def clear(self) -> None:
        self._r.flushdb()


class CacheManager:
    def __init__(self) -> None:
        s = get_settings()
        self.default_ttl = s.CACHE_TTL_SECONDS
        self.backend: CacheBackend
        if s.REDIS_URL:
            try:
                self.backend = RedisCache(s.REDIS_URL, s.CACHE_TTL_SECONDS)
                logger.event("cache.backend=redis")
                return
            except Exception as exc:  # redis 不可用 => 降级内存，绝不中断启动
                logger.event("cache.redis_unavailable", error=str(exc))
        self.backend = MemoryCache(s.CACHE_MAX_ENTRIES, s.CACHE_TTL_SECONDS)
        logger.event("cache.backend=memory")

    @staticmethod
    def make_key(prefix: str, *parts: Any) -> str:
        raw = ":".join(str(p) for p in parts)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]
        return f"{prefix}:{digest}"

    def get(self, key: str) -> Any | None:
        return self.backend.get(key)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        self.backend.set(key, value, ttl or self.default_ttl)

    def invalidate_prefix(self, prefix: str) -> int:
        """按前缀失效缓存，返回实际删除条数。

        对不支持该操作的后端记录一条告警日志，而不是静默返回：调用方需要知道清理没有生效。
        """
        try:
            removed = self.backend.delete_prefix(prefix)
            logger.event("cache.invalidated", prefix=prefix, removed=removed)
            return removed
        except NotImplementedError:
            logger.event("cache.invalidate_unsupported", prefix=prefix,
                         backend=type(self.backend).__name__)
            return 0


_cache_singleton: CacheManager | None = None


def get_cache() -> CacheManager:
    global _cache_singleton
    if _cache_singleton is None:
        _cache_singleton = CacheManager()
    return _cache_singleton
