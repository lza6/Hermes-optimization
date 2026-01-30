"""
滑动窗口限流器 (Sliding Window Rate Limiter)

采用滑动窗口算法实现请求限流，相比简单的固定窗口计数器：
- 更平滑的限流效果，避免窗口边界突发
- 支持多维度限流（IP、API Key）
- 提供详细的限流状态信息
"""

import time
import asyncio
from dataclasses import dataclass
from typing import Dict, Optional
from collections import defaultdict


@dataclass
class RateLimitResult:
    """限流检查结果"""
    allowed: bool           # 请求是否被允许
    limit: int              # 窗口内最大请求数
    remaining: int          # 窗口内剩余可用请求数
    reset_at: int           # 窗口重置时间戳（秒）
    retry_after: int        # 如被限流，建议等待秒数


class SlidingWindowLimiter:
    """
    滑动窗口限流器
    
    算法原理：
    将时间窗口分为多个小槽(slot)，每个槽记录该时间段的请求数。
    检查时计算当前时间前 window_seconds 秒内所有槽的请求总数。
    
    优势：相比固定窗口，避免了窗口边界的请求激增问题。
    """
    
    def __init__(
        self,
        max_requests: int = 60,
        window_seconds: int = 60,
        slot_count: int = 12,  # 将窗口分为12个槽，每槽5秒
        cleanup_interval: int = 300  # 每5分钟清理过期数据
    ):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.slot_count = slot_count
        self.slot_duration = window_seconds / slot_count
        
        # 存储结构: {key: {slot_index: count}}
        self._windows: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
        self._last_cleanup = time.time()
        self._cleanup_interval = cleanup_interval
        self._lock = asyncio.Lock()
    
    def _current_slot(self) -> int:
        """获取当前时间对应的槽索引"""
        return int(time.time() / self.slot_duration)
    
    def _get_window_slots(self) -> range:
        """获取当前窗口内的所有槽索引"""
        current = self._current_slot()
        return range(current - self.slot_count + 1, current + 1)
    
    async def _cleanup_if_needed(self):
        """定期清理过期的条目以释放内存"""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        current_slot = self._current_slot()
        min_valid_slot = current_slot - self.slot_count
        
        keys_to_delete = []
        for key, slots in self._windows.items():
            # 删除过期的槽
            expired_slots = [s for s in slots if s < min_valid_slot]
            for slot in expired_slots:
                del slots[slot]
            
            # 如果所有槽都被删除，标记整个 key 为待删除
            if not slots:
                keys_to_delete.append(key)
        
        for key in keys_to_delete:
            del self._windows[key]
        
        self._last_cleanup = now
    
    async def check(self, key: str) -> RateLimitResult:
        """
        检查并记录一个请求
        
        Args:
            key: 限流键（如 IP 地址或 API Key）
        
        Returns:
            RateLimitResult: 限流检查结果
        """
        async with self._lock:
            await self._cleanup_if_needed()
            
            current_slot = self._current_slot()
            window_slots = self._get_window_slots()
            
            # 计算当前窗口内的请求总数
            window = self._windows[key]
            current_count = sum(window.get(slot, 0) for slot in window_slots)
            
            # 计算窗口重置时间
            reset_at = int((current_slot + 1) * self.slot_duration)
            
            if current_count >= self.max_requests:
                # 被限流
                return RateLimitResult(
                    allowed=False,
                    limit=self.max_requests,
                    remaining=0,
                    reset_at=reset_at,
                    retry_after=max(1, reset_at - int(time.time()))
                )
            
            # 允许请求，增加计数
            window[current_slot] += 1
            
            return RateLimitResult(
                allowed=True,
                limit=self.max_requests,
                remaining=self.max_requests - current_count - 1,
                reset_at=reset_at,
                retry_after=0
            )
    
    async def get_status(self, key: str) -> RateLimitResult:
        """获取指定 key 的当前限流状态（不增加计数）"""
        async with self._lock:
            window_slots = self._get_window_slots()
            window = self._windows[key]
            current_count = sum(window.get(slot, 0) for slot in window_slots)
            
            current_slot = self._current_slot()
            reset_at = int((current_slot + 1) * self.slot_duration)
            
            return RateLimitResult(
                allowed=current_count < self.max_requests,
                limit=self.max_requests,
                remaining=max(0, self.max_requests - current_count),
                reset_at=reset_at,
                retry_after=0 if current_count < self.max_requests else max(1, reset_at - int(time.time()))
            )
    
    async def reset(self, key: str):
        """重置指定 key 的计数"""
        async with self._lock:
            if key in self._windows:
                del self._windows[key]
    
    def get_all_keys(self) -> list:
        """获取所有当前被追踪的 key 列表"""
        return list(self._windows.keys())


# 全局限流器实例 (可在 main.py 中配置化)
default_limiter = SlidingWindowLimiter(max_requests=60, window_seconds=60)
