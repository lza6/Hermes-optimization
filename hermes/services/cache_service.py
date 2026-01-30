"""
CacheService - 内存缓存服务

提供带 TTL 的 LRU 缓存功能，用于减少数据库查询：
- 供应商数据缓存
- 模型列表缓存
- 支持手动失效和自动过期

v3.0.0 新增
"""

import time
import asyncio
from typing import Optional, Any, Dict, Callable, TypeVar
from functools import wraps
from collections import OrderedDict
from dataclasses import dataclass

from ..config import config
from ..utils.logger import logger


T = TypeVar('T')


@dataclass
class CacheEntry:
    """缓存条目"""
    value: Any
    expires_at: float  # 过期时间戳


class TTLCache:
    """
    带 TTL 的 LRU 缓存实现
    
    特性：
    - 自动过期清理
    - LRU 淘汰策略
    - 线程安全（使用 asyncio.Lock）
    """
    
    def __init__(self, max_size: int = 100, default_ttl: int = 60):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._hits = 0
        self._misses = 0
    
    async def get(self, key: str) -> Optional[Any]:
        """获取缓存值，返回 None 表示未命中或已过期"""
        async with self._lock:
            entry = self._cache.get(key)
            
            if entry is None:
                self._misses += 1
                return None
            
            # 检查过期
            if time.time() > entry.expires_at:
                del self._cache[key]
                self._misses += 1
                return None
            
            # LRU: 移动到末尾表示最近使用
            self._cache.move_to_end(key)
            self._hits += 1
            return entry.value
    
    async def set(self, key: str, value: Any, ttl: int = None) -> None:
        """设置缓存值"""
        ttl = ttl or self.default_ttl
        expires_at = time.time() + ttl
        
        async with self._lock:
            # 如果已存在，先删除再添加（保持顺序）
            if key in self._cache:
                del self._cache[key]
            
            # 检查容量限制
            while len(self._cache) >= self.max_size:
                # 移除最旧的条目 (LRU)
                self._cache.popitem(last=False)
            
            self._cache[key] = CacheEntry(value=value, expires_at=expires_at)
    
    async def delete(self, key: str) -> bool:
        """删除指定 key"""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False
    
    async def clear(self) -> None:
        """清空所有缓存"""
        async with self._lock:
            self._cache.clear()
            logger.info("[Cache] 缓存已清空")
    
    async def invalidate_pattern(self, pattern: str) -> int:
        """
        按模式失效缓存
        pattern: 包含此字符串的 key 都会被删除
        返回删除的条目数
        """
        async with self._lock:
            keys_to_delete = [k for k in self._cache.keys() if pattern in k]
            for key in keys_to_delete:
                del self._cache[key]
            if keys_to_delete:
                logger.info(f"[Cache] 失效 {len(keys_to_delete)} 条匹配 '{pattern}' 的缓存")
            return len(keys_to_delete)
    
    async def cleanup_expired(self) -> int:
        """清理所有过期条目，返回清理数量"""
        now = time.time()
        async with self._lock:
            expired = [k for k, v in self._cache.items() if now > v.expires_at]
            for key in expired:
                del self._cache[key]
            return len(expired)
    
    def stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        total = self._hits + self._misses
        hit_rate = (self._hits / total * 100) if total > 0 else 0
        return {
            "size": len(self._cache),
            "max_size": self.max_size,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": f"{hit_rate:.1f}%"
        }


class CacheService:
    """
    缓存服务单例
    
    提供多个命名缓存实例，支持不同的 TTL 策略
    """
    
    # 全局缓存实例
    _providers_cache: TTLCache = None
    _models_cache: TTLCache = None
    _general_cache: TTLCache = None
    
    @classmethod
    def initialize(cls):
        """初始化缓存实例"""
        if cls._providers_cache is None:
            cls._providers_cache = TTLCache(
                max_size=config.CACHE_MAX_SIZE,
                default_ttl=config.CACHE_TTL_PROVIDERS
            )
            cls._models_cache = TTLCache(
                max_size=config.CACHE_MAX_SIZE,
                default_ttl=config.CACHE_TTL_MODELS
            )
            cls._general_cache = TTLCache(
                max_size=config.CACHE_MAX_SIZE * 2,
                default_ttl=60
            )
            logger.info(f"[Cache] 缓存服务初始化完成 (供应商TTL={config.CACHE_TTL_PROVIDERS}s, 模型TTL={config.CACHE_TTL_MODELS}s)")
    
    @classmethod
    async def get_providers_cache(cls) -> TTLCache:
        if cls._providers_cache is None:
            cls.initialize()
        return cls._providers_cache
    
    @classmethod
    async def get_models_cache(cls) -> TTLCache:
        if cls._models_cache is None:
            cls.initialize()
        return cls._models_cache
    
    @classmethod
    async def get_general_cache(cls) -> TTLCache:
        if cls._general_cache is None:
            cls.initialize()
        return cls._general_cache
    
    @classmethod
    async def invalidate_providers(cls):
        """失效所有供应商相关缓存"""
        if cls._providers_cache:
            await cls._providers_cache.clear()
        if cls._models_cache:
            await cls._models_cache.clear()
        logger.info("[Cache] 供应商和模型缓存已失效")
    
    @classmethod
    def get_all_stats(cls) -> Dict[str, Any]:
        """获取所有缓存统计"""
        result = {}
        if cls._providers_cache:
            result["providers"] = cls._providers_cache.stats()
        if cls._models_cache:
            result["models"] = cls._models_cache.stats()
        if cls._general_cache:
            result["general"] = cls._general_cache.stats()
        return result


def cached(cache_name: str = "general", key_prefix: str = "", ttl: int = None):
    """
    缓存装饰器
    
    用法:
        @cached(cache_name="providers", key_prefix="get_all")
        async def get_all_providers():
            ...
    
    Args:
        cache_name: 缓存名称 ("providers", "models", "general")
        key_prefix: 缓存 key 前缀
        ttl: 自定义 TTL (秒)
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            # 生成缓存 key
            cache_key = f"{key_prefix}:{func.__name__}"
            if args:
                cache_key += f":{hash(args)}"
            if kwargs:
                cache_key += f":{hash(frozenset(kwargs.items()))}"
            
            # 获取对应缓存实例
            if cache_name == "providers":
                cache = await CacheService.get_providers_cache()
            elif cache_name == "models":
                cache = await CacheService.get_models_cache()
            else:
                cache = await CacheService.get_general_cache()
            
            # 尝试从缓存获取
            cached_value = await cache.get(cache_key)
            if cached_value is not None:
                return cached_value
            
            # 执行原函数
            result = await func(*args, **kwargs)
            
            # 存入缓存
            await cache.set(cache_key, result, ttl)
            
            return result
        
        return wrapper
    return decorator
