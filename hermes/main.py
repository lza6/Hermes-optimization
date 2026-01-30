import time
import asyncio
import os
import uuid
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from .config import config
from .database import init_db, close_pool, check_pool_health
from .utils.logger import logger
from .controllers import chat, admin
from .services.log_service import LogService, LogBatcher
from .services.provider_manager import ProviderManagerService
from .services.rate_limiter import SlidingWindowLimiter
from .services.cache_service import CacheService
from .services.circuit_breaker import circuit_breaker

# Rate Limiter Setup
limiter = Limiter(key_func=get_remote_address)

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info(f"正在初始化 Hermes AI 网关 v{config.VERSION} (Cosmic-Genesis 版)...")
    await init_db()
    await LogService.initialize()
    
    # v3.0.0: 初始化缓存服务
    CacheService.initialize()
    
    # v3.0.0: 启动日志批量写入器
    await LogBatcher.start()
    
    # Start periodic sync task
    asyncio.create_task(ProviderManagerService.start_periodic_sync())
    
    logger.info(f"Fox Hermes v{config.VERSION} 正在运行，端口：{config.PORT} 🚀")
    logger.info(f"控制中心访问地址：http://localhost:{config.PORT}/dashboard")
    
    yield
    
    # Shutdown - 优雅关闭清理资源
    logger.info("Hermes 网关正在关闭...")
    
    # v3.0.0: 停止日志批量写入器
    await LogBatcher.stop()
    
    # 关闭 HTTP 客户端池
    from .services.proxy_service import close_http_client
    await close_http_client()
    
    # 关闭数据库连接池
    await close_pool()
    logger.info("清理工作已完成。祝您有愉快的一天! 👋")

