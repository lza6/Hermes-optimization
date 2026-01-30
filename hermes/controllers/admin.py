from fastapi import APIRouter, HTTPException, Query, Body, Response
from typing import Optional, List
import time
import json

from ..models.schemas import *
from ..services.provider_manager import ProviderManagerService
from ..services.log_service import LogService
from ..services.auth_service import AuthService
from ..services.config_service import ConfigService
from ..services.dispatcher_service import DispatcherService
from ..services.circuit_breaker import circuit_breaker
from ..services.cache_service import CacheService
from ..utils.logger import logger
from ..config import config

router = APIRouter(prefix="/admin")

# Provider Management
@router.get("/providers")
async def get_providers():
    return {"data": await ProviderManagerService.get_all()}

@router.get("/providers/export")
async def export_providers():
    all_providers = await ProviderManagerService.get_all()
    providers = [
        {
            "name": p["name"],
            "baseUrl": p["baseUrl"],
            "apiKey": p["apiKey"],
            "modelBlacklist": p.get("modelBlacklist", [])
        }
        for p in all_providers
    ]
    return {
        "exportedAt": int(time.time() * 1000),
        "providers": providers
    }

@router.post("/providers")
async def add_provider(provider: ProviderCreate):
    try:
        new_provider = await ProviderManagerService.add_provider(
            provider.name, provider.baseUrl, provider.apiKey, provider.modelBlacklist
        )
        return {"success": True, "data": new_provider}
    except Exception as e:
        logger.error(f"添加提供商失败: {e}")
        return Response(content=json.dumps({"success": False, "error": str(e)}), status_code=500, media_type="application/json")

@router.patch("/providers/{id}")
async def update_provider(id: str, updates: ProviderUpdate):
    try:
        update_dict = updates.model_dump(exclude_unset=True)
        updated = await ProviderManagerService.update_provider(id, update_dict)
        return {"success": True, "data": updated}
    except Exception as e:
        logger.error(f"更新提供商失败: {e}")
        return Response(content=json.dumps({"success": False, "error": str(e)}), status_code=500, media_type="application/json")

@router.delete("/providers/{id}")
async def delete_provider(id: str):
    success = await ProviderManagerService.remove_provider(id)
    return {"success": success}

@router.post("/providers/{id}/resync")
async def resync_provider(id: str):
    try:
        await ProviderManagerService.trigger_resync(id)
        return {"success": True}
    except Exception as e:
        logger.error(f"手动重新同步失败: {e}")
        return Response(content=json.dumps({"success": False, "error": str(e)}), status_code=500, media_type="application/json")

@router.post("/providers/import")
async def import_providers(req: ProviderImportRequest):
    try:
        data = [p.model_dump() for p in req.providers]
        result = await ProviderManagerService.import_providers(data)
        return {"success": True, **result}
    except Exception as e:
        logger.error(f"导入提供商配置失败: {e}")
        return Response(content=json.dumps({"success": False, "error": str(e)}), status_code=500, media_type="application/json")


# Logs & Metrics
@router.get("/request-logs")
async def get_request_logs(
    page: int = 1,
    limit: int = 10,
    method: Optional[str] = None,
    path: Optional[str] = None,
    model: Optional[str] = None,
    status: Optional[int] = None
):
    offset = (page - 1) * limit
    filters = {}
    if method: filters["method"] = method
    if path: filters["path"] = path
    if model: filters["model"] = model
    if status is not None: filters["status"] = status
    
    return {"data": await LogService.get_recent_requests(limit, offset, filters)}

@router.get("/sync-logs")
async def get_sync_logs(
    page: int = 1,
    limit: int = 10,
    providerName: Optional[str] = None,
    model: Optional[str] = None,
    result: Optional[str] = Query(None, pattern="^(success|failure)$")
):
    offset = (page - 1) * limit
    filters = {}
    if providerName: filters["providerName"] = providerName
    if model: filters["model"] = model
    if result: filters["result"] = result
    
    return {"data": await LogService.get_recent_sync_logs(limit, offset, filters)}

@router.get("/metrics")
async def get_metrics():
    # LogService.get_metrics is partially sync (memory read) but kept standard
    # If we made it async, await it. I made it sync in LogService (re-read LogService.py)
    # Re-reading LogService.py from Step 55... 
    # Defined as: def get_metrics(cls): (SYNC)
    # So no await needed.
    return {"data": LogService.get_metrics()}


# Keys
@router.get("/keys")
async def get_keys(description: Optional[str] = None, id: Optional[str] = None):
    filters = {}
    if description: filters["description"] = description
    if id: filters["id"] = id
    return {"data": await AuthService.get_generated_keys(filters)}

