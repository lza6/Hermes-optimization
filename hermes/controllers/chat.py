from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict
import time

from ..models.schemas import ChatCompletionRequest
from ..services.auth_service import AuthService
from ..services.provider_manager import ProviderManagerService
from ..services.dispatcher_service import DispatcherService
from ..services.proxy_service import ProxyService
from ..services.cache_service import CacheService
from ..utils.model_normalizer import build_model_alias_maps
from ..config import config
from ..utils.logger import logger
from ..services.config_service import ConfigService

router = APIRouter(prefix="/v1")

@router.get("/models")
async def get_models(authorization: Optional[str] = Header(None)):
    if not await AuthService.validate_key(authorization):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "提供的 Hermes Key 无效 (Invalid Hermes Key provided).",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key"
                }
            }
        )
    
    # v4.0.0: 使用缓存减少重复计算
    cache = await CacheService.get_models_cache()
    cache_key = "models:list"
    
    cached_result = await cache.get(cache_key)
    if cached_result is not None:
        return cached_result
    
    providers = await ProviderManagerService.get_all()
    unique_models = set()
    
    alias_maps = build_model_alias_maps(providers)
    for canonical in alias_maps.canonical_to_variants:
        unique_models.add(canonical)
    
    result = {
        "object": "list",
        "data": [
            {
                "id": mid,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "hermes-gateway"
            }
            for mid in sorted(unique_models)  # 排序以保持一致性
        ]
    }
    
    # 存入缓存
    await cache.set(cache_key, result)
    
    return result

@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    payload: ChatCompletionRequest,
    authorization: Optional[str] = Header(None)
):
    # Set model for middleware logging
    request.state.model = payload.model

    if not await AuthService.validate_key(authorization):
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "message": "提供的 Hermes Key 无效 (Invalid Hermes Key provided).",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key"
                }
            }
        )
        
    # Smart Retry Logic
    max_retries = max(1, await ConfigService.get_number("chatMaxRetries", 3) or 3)
    
    tried_provider_ids = set()
    last_error_response = None
    last_error = None
    
    for attempt in range(1, max_retries + 1):
        selection = await DispatcherService.get_provider_for_model(payload.model, list(tried_provider_ids))
        
        if not selection:
            if attempt == 1:
                return JSONResponse(
                    status_code=404,
                    content={
                        "error": {
                            "message": f"没有上游服务站支持模型 '{payload.model}' (Model not supported).",
                            "type": "invalid_request_error",
                            "code": "model_not_found"
                        }
                    }
                )
            logger.warning(f"模型 {payload.model} 重试耗尽，无更多可用节点")
            break
            
        provider, resolved_model = selection
        tried_provider_ids.add(provider["id"])
        
        try:
            # Prepare payload with resolved model
            forward_payload = payload.model_dump(exclude_unset=True)
            forward_payload["model"] = resolved_model
            
            response = await ProxyService.forward_request(provider, forward_payload)
            
            if response.status_code >= 200 and response.status_code < 300:
                try:
                    # Async fire-and-forget logging for success (or keep it simple)
                    pass
                except:
                    pass
                return response
            
            logger.warning(f"Provider {provider['name']} 返回错误 {response.status_code}，准备重试...")
            last_error_response = response
            continue
            
        except Exception as e:
            logger.error(f"Provider {provider['name']} 连接失败: {e}")
            last_error = e
            continue

    if last_error_response:
        return last_error_response
        
    return JSONResponse(
        status_code=502,
        content={
            "error": {
                "message": "所有上游提供商均无法响应 (All upstream providers failed).",
                "type": "api_error",
                "code": "upstream_error",
                "last_error": str(last_error) if last_error else None
            }
        }
    )
