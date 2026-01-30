import time
import asyncio
import random
import httpx
from typing import List, Optional, Tuple, Any

from ..config import config
from ..utils.logger import logger
from .log_service import LogService
from .config_service import ConfigService
from .routing_score_service import RoutingScoreService
from .circuit_breaker import circuit_breaker, CircuitOpenError
from ..utils.model_normalizer import build_model_alias_maps, normalize_model_name

class DispatcherService:
    _cooldowns = {} # key: providerId:model -> {until, backoffMs, force}
    _penalty_counts = {} # key -> {count, lastResync}

    # Properties are fine calling async ConfigService inside an async context, 
    # but as properties they are sync. 
    # Better to make them helpers or just await ConfigService where used.
    # For now, let's keep it simple: await ConfigService.get_number() at usage site.

    @classmethod
    def _key(cls, provider_id: str, model: str):
        return f"{provider_id}:{model}"

    @classmethod
    async def get_cooldowns(cls):
        from .provider_manager import ProviderManagerService
        all_providers = await ProviderManagerService.get_all()
        providers = {p["id"]: p["name"] for p in all_providers}
        
        result = []
        now = int(time.time() * 1000)
        for key, val in cls._cooldowns.items():
            pid, model = key.split(":", 1)
            result.append({
                "providerId": pid,
                "providerName": providers.get(pid, pid),
                "modelName": model,
                "until": val["until"],
                "backoffMs": val["backoffMs"],
                "remainingMs": max(0, val["until"] - now)
            })
        
        return sorted(result, key=lambda x: x["remainingMs"], reverse=True)

    @classmethod
    async def _set_cooldown(cls, provider_id: str, model_name: str, backoff_ms: int, force: bool = False):
        until = int(time.time() * 1000) + backoff_ms
        key = cls._key(provider_id, model_name)
        cls._cooldowns[key] = {"until": until, "backoffMs": backoff_ms, "force": force}
        
        from .provider_manager import ProviderManagerService
        all_providers = await ProviderManagerService.get_all()
        p_name = next((p["name"] for p in all_providers if p["id"] == provider_id), provider_id)
        
        await LogService.track_cooldown(provider_id, p_name, model_name)
        logger.warning(f"[Dispatcher] 暂停上游: provider={provider_id} model={model_name} 直到 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until/1000))} (backoff={backoff_ms}ms)")

    @classmethod
    def clear_cooldown(cls, provider_id: str, model_name: str):
        # Sync method is fine for clearing dict
        key = cls._key(provider_id, model_name)
        if key in cls._cooldowns:
            del cls._cooldowns[key]
            logger.info(f"[Dispatcher] 解除冷却 (同步成功): provider={provider_id} model={model_name}")

    @classmethod
    async def penalize(cls, provider_id: str, model_name: str, duration_ms: int = None, force: bool = False):
        # Config access
        INITIAL_PENALTY_MS = await ConfigService.get_number("dispatcher_initial_penalty_ms", 30 * 60_000)
        MAX_PENALTY_MS = await ConfigService.get_number("dispatcher_max_penalty_ms", 4 * 60 * 60_000)
        RESYNC_THRESHOLD = await ConfigService.get_number("dispatcher_resync_threshold", 3)
        RESYNC_COOLDOWN_MS = await ConfigService.get_number("dispatcher_resync_cooldown_ms", 10 * 60_000)

        if duration_ms is None: duration_ms = INITIAL_PENALTY_MS
        
        key = cls._key(provider_id, model_name)
        existing = cls._cooldowns.get(key)
        
        backoff = min(existing["backoffMs"] * 2, MAX_PENALTY_MS) if existing else max(duration_ms, INITIAL_PENALTY_MS)
        
        await cls._set_cooldown(provider_id, model_name, backoff, force)
        
        # Penalty counting for resync trigger
        penalty = cls._penalty_counts.get(key, {"count": 0, "lastResync": None})
        penalty["count"] += 1
        now = int(time.time() * 1000)
        
        should_resync = (penalty["count"] >= RESYNC_THRESHOLD) and \
                        (not penalty["lastResync"] or (now - penalty["lastResync"]) > RESYNC_COOLDOWN_MS)
                        
        if should_resync:
            try:
                from .provider_manager import ProviderManagerService
                await ProviderManagerService.trigger_resync(provider_id)
                penalty["count"] = 0
                penalty["lastResync"] = now
                logger.warning(f"[Dispatcher] 惩罚达阈值，触发重新同步模型列表: provider={provider_id} model={model_name}")
            except Exception as e:
                logger.error(f"[Dispatcher] 触发重新同步失败: provider={provider_id} {e}")
        
        cls._penalty_counts[key] = penalty

    @classmethod
    async def _probe_model(cls, provider: dict, model_name: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{provider['baseUrl']}/chat/completions",
                    headers={"Authorization": f"Bearer {provider['apiKey']}"},
                    json={
                        "model": model_name,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1
                    }
                )
                return resp.is_success
        except:
            return False

    @classmethod
    async def _is_available(cls, provider: dict, model_name: str) -> bool:
        key = cls._key(provider["id"], model_name)
        
        # v4.0.0: 首先检查断路器状态
        circuit_key = f"provider:{provider['id']}"
        if not await circuit_breaker.is_allowed(circuit_key):
            logger.info(f"[Dispatcher] 断路器熔断中，跳过供应商: provider={provider['id']}")
            return False
        
        entry = cls._cooldowns.get(key)
        
        # We need MAX_PENALTY_MS for setting cooldown again if probe fails
        MAX_PENALTY_MS = await ConfigService.get_number("dispatcher_max_penalty_ms", 4 * 60 * 60_000)

        RECENT_SYNC_THRESHOLD = 5 * 60 * 1000
        last_synced = provider.get("lastSyncedAt")
        
        if not entry and last_synced and (int(time.time() * 1000) - last_synced < RECENT_SYNC_THRESHOLD):
             return True
             
        if entry and not entry.get("force") and last_synced and (int(time.time() * 1000) - last_synced < RECENT_SYNC_THRESHOLD):
             del cls._cooldowns[key]
             logger.info(f"[Dispatcher] 信任后台同步结果，强制解除冷却: provider={provider['id']}")
             return True
             
        if not entry: return True
        
        now = int(time.time() * 1000)
        if entry["until"] > now: return False
        
        # Self-healing probe
        ok = await cls._probe_model(provider, model_name)
        if ok:
            del cls._cooldowns[key]
            logger.info(f"[Dispatcher] 上游恢复: provider={provider['id']} model={model_name}")
            return True
            
        next_backoff = min(entry["backoffMs"] * 2, MAX_PENALTY_MS)
        await cls._set_cooldown(provider["id"], model_name, next_backoff)
        return False

    @classmethod
    async def get_provider_for_model(cls, model_name: str, excluded_ids: List[str] = []) -> Optional[Tuple[dict, str]]:
        from .provider_manager import ProviderManagerService
        all_providers = await ProviderManagerService.get_all()
        
        alias_maps = build_model_alias_maps(all_providers)
        normalized_input = normalize_model_name(model_name).canonical or model_name
        canonical = alias_maps.variant_to_canonical.get(normalized_input) or normalized_input
        variants = alias_maps.canonical_to_variants.get(canonical, {model_name})
        variant_list = list(variants)
        
        candidates = [
            p for p in all_providers
            if (p["status"] == 'active' or p["status"] == 'syncing') and
            any(v in p["models"] for v in variant_list) and
            p["id"] not in excluded_ids
        ]
        
        if not candidates:
            reason = "所有支持该模型的活跃提供商都已尝试失败" if excluded_ids else "未找到支持该模型的活跃提供商"
            logger.warning(f"{reason}: {model_name}")
            return None
            
        scored = []
        for provider in candidates:
            available_models = [v for v in variant_list if v in provider["models"]]
            resolved_model = random.choice(available_models) if available_models else model_name
            
            available = await cls._is_available(provider, resolved_model)
            if not available:
                logger.info(f"[Dispatcher] provider={provider['id']} ({provider['name']}) 冷却中，跳过")
                continue
            
            score = RoutingScoreService.score_for(provider["id"], resolved_model)
            scored.append({"provider": provider, "resolvedModel": resolved_model, "score": score})
            
        if not scored:
            logger.warning(f"所有支持模型 {model_name} 的上游均在冷却中")
            return None
            
        # Sort desc
        scored.sort(key=lambda x: x["score"], reverse=True)
        picked = scored[0]
        
        logger.info(f"[Dispatcher] 选择上游: provider={picked['provider']['id']} ({picked['provider']['name']}) model={picked['resolvedModel']} (requested={model_name}) score={picked['score']:.3f}")
        return (picked["provider"], picked["resolvedModel"])

    @classmethod
    async def get_max_penalty_ms(cls):
        return await ConfigService.get_number("dispatcher_max_penalty_ms", 4 * 60 * 60_000)