@router.post("/keys/generate")
async def generate_key(req: KeyGenerateRequest):
    final_key = req.key if req.key else AuthService.generate_key()
    desc = req.description or 'Generated by Admin'
    
    generated_id = await AuthService.store_key(final_key, desc)
    return {
        "success": True,
        "id": generated_id,
        "key": final_key,
        "description": desc
    }


# Settings
@router.get("/settings/periodic-sync-interval-hours")
async def get_periodic_sync_interval():
    val = await ConfigService.get_number("periodicSyncIntervalHours", 1) # Default 1
    return {"intervalHours": val}

@router.post("/settings/periodic-sync-interval-hours")
async def set_periodic_sync_interval(req: PeriodicSyncIntervalRequest):
    if req.intervalHours <= 0:
        raise HTTPException(status_code=400, detail="间隔时间必须大于 0 小时")
    await ConfigService.set("periodicSyncIntervalHours", str(req.intervalHours))
    return {"success": True, "newIntervalHours": req.intervalHours}

@router.get("/settings/chat-max-retries")
async def get_chat_max_retries():
    val = await ConfigService.get_number("chatMaxRetries", 3)
    return {"maxRetries": val}

@router.post("/settings/chat-max-retries")
async def set_chat_max_retries(req: ChatMaxRetriesRequest):
    if req.maxRetries <= 0:
        raise HTTPException(status_code=400, detail="重试次数必须大于 0")
    await ConfigService.set("chatMaxRetries", str(req.maxRetries))
    return {"success": True, "maxRetries": req.maxRetries}

@router.get("/settings/dispatcher")
async def get_dispatcher_settings():
    return {
        "initialPenaltyMs": await ConfigService.get_number("dispatcher_initial_penalty_ms", 30 * 60_000),
        "maxPenaltyMs": await ConfigService.get_number("dispatcher_max_penalty_ms", 4 * 60 * 60_000),
        "resyncThreshold": await ConfigService.get_number("dispatcher_resync_threshold", 3),
        "resyncCooldownMs": await ConfigService.get_number("dispatcher_resync_cooldown_ms", 10 * 60_000)
    }

@router.post("/settings/dispatcher")
async def set_dispatcher_settings(req: DispatcherSettingsRequest):
    if req.initialPenaltyMs: await ConfigService.set("dispatcher_initial_penalty_ms", str(req.initialPenaltyMs))
    if req.maxPenaltyMs: await ConfigService.set("dispatcher_max_penalty_ms", str(req.maxPenaltyMs))
    if req.resyncThreshold: await ConfigService.set("dispatcher_resync_threshold", str(req.resyncThreshold))
    if req.resyncCooldownMs: await ConfigService.set("dispatcher_resync_cooldown_ms", str(req.resyncCooldownMs))
    return {"success": True}

@router.get("/dispatcher/cooldowns")
async def get_cooldowns():
    return {"data": await DispatcherService.get_cooldowns()}

@router.post("/dispatcher/cooldowns/clear")
async def clear_cooldown(req: ClearCooldownRequest):
    DispatcherService.clear_cooldown(req.providerId, req.modelName)
    return {"success": True}


# ========================================
# v4.0.0 新增: 断路器管理 API
# ========================================

@router.get("/circuit-breaker/status")
async def get_circuit_breaker_status():
    """获取所有断路器状态"""
    return {
        "data": circuit_breaker.get_all_status(),
        "config": {
            "failureThreshold": circuit_breaker.failure_threshold,
            "recoveryTimeout": circuit_breaker.recovery_timeout,
            "successThreshold": circuit_breaker.success_threshold
        }
    }

@router.get("/circuit-breaker/status/{key}")
async def get_circuit_breaker_status_by_key(key: str):
    """获取指定断路器状态"""
    return {"data": circuit_breaker.get_status(key)}

@router.post("/circuit-breaker/reset/{key}")
async def reset_circuit_breaker(key: str):
    """重置指定断路器"""
    await circuit_breaker.reset(key)
    return {"success": True, "key": key}


# ========================================
# v4.0.0 新增: 缓存管理 API
# ========================================

@router.get("/cache/stats")
async def get_cache_stats():
    """获取缓存统计信息"""
    return {"data": CacheService.get_all_stats()}

@router.post("/cache/clear")
async def clear_cache():
    """清空所有缓存"""
    await CacheService.invalidate_providers()
    return {"success": True, "message": "所有缓存已清空"}

@router.post("/cache/clear/providers")
async def clear_providers_cache():
    """仅清空供应商缓存"""
    cache = await CacheService.get_providers_cache()
    await cache.clear()
    return {"success": True, "message": "供应商缓存已清空"}

@router.post("/cache/clear/models")
async def clear_models_cache():
    """仅清空模型列表缓存"""
    cache = await CacheService.get_models_cache()
    await cache.clear()
    return {"success": True, "message": "模型列表缓存已清空"}
