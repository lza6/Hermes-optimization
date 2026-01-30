"""
LogService - 日志和指标服务 (v5.0 COSMIC-GENESIS)

负责：
- 请求/同步日志记录
- 使用量统计与计算
- 实时事件广播 (SSE)
- 延迟百分位统计 (P50/P90/P99)
- 内存流式指标监控
"""

from ..database import execute_query, fetch_all, get_db, get_pool
from ..utils.logger import logger
from ..config import config
import time
import uuid
import asyncio
import json
from collections import deque
from typing import Dict, List, Optional, Tuple, Set
from dataclasses import dataclass, field


@dataclass
class LogEntry:
    """日志条目"""
    log_type: str  # 'request' or 'sync'
    data: Tuple
    created_at: float = field(default_factory=time.time)


class LogBatcher:
    """
    日志批量写入器 (Batch Writer)
    """
    _queue: deque = deque(maxlen=2000)
    _flush_task: asyncio.Task = None
    _lock = asyncio.Lock()
    _running = False
    
    @classmethod
    async def start(cls):
        if cls._running: return
        cls._running = True
        cls._flush_task = asyncio.create_task(cls._periodic_flush())
        logger.info(f"[日志批处理器] 已启动 (批大小={config.LOG_BATCH_SIZE}, 刷新间隔={config.LOG_FLUSH_INTERVAL}s)")
    
    @classmethod
    async def stop(cls):
        cls._running = False
        if cls._flush_task:
            cls._flush_task.cancel()
            try: await cls._flush_task
            except asyncio.CancelledError: pass
        await cls.flush()
    
    @classmethod
    async def add(cls, log_type: str, data: Tuple):
        async with cls._lock:
            cls._queue.append(LogEntry(log_type=log_type, data=data))
            if len(cls._queue) >= config.LOG_BATCH_SIZE:
                asyncio.create_task(cls.flush())
    
    @classmethod
    async def _periodic_flush(cls):
        while cls._running:
            try:
                await asyncio.sleep(config.LOG_FLUSH_INTERVAL)
                if cls._queue: await cls.flush()
            except asyncio.CancelledError: break
            except Exception as e: logger.error(f"[日志批处理器] 刷新错误: {e}")
    
    @classmethod
    async def flush(cls):
        if not cls._queue: return
        async with cls._lock:
            entries = list(cls._queue)
            cls._queue.clear()
        
        request_logs = [e.data for e in entries if e.log_type == 'request']
        sync_logs = [e.data for e in entries if e.log_type == 'sync']
        
        try:
            pool = await get_pool()
            if request_logs:
                await pool.executemany(
                    "INSERT INTO request_logs (id, method, path, model, status, duration, ip, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    request_logs
                )
            if sync_logs:
                await pool.executemany(
                    "INSERT INTO sync_logs (id, providerId, providerName, model, result, message, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    sync_logs
                )
            await pool.commit()
        except Exception as e:
            logger.error(f"[日志批处理器] 数据库写入错误: {e}")


