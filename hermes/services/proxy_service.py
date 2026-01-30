"""
ProxyService - 代理转发服务 (v5.0 COSMIC-GENESIS)

负责将请求高并行转发到选定的上游供应商，具备以下特性：
- 极致连接复用：基于 HTTP/2 的全局异步连接池
- 全链路反馈：为贝叶斯路由引擎提供高精度时延与成功率信号
- 实时洞察：通过 LogService 同步触发 SSE 指标广播
- 智能熔断：集成 CircuitBreaker 防止故障蔓延
"""

import json
import time
import httpx
import asyncio
from typing import Optional, Dict
from fastapi.responses import StreamingResponse, Response, JSONResponse
from ..utils.logger import logger
from .log_service import LogService
from .routing_score_service import RoutingScoreService
from .dispatcher_service import DispatcherService
from .provider_manager import ProviderManagerService
from .circuit_breaker import circuit_breaker


# ========================================
# 全局 HTTP 客户端管理 (连接复用 + HTTP/2)
# ========================================
_http_client: Optional[httpx.AsyncClient] = None
_client_lock = asyncio.Lock()


async def get_http_client() -> httpx.AsyncClient:
    global _http_client
    async with _client_lock:
        if _http_client is None:
            _http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0),
                limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
                http2=True,
                follow_redirects=True
            )
            logger.info("Cosmic HTTP 连接池已初始化 (已开启 HTTP/2 支持)")
        return _http_client


async def close_http_client():
    global _http_client
    async with _client_lock:
        if _http_client is not None:
            await _http_client.aclose()
            _http_client = None


class ProxyService:
    """代理服务 - 路由执行与反馈回路"""
    
    @staticmethod
    async def forward_request(provider: Dict, payload: Dict):
        """核心转发逻辑，包含指标上报反馈"""
        url = f"{provider['baseUrl']}/chat/completions"
        model = payload.get("model")
        start_time = int(time.time() * 1000)
        
        # 预先记录活跃请求 (SSE)
        await LogService.track_usage(provider["id"], provider["name"], model)
        
        client = await get_http_client()
        circuit_key = f"provider:{provider['id']}"
        
        try:
            req = client.build_request(
                "POST", url,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {provider['apiKey']}"
                },
                json=payload
            )
            
            resp = await client.send(req, stream=True)
            
            # 处理 HTTP 级别错误 (4xx, 5xx)
            if not resp.is_success:
                duration = int(time.time() * 1000) - start_time
                await resp.aread()
                error_text = resp.text
                
                # 1. 更新贝叶斯评分 (标记失败)
                RoutingScoreService.update(provider["id"], model, False, duration)
                LogService.record_latency(duration)
                
                # 2. 统计上报与 SSE 触发
                await LogService.track_upstream_error(provider["id"], provider["name"], model)
                
                # 3. 熔断判定
                await circuit_breaker.record_failure(circuit_key)
                
                # 4. 特殊错误识别 (模型丢失或配额耗尽)
                is_out_of_sync = resp.status_code == 404 or "model_not_found" in error_text
                if is_out_of_sync:
                    await ProviderManagerService.handle_model_not_found(provider["id"], model)
                
                logger.error(f"上游供应商错误 [{provider['name']}]: {resp.status_code} | {error_text[:100]}")
                await resp.aclose()
                return Response(content=error_text, status_code=resp.status_code, media_type="application/json")

            # 成功路径：流式输出
            if payload.get("stream"):
                async def stream_generator():
                    try:
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                        # 流正常结束后反馈成功
                        duration = int(time.time() * 1000) - start_time
                        RoutingScoreService.update(provider["id"], model, True, duration)
                        LogService.record_latency(duration)
                        await circuit_breaker.record_success(circuit_key)
                    except Exception as e:
                        RoutingScoreService.update(provider["id"], model, False, 0)
                        LogService.record_latency(0)
                        logger.warning(f"流式传输中断 [{provider['name']}]: {e}")
                    finally:
                        await resp.aclose()

                return StreamingResponse(
                    stream_generator(),
                    media_type="text/event-stream",
                    headers={
                        "X-Accel-Buffering": "no", 
                        "X-Hermes-Provider": provider["id"],
                        "X-Hermes-Model": model
                    }
                )
            
            # 成功路径：全量 JSON
            try:
                data = await resp.json()
            except:
                data = {"content": await resp.text()}
            await resp.aclose()
            
            duration = int(time.time() * 1000) - start_time
            RoutingScoreService.update(provider["id"], model, True, duration)
            LogService.record_latency(duration)
            await circuit_breaker.record_success(circuit_key)
            
            # 获取当前路由评分用于展示
            score = RoutingScoreService.score_for(provider["id"], model)
            
            res = JSONResponse(data)
            res.headers.update({
                "X-Hermes-Provider": provider["id"],
                "X-Hermes-Model": model,
                "X-Hermes-Latency": f"{duration}ms",
                "X-Hermes-Score": f"{score:.4f}"
            })
            return res
            
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            duration = int(time.time() * 1000) - start_time
            RoutingScoreService.update(provider["id"], model, False, duration)
            LogService.record_latency(duration)
            await LogService.track_upstream_error(provider["id"], provider["name"], model)
            await circuit_breaker.record_failure(circuit_key)
            logger.error(f"与供应商 {provider['name']} 的连接出现异常: {type(e).__name__}")
            raise
            
        except Exception as e:
            RoutingScoreService.update(provider["id"], model, False, 0)
            LogService.record_latency(0)
            await LogService.track_upstream_error(provider["id"], provider["name"], model)
            await circuit_breaker.record_failure(circuit_key)
            logger.critical(f"非预期的代理转发故障: {e}")
            raise
