import aiosqlite
import os
import asyncio
from typing import Optional, List
from contextlib import asynccontextmanager
from .config import config
from .utils.logger import logger

DB_PATH = config.DB_PATH

# ========================================
# 全局连接池管理 (Singleton Connection Pool)
# 使用单例模式管理数据库连接，避免频繁创建/销毁
# ========================================
_pool: Optional[aiosqlite.Connection] = None
_pool_lock = asyncio.Lock()
_pool_healthy = True


async def get_pool() -> aiosqlite.Connection:
    """
    获取全局连接池实例。
    使用锁确保线程安全，自动创建连接并配置优化参数。
    """
    global _pool, _pool_healthy
    async with _pool_lock:
        # 如果连接不存在或不健康，重新创建
        if _pool is None or not _pool_healthy:
            try:
                if _pool is not None:
                    try:
                        await _pool.close()
                    except:
                        pass
                
                _pool = await aiosqlite.connect(DB_PATH, isolation_level=None)
                _pool.row_factory = aiosqlite.Row
                
                # 优化 SQLite 性能配置
                await _pool.execute("PRAGMA journal_mode=WAL")        # WAL 模式提高并发
                await _pool.execute("PRAGMA synchronous=NORMAL")      # 平衡性能与安全
                await _pool.execute("PRAGMA cache_size=-64000")       # 64MB 缓存
                await _pool.execute("PRAGMA temp_store=MEMORY")       # 临时表存内存
                await _pool.execute("PRAGMA mmap_size=268435456")     # 256MB 内存映射
                
                _pool_healthy = True
                logger.info("Database connection pool initialized with optimized settings")
            except Exception as e:
                logger.error(f"Failed to create database connection: {e}")
                _pool_healthy = False
                raise
        
        return _pool


async def check_pool_health() -> bool:
    """检查连接池健康状态"""
    global _pool_healthy
    try:
        pool = await get_pool()
        async with pool.execute("SELECT 1") as cursor:
            await cursor.fetchone()
        _pool_healthy = True
        return True
    except Exception as e:
        logger.warning(f"Database health check failed: {e}")
        _pool_healthy = False
        return False


async def close_pool():
    """关闭连接池（用于应用关闭时）"""
    global _pool, _pool_healthy
    async with _pool_lock:
        if _pool is not None:
            try:
                await _pool.close()
            except:
                pass
            _pool = None
            _pool_healthy = False
            logger.info("Database connection pool closed")

async def get_db() -> aiosqlite.Connection:
    """
    Factory to get a new DB connection. 
    Usage: async with get_db() as db: ...
    """
    conn = await aiosqlite.connect(DB_PATH)
    conn.row_factory = aiosqlite.Row
    return conn

async def init_db():
    logger.info(f"Connecting to SQLite database at {DB_PATH} (Async)...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Enable usage of row['name']
        db.row_factory = aiosqlite.Row
        
        # Enable WAL mode for better concurrency
        await db.execute("PRAGMA journal_mode = WAL;")
        
        # Create Tables
        # Providers Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            baseUrl TEXT NOT NULL,
            apiKey TEXT NOT NULL,
            models TEXT DEFAULT '[]', -- JSON string
            status TEXT DEFAULT 'pending',
            lastSyncedAt INTEGER,
            lastUsedAt INTEGER,
            createdAt INTEGER,
            modelBlacklist TEXT DEFAULT '[]'
        );
        """)

        # Sync Logs Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS sync_logs (
            id TEXT PRIMARY KEY,
            providerId TEXT,
            providerName TEXT,
            model TEXT,
            result TEXT, -- 'success' | 'failure'
            message TEXT,
            createdAt INTEGER
        );
        """)

        # Request Logs Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS request_logs (
            id TEXT PRIMARY KEY,
            method TEXT,
            path TEXT,
            model TEXT,
            status INTEGER,
            duration INTEGER,
            ip TEXT,
            createdAt INTEGER
        );
        """)

        # Hermes Keys Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS hermes_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT NOT NULL,
            description TEXT,
            createdAt INTEGER,
            lastUsedAt INTEGER
        );
        """)

        # Settings Table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """)

        # Metrics Tables
        await db.execute("""
        CREATE TABLE IF NOT EXISTS metrics_counters (
            key TEXT PRIMARY KEY,
            value INTEGER
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS metrics_models (
            model TEXT PRIMARY KEY,
            count INTEGER
        );
        """)

        await db.execute("""
        CREATE TABLE IF NOT EXISTS metrics_providers (
            id TEXT PRIMARY KEY,
            name TEXT,
            count INTEGER,
            errors INTEGER
        );
        """)
        
        # Migrations / Column Checks
        async with db.execute("PRAGMA table_info(providers)") as cursor:
            columns = [row['name'] for row in await cursor.fetchall()]
        
        if "lastUsedAt" not in columns:
            await db.execute("ALTER TABLE providers ADD COLUMN lastUsedAt INTEGER;")
        
        if "modelBlacklist" not in columns:
            await db.execute("ALTER TABLE providers ADD COLUMN modelBlacklist TEXT DEFAULT '[]';")

        await db.commit()
        logger.info("SQLite database initialized successfully (Async).")

# ========================================
# 数据库辅助函数 (使用连接池优化)
# ========================================

async def execute_query(sql: str, params: tuple = ()) -> None:
    """执行写入查询 (INSERT/UPDATE/DELETE)，使用连接池"""
    try:
        pool = await get_pool()
        await pool.execute(sql, params)
        await pool.commit()
    except aiosqlite.OperationalError as e:
        # 连接异常时标记为不健康，下次获取时会重新创建
        global _pool_healthy
        _pool_healthy = False
        logger.error(f"Database operation error: {e}")
        raise


async def fetch_one(sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
    """获取单条记录，使用连接池"""
    try:
        pool = await get_pool()
        async with pool.execute(sql, params) as cursor:
            return await cursor.fetchone()
    except aiosqlite.OperationalError as e:
        global _pool_healthy
        _pool_healthy = False
        logger.error(f"Database fetch error: {e}")
        raise


async def fetch_all(sql: str, params: tuple = ()) -> List[aiosqlite.Row]:
    """获取多条记录，使用连接池"""
    try:
        pool = await get_pool()
        async with pool.execute(sql, params) as cursor:
            return await cursor.fetchall()
    except aiosqlite.OperationalError as e:
        global _pool_healthy
        _pool_healthy = False
        logger.error(f"Database fetch error: {e}")
        raise