class LogService:
    """日志和实时指标服务"""
    
    # 核心统计快照 (内存)
    _counters = {"upstream_errors": 0, "active_requests": 0, "total_requests": 0}
    _usage = {"models": {}, "providers": {}}
    _latency_samples: deque = deque(maxlen=200)
    
    # SSE 广播订阅者
    _listeners: Set[asyncio.Queue] = set()
    _broadcast_lock = asyncio.Lock()

    @classmethod
    async def initialize(cls):
        """从数据库冷启动加载历史指标"""
        try:
            # 加载计数器
            rows = await fetch_all("SELECT key, value FROM metrics_counters")
            for row in rows:
                if row["key"] == "upstreamErrors": 
                    cls._counters["upstream_errors"] = row["value"] or 0
            
            # 加载模型分布
            rows = await fetch_all("SELECT model, count FROM metrics_models")
            for row in rows:
                if row["model"]: cls._usage["models"][row["model"]] = row["count"] or 0
                
            # 加载总请求数
            row = await fetch_all("SELECT COUNT(*) as total FROM request_logs")
            if row: cls._counters["total_requests"] = row[0]["total"]
            
            logger.info("LogService 指标已从持久化存储中初始化完成。")
        except Exception as e:
            logger.error(f"指标初始化失败: {e}")

    @classmethod
    async def subscribe(cls) -> asyncio.Queue:
        """订阅实时事件流"""
        queue = asyncio.Queue(maxsize=100)
        async with cls._broadcast_lock:
            cls._listeners.add(queue)
        return queue

    @classmethod
    async def unsubscribe(cls, queue: asyncio.Queue):
        """取消订阅"""
        async with cls._broadcast_lock:
            if queue in cls._listeners:
                cls._listeners.remove(queue)

    @classmethod
    async def broadcast(cls, event_type: str, data: dict):
        """向所有连接的 SSE 客户端广播事件"""
        if not cls._listeners: return
        
        payload = json.dumps({"type": event_type, "data": data, "ts": time.time()})
        async with cls._broadcast_lock:
            disconnected = []
            for queue in cls._listeners:
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass
                except Exception:
                    disconnected.append(queue)
            
            for q in disconnected:
                cls._listeners.remove(q)

    @classmethod
    def get_realtime_stats(cls) -> dict:
        """生成当前的实时状态快照"""
        return {
            "counters": cls._counters.copy(),
            "latency": cls.get_latency_percentiles(),
            "usage": cls._usage.copy()
        }

    @classmethod
    async def track_usage(cls, provider_id: str, provider_name: str, model: str):
        cls._counters["total_requests"] += 1
        cls._usage["models"][model] = cls._usage["models"].get(model, 0) + 1
        
        p_stats = cls._usage["providers"].get(provider_id, {"count": 0, "name": provider_name})
        p_stats["count"] += 1
        cls._usage["providers"][provider_id] = p_stats
        
        # 触发广播
        await cls.broadcast("metrics_update", cls.get_realtime_stats())

    @classmethod
    async def track_upstream_error(cls, provider_id: str, provider_name: str, model: str):
        cls._counters["upstream_errors"] += 1
        await cls.broadcast("error", {"provider": provider_name, "model": model, "msg": "检测到上游供应商异常"})
        await cls.broadcast("metrics_update", cls.get_realtime_stats())

    @classmethod
    def record_latency(cls, duration_ms: int):
        cls._latency_samples.append(duration_ms)

    @classmethod
    def get_latency_percentiles(cls) -> dict:
        if not cls._latency_samples: return {"p50": 0, "p90": 0, "p99": 0}
        samples = sorted(list(cls._latency_samples))
        n = len(samples)
        return {
            "p50": samples[int(n*0.5)],
            "p90": samples[int(n*0.9)],
            "p99": samples[int(n*0.99)]
        }

    # 持久化相关 (使用 Batcher)
    @classmethod
    async def log_request(cls, **kwargs):
        data = (str(uuid.uuid4()), kwargs['method'], kwargs['path'], kwargs.get('model'), 
                kwargs['status'], kwargs['duration'], kwargs.get('ip'), int(time.time()*1000))
        await LogBatcher.add('request', data)
        # 通知更新
        await cls.broadcast("request", {"model": kwargs.get('model'), "duration": kwargs['duration'], "status": kwargs['status']})

    @classmethod
    def get_metrics(cls) -> dict:
        """获取当前指标数据"""
        return {
            "counters": cls._counters.copy(),
            "latency": cls.get_latency_percentiles(),
            "usage": cls._usage.copy()
        }

    @staticmethod
    async def get_recent_requests(limit=10, offset=0, filters=None):
        """获取请求日志，支持过滤"""
        if filters is None:
            filters = {}
        
        query = "SELECT * FROM request_logs WHERE 1=1"
        params = []
        
        if filters.get("method"):
            query += " AND method = ?"
            params.append(filters["method"])
        if filters.get("path"):
            query += " AND path LIKE ?"
            params.append(f"%{filters['path']}%")
        if filters.get("model"):
            query += " AND model = ?"
            params.append(filters["model"])
        if filters.get("status") is not None:
            query += " AND status = ?"
            params.append(filters["status"])
        
        query += " ORDER BY createdAt DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = await fetch_all(query, tuple(params))
        return [dict(r) for r in rows]

    @staticmethod
    async def get_recent_sync_logs(limit=10, offset=0, filters=None):
        """获取同步日志，支持过滤"""
        if filters is None:
            filters = {}
        
        query = "SELECT * FROM sync_logs WHERE 1=1"
        params = []
        
        if filters.get("providerName"):
            query += " AND providerName LIKE ?"
            params.append(f"%{filters['providerName']}%")
        if filters.get("model"):
            query += " AND model = ?"
            params.append(filters["model"])
        if filters.get("result"):
            query += " AND result = ?"
            params.append(filters["result"])
        
        query += " ORDER BY createdAt DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        
        rows = await fetch_all(query, tuple(params))
        return [dict(r) for r in rows]
