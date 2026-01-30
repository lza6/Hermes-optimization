"""
CircuitBreaker - 断路器服务

实现三态断路器模式，用于故障隔离和快速失败：
- CLOSED: 正常状态，请求正常通过
- OPEN: 熔断状态，快速拒绝请求
- HALF_OPEN: 半开状态，允许探测请求

v3.0.0 新增
"""

import time
import asyncio
from typing import Dict, Optional, Callable, Any
from enum import Enum
from dataclasses import dataclass, field

from ..config import config
from ..utils.logger import logger


class CircuitState(Enum):
    """断路器状态"""
    CLOSED = "closed"      # 正常
    OPEN = "open"          # 熔断
    HALF_OPEN = "half_open"  # 半开


@dataclass
class CircuitStats:
    """断路器统计"""
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    last_failure_time: float = 0
    opened_at: float = 0
    
    def reset(self):
        """重置计数器"""
        self.failure_count = 0
        self.success_count = 0


class CircuitOpenError(Exception):
    """断路器打开时抛出的异常"""
    def __init__(self, key: str, retry_after: int):
        self.key = key
        self.retry_after = retry_after
        super().__init__(f"Circuit breaker open for {key}, retry after {retry_after}s")


class CircuitBreaker:
    """
    断路器实现
    
    状态转换：
    CLOSED -> OPEN: 失败次数达到阈值
    OPEN -> HALF_OPEN: 恢复超时后
    HALF_OPEN -> CLOSED: 探测成功
    HALF_OPEN -> OPEN: 探测失败
    """
    
    def __init__(
        self,
        failure_threshold: int = None,
        recovery_timeout: int = None,
        success_threshold: int = 2  # 半开状态需要连续成功次数
    ):
        self.failure_threshold = failure_threshold or config.CIRCUIT_FAILURE_THRESHOLD
        self.recovery_timeout = recovery_timeout or config.CIRCUIT_RECOVERY_TIMEOUT
        self.success_threshold = success_threshold
        
        self._circuits: Dict[str, CircuitStats] = {}
        self._lock = asyncio.Lock()
    
    def _get_stats(self, key: str) -> CircuitStats:
        """获取或创建断路器统计"""
        if key not in self._circuits:
            self._circuits[key] = CircuitStats()
        return self._circuits[key]
    
    async def is_allowed(self, key: str) -> bool:
        """检查请求是否被允许通过"""
        async with self._lock:
            stats = self._get_stats(key)
            now = time.time()
            
            if stats.state == CircuitState.CLOSED:
                return True
            
            if stats.state == CircuitState.OPEN:
                # 检查是否可以进入半开状态
                if now - stats.opened_at >= self.recovery_timeout:
                    stats.state = CircuitState.HALF_OPEN
                    stats.reset()
                    logger.info(f"[CircuitBreaker] {key} 进入半开状态，开始探测")
                    return True
                return False
            
            # HALF_OPEN 状态允许探测
            return True
    
    async def record_success(self, key: str):
        """记录成功"""
        async with self._lock:
            stats = self._get_stats(key)
            
            if stats.state == CircuitState.HALF_OPEN:
                stats.success_count += 1
                if stats.success_count >= self.success_threshold:
                    stats.state = CircuitState.CLOSED
                    stats.reset()
                    logger.info(f"[CircuitBreaker] {key} 恢复正常 (CLOSED)")
            elif stats.state == CircuitState.CLOSED:
                # 重置失败计数
                stats.failure_count = 0
    
    async def record_failure(self, key: str):
        """记录失败"""
        async with self._lock:
            stats = self._get_stats(key)
            stats.failure_count += 1
            stats.last_failure_time = time.time()
            
            if stats.state == CircuitState.HALF_OPEN:
                # 半开状态下失败，立即回到打开状态
                stats.state = CircuitState.OPEN
                stats.opened_at = time.time()
                logger.warning(f"[CircuitBreaker] {key} 探测失败，重新熔断")
            
            elif stats.state == CircuitState.CLOSED:
                if stats.failure_count >= self.failure_threshold:
                    stats.state = CircuitState.OPEN
                    stats.opened_at = time.time()
                    logger.warning(f"[CircuitBreaker] {key} 熔断! 失败次数: {stats.failure_count}")
    
    async def call(self, key: str, func: Callable, *args, **kwargs) -> Any:
        """
        通过断路器执行函数
        
        Args:
            key: 断路器标识
            func: 要执行的异步函数
            *args, **kwargs: 函数参数
        
        Returns:
            函数返回值
        
        Raises:
            CircuitOpenError: 断路器打开时
        """
        if not await self.is_allowed(key):
            stats = self._get_stats(key)
            retry_after = max(1, int(self.recovery_timeout - (time.time() - stats.opened_at)))
            raise CircuitOpenError(key, retry_after)
        
        try:
            result = await func(*args, **kwargs)
            await self.record_success(key)
            return result
        except Exception as e:
            await self.record_failure(key)
            raise
    
    async def reset(self, key: str):
        """手动重置断路器"""
        async with self._lock:
            if key in self._circuits:
                self._circuits[key] = CircuitStats()
                logger.info(f"[CircuitBreaker] {key} 已手动重置")
    
    def get_status(self, key: str) -> Dict:
        """获取断路器状态"""
        stats = self._get_stats(key)
        now = time.time()
        
        return {
            "key": key,
            "state": stats.state.value,
            "failure_count": stats.failure_count,
            "success_count": stats.success_count,
            "time_since_open": int(now - stats.opened_at) if stats.opened_at > 0 else 0,
            "recovery_timeout": self.recovery_timeout
        }
    
    def get_all_status(self) -> Dict[str, Dict]:
        """获取所有断路器状态"""
        return {key: self.get_status(key) for key in self._circuits}


# 全局断路器实例
circuit_breaker = CircuitBreaker()