app = FastAPI(lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# v3.0.0: 请求追踪 ID 中间件
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """为每个请求添加追踪 ID"""
    trace_id = request.headers.get("X-Trace-ID") or str(uuid.uuid4())[:8]
    request.state.trace_id = trace_id
    
    response = await call_next(request)
    response.headers["X-Trace-ID"] = trace_id
    return response

# Global Request Logging Middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    # Request Logger
    path = request.url.path
    if path == "/v1/chat/completions":
        start_time = int(time.time() * 1000)
        
        response = await call_next(request)
        
        duration = int(time.time() * 1000) - start_time
        status = response.status_code
        ip = request.client.host
        
        # Model extracted from request state (set in controller)
        model = getattr(request.state, "model", None)
        trace_id = getattr(request.state, "trace_id", "-")
        
        # v5.0.0: 实时指标与持久化日志同步
        try:
            await LogService.log_request(
                method=request.method,
                path=path,
                status=status,
                duration=duration,
                model=model,
                ip=ip
            )
            # 记录延迟到内存样本
            LogService.record_latency(duration)
        except Exception as e:
            logger.error(f"日志中间件异常: {e}")
        
        logger.info(f"[{trace_id}] [{status}] {request.method} {path} - {duration}ms")
        return response
        
    return await call_next(request)

# ========================================
# 滑动窗口限流中间件 (Sliding Window Rate Limiter)
# 替代简单计数器，提供更平滑的限流效果
# ========================================
_rate_limiter = SlidingWindowLimiter(
    max_requests=int(os.getenv("RATE_LIMIT_MAX", 60)),
    window_seconds=int(os.getenv("RATE_LIMIT_WINDOW", 60)),
    slot_count=12  # 12个槽，每槽5秒
)

@app.middleware("http")
async def global_rate_limit(request: Request, call_next):
    # 跳过静态资源和健康检查
    path = request.url.path
    if path.startswith("/logo") or path.startswith("/Hermes") or path == "/health":
        return await call_next(request)
    
    ip = request.client.host if request.client else "unknown"
    result = await _rate_limiter.check(ip)
    
    if not result.allowed:
        return Response(
            content="请求频率超限 (请求过于频繁，请稍后再试)",
            status_code=429,
            headers={
                "Content-Type": "text/plain; charset=utf-8",
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(result.reset_at),
                "Retry-After": str(result.retry_after)
            }
        )
    
    response = await call_next(request)
    
    # 添加限流状态响应头（便于客户端监控配额）
    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(result.reset_at)
    
    return response


# 健康检查端点 (v4.0.0 增强)
@app.get("/health")
async def health_check():
    """v4.0.0 增强版健康检查：包含断路器、供应商、缓存状态"""
    db_healthy = await check_pool_health()
    
    # v4.0.0: 获取断路器状态摘要
    circuit_status = circuit_breaker.get_all_status()
    open_circuits = [k for k, v in circuit_status.items() if v.get("state") == "open"]
    half_open_circuits = [k for k, v in circuit_status.items() if v.get("state") == "half_open"]
    
    # v4.0.0: 获取供应商状态摘要
    try:
        providers = await ProviderManagerService.get_all()
        active_providers = len([p for p in providers if p.get("status") == "active"])
        total_providers = len(providers)
    except:
        active_providers = 0
        total_providers = 0
    
    # v4.0.0: 获取延迟统计
    latency_stats = LogService.get_latency_percentiles()
    
    # 判断整体健康状态
    overall_status = "healthy"
    if not db_healthy:
        overall_status = "unhealthy"
    elif open_circuits or active_providers == 0:
        overall_status = "degraded"
    
    return {
        "status": overall_status,
        "version": config.VERSION,
        "database": {
            "connected": db_healthy
        },
        "circuit_breaker": {
            "total": len(circuit_status),
            "open": len(open_circuits),
            "half_open": len(half_open_circuits),
            "open_keys": open_circuits if open_circuits else None
        },
        "providers": {
            "active": active_providers,
            "total": total_providers
        },
        "latency": latency_stats,
        "cache": CacheService.get_all_stats()
    }


# ========================================
# SSE 实时指标广播端点 (v5.0 COSMIC-GENESIS)
# ========================================
from fastapi.responses import StreamingResponse

@app.get("/admin/events")
async def sse_endpoint(request: Request):
    """
    SSE 通道：向前端推送实时指标和系统事件。
    """
    async def event_generator():
        queue = await LogService.subscribe()
        try:
            # 发送初始状态
            initial_data = json.dumps({
                "type": "init", 
                "data": LogService.get_realtime_stats(),
                "ts": time.time()
            })
            yield f"data: {initial_data}\n\n"
            
            while True:
                if await request.is_disconnected():
                    break
                
                try:
                    # 使用 wait_for 防止无限等待，以便检查连接状态
                    event = await asyncio.wait_for(queue.get(), timeout=5.0)
                    yield f"data: {event}\n\n"
                except asyncio.TimeoutError:
                    # 心跳
                    yield ": ping\n\n"
                    
        finally:
            await LogService.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

# Register Routers
app.include_router(chat.router)
app.include_router(admin.router)

from fastapi.templating import Jinja2Templates

# Setup Templates
templates = Jinja2Templates(directory="hermes/templates")

# UI Routes with Jinja2
@app.get("/dashboard")
async def dashboard(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

@app.get("/logs")
async def logs(request: Request):
    return templates.TemplateResponse("logs.html", {"request": request})

@app.get("/settings")
async def settings(request: Request):
    return templates.TemplateResponse("settings.html", {"request": request})

@app.get("/metrics")
async def metrics(request: Request):
    return templates.TemplateResponse("metrics.html", {"request": request})

@app.get("/chat")
async def chat_ui(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})

@app.get("/")
async def root(request: Request):
    return templates.TemplateResponse("dashboard.html", {"request": request})

# Static Files (Serve logo, etc from public)
if os.path.exists("public"):
    app.mount("/", StaticFiles(directory="public", html=False), name="public")



if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=config.PORT)
