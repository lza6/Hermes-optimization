import json
import time
import asyncio
import httpx
import uuid
from typing import List, Dict, Any

from ..config import config
from ..database import fetch_all, execute_query, fetch_one, DB_PATH
import aiosqlite
from ..utils.logger import logger
from .log_service import LogService
from .dispatcher_service import DispatcherService
from .cache_service import CacheService

class ProviderManagerService:
    _periodic_sync_task = None
    _syncing = set() # provider_id set

    @classmethod
    async def get_all(cls, use_cache: bool = True) -> List[Dict]:
        """
        获取所有供应商列表。
        
        Args:
            use_cache: 是否使用缓存，默认 True
        """
        cache_key = "providers:all"
        
        # 尝试从缓存获取
        if use_cache:
            cache = await CacheService.get_providers_cache()
            cached = await cache.get(cache_key)
            if cached is not None:
                return cached
        
        # 从数据库获取
        rows = await fetch_all("SELECT * FROM providers ORDER BY createdAt DESC")
        result = [
            dict(
                id=row["id"],
                name=row["name"],
                baseUrl=row["baseUrl"],
                apiKey=row["apiKey"],
                models=json.loads(row["models"] or "[]"),
                modelBlacklist=json.loads(row["modelBlacklist"] or "[]"),
                status=row["status"],
                lastSyncedAt=row["lastSyncedAt"],
                lastUsedAt=row["lastUsedAt"],
                createdAt=row["createdAt"]
            ) for row in rows
        ]
        
        # 存入缓存
        if use_cache:
            cache = await CacheService.get_providers_cache()
            await cache.set(cache_key, result)
        
        return result

    @classmethod
    async def add_provider(cls, name: str, base_url: str, api_key: str, model_blacklist: List[str] = []) -> Dict:
        provider_id = str(uuid.uuid4())
        created_at = int(time.time() * 1000)
        cleaned_blacklist = [m.strip() for m in model_blacklist if m.strip()]
        
        provider = {
            "id": provider_id,
            "name": name,
            "baseUrl": base_url.rstrip("/"),
            "apiKey": api_key,
            "models": [],
            "modelBlacklist": cleaned_blacklist,
            "status": "pending",
            "createdAt": created_at
        }

        await execute_query("""
            INSERT INTO providers (id, name, baseUrl, apiKey, models, modelBlacklist, status, createdAt)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            provider["id"], provider["name"], provider["baseUrl"], provider["apiKey"],
            json.dumps(provider["models"]), json.dumps(provider["modelBlacklist"]),
            provider["status"], provider["createdAt"]
        ))

        # 失效缓存
        await CacheService.invalidate_providers()
        
        # Trigger background sync
        asyncio.create_task(cls.background_sync_task(provider))
        return provider

    @classmethod
    async def update_provider(cls, provider_id: str, updates: Dict) -> Dict:
        row = await fetch_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        if not row:
            raise Exception("Provider not found (找不到提供商)")
        
        existing = dict(row)
        
        name = updates.get("name", existing["name"])
        base_url = updates.get("baseUrl", existing["baseUrl"]).rstrip("/") if updates.get("baseUrl") else existing["baseUrl"]
        api_key = updates.get("apiKey", existing["apiKey"])
        
        model_blacklist = existing["modelBlacklist"]
        if updates.get("modelBlacklist") is not None:
            model_blacklist = json.dumps([m.strip() for m in updates["modelBlacklist"] if m.strip()])
        
        await execute_query("""
            UPDATE providers
            SET name = ?, baseUrl = ?, apiKey = ?, status = 'pending', models = '[]',
                modelBlacklist = ?, lastSyncedAt = NULL, lastUsedAt = ?
            WHERE id = ?
        """, (name, base_url, api_key, model_blacklist, int(time.time() * 1000), provider_id))
        
        # Fetch updated to return
        updated_row = await fetch_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        prov_dict = dict(updated_row)
        prov_dict["models"] = json.loads(prov_dict["models"] or "[]")
        prov_dict["modelBlacklist"] = json.loads(prov_dict["modelBlacklist"] or "[]")
        
        # 失效缓存
        await CacheService.invalidate_providers()
        
        # Trigger sync
        asyncio.create_task(cls.background_sync_task(prov_dict))
        return prov_dict

    @classmethod
    async def remove_provider(cls, provider_id: str) -> bool:
        async with aiosqlite.connect(DB_PATH) as db:
             cursor = await db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))
             await db.commit()
             deleted = cursor.rowcount > 0
        
        # 失效缓存
        if deleted:
            await CacheService.invalidate_providers()
        
        return deleted

    @classmethod
    async def trigger_resync(cls, provider_id: str):
        row = await fetch_one("SELECT * FROM providers WHERE id = ?", (provider_id,))
        if not row:
            raise Exception("Provider not found")
        
        provider = dict(row)
        provider["models"] = json.loads(provider["models"] or "[]")
        provider["modelBlacklist"] = json.loads(provider["modelBlacklist"] or "[]")
        
        asyncio.create_task(cls.background_sync_task(provider))

    @classmethod
    async def _update_provider_status(cls, provider_id: str, status: str, models: List[str] = None):
        now = int(time.time() * 1000)
        if models is not None:
            await execute_query("""
                UPDATE providers SET status = ?, models = ?, lastUsedAt = ?, lastSyncedAt = ? WHERE id = ?
            """, (status, json.dumps(models), now, now, provider_id))
        else:
            await execute_query("""
                UPDATE providers SET status = ? WHERE id = ?
            """, (status, provider_id))

    @classmethod
    async def background_sync_task(cls, provider: Dict):
        if provider["id"] in cls._syncing:
            logger.info(f"[后台任务] {provider['name']} 同步已在进行，跳过重复触发")
            return
        
        cls._syncing.add(provider["id"])
        logger.info(f"[后台任务] 开始为 {provider['name']} 同步模型...")
        
        await cls._update_provider_status(provider["id"], "syncing")
        
        try:
            raw_models = await cls._fetch_models_from_upstream(provider["baseUrl"], provider["apiKey"])
            models_to_check = list(set(raw_models))
            
            logger.info(f"[后台任务] {provider['name']} 名称筛选后候选数: {len(models_to_check)}")
            
            valid_models = []
            blacklist = provider.get("modelBlacklist", [])
            
            await cls._update_provider_status(provider["id"], "syncing", [])
            
            for model in models_to_check:
                if model in blacklist:
                    logger.info(f"[黑名单跳过] provider={provider['name']} model={model}")
                    continue
                if cls._is_non_chat_model(model):
                    logger.info(f"[非聊天模型跳过] provider={provider['name']} model={model}")
                    continue
                
                await asyncio.sleep(5) # Low RPM protection
                
                probe = await cls._verify_model(provider["baseUrl"], provider["apiKey"], model)
                
                if probe["ok"]:
                    valid_models.append(model)
                    logger.info(f"[检测通过] {model}")
                    await cls._update_provider_status(provider["id"], "syncing", valid_models)
                    DispatcherService.clear_cooldown(provider["id"], model)
                    
                    await LogService.log_sync(
                        provider["id"], provider["name"], model, "success",
                        f"模型返回状态 {probe.get('status')} (Model responded with {probe.get('status')})" if probe.get("status") else "Model is active"
                    )
                else:
                    msg = f"验证失败 (Verification failed) status={probe.get('status')} {probe.get('errorText')}"
                    logger.warning(f"[检测失败] {model} {msg}")
                    await LogService.log_sync(
                        provider["id"], provider["name"], model, "failure", msg
                    )
            
            logger.info(f"[后台任务] {provider['name']} 同步完成。最终可用: {len(valid_models)}")
            await cls._update_provider_status(provider["id"], "active", valid_models)
            
        except Exception as e:
            logger.error(f"[后台任务] {provider['name']} 同步失败: {e}")
            await cls._update_provider_status(provider["id"], "error")
            await LogService.log_sync(
                provider["id"], provider["name"], "ALL", "failure", f"同步过程失败 (Sync process failed): {str(e)}"
            )
        finally:
            cls._syncing.discard(provider["id"])

    @staticmethod
    def _is_non_chat_model(model: str) -> bool:
        lower = model.lower()
        return "embedding" in lower or "embed" in lower
    
    @classmethod
    async def handle_model_not_found(cls, provider_id: str, model: str) -> bool:
        row = await fetch_one("SELECT models FROM providers WHERE id = ?", (provider_id,))
        if not row:
            logger.warning(f"[ProviderManager] 无法处理 model_not_found，Provider 不存在: {provider_id}")
            return False
            
        models = json.loads(row["models"] or "[]")
        if model not in models:
            logger.info(f"[ProviderManager] model_not_found: Provider {provider_id} 未列出模型 {model}，略过剔除")
            return False
            
        next_models = [m for m in models if m != model]
        await execute_query("""
            UPDATE providers SET models = ?, status = 'syncing', lastUsedAt = ? WHERE id = ?
        """, (json.dumps(next_models), int(time.time() * 1000), provider_id))
        
        logger.warning(f"[ProviderManager] 上游回报 model_not_found，暂时移除模型并重同步: provider={provider_id} model={model}")
        
        # Trigger resync
        try:
            await cls.trigger_resync(provider_id)
        except Exception as e:
            logger.error(f"[ProviderManager] 重同步失败 (model_not_found): provider={provider_id} {e}")
            
        return True

    @staticmethod
    async def _verify_model(base_url: str, api_key: str, model: str) -> Dict:
        url = f"{base_url}/chat/completions"
        probe_message = "Quick check: in React, what does useEffect do? Reply 'ok' if you see this."
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": probe_message}],
                        "max_tokens": 1
                    }
                )
                if resp.is_success:
                    return {"ok": True, "status": resp.status_code}
                return {"ok": False, "status": resp.status_code, "errorText": resp.text[:200]}
        except Exception as e:
            return {"ok": False, "errorText": str(e)}

    @staticmethod
    async def _fetch_models_from_upstream(base_url: str, api_key: str) -> List[str]:
        url = f"{base_url}/models"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers={"Authorization": f"Bearer {api_key}"})
            if not resp.is_success:
                raise Exception(f"Upstream responded with {resp.status_code}")
            
            data = resp.json()
            if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
                return [m["id"] for m in data["data"] if "id" in m]
            return []

    @classmethod
    async def start_periodic_sync(cls):
        while True:
            # Re-read internal hours every loop
            try:
                from .config_service import ConfigService
                # Use a small wait to allow DB to be ready if needed, loops 60s check
                # Actually, wait 1 hour
                # We can check config service every minute? 
                # Simplest: Just use hardcoded or long interval.
                # Let's say we check sync every hour.
                 
                logger.info("[定时任务] 开始执行所有 Provider 的同步")
                providers = await cls.get_all()
                for p in providers:
                    asyncio.create_task(cls.background_sync_task(p))
                
                await asyncio.sleep(3600)
            except Exception as e:
                logger.error(f"Periodic sync loop error: {e}")
                await asyncio.sleep(60)

    @classmethod
    async def import_providers(cls, providers_data: List[Dict]):
        existing = await cls.get_all()
        seen = {f"{p['name'].lower()}::{p['baseUrl']}" for p in existing}
        
        imported = []
        skipped = []
        
        for raw in providers_data:
            if not raw.get("name") or not raw.get("baseUrl") or not raw.get("apiKey"):
                skipped.append({"name": raw.get("name", "未知"), "baseUrl": raw.get("baseUrl", "-"), "reason": "缺少必要字段"})
                continue
                
            base_url = raw["baseUrl"].rstrip("/")
            key = f"{raw['name'].lower()}::{base_url}"
            
            if key in seen:
                skipped.append({"name": raw["name"], "baseUrl": base_url, "reason": "已存在相同名称+地址"})
                continue
            
            created = await cls.add_provider(raw["name"], base_url, raw["apiKey"], raw.get("modelBlacklist", []))
            seen.add(key)
            imported.append({"id": created["id"], "name": created["name"]})
            
        return {
            "imported": imported,
            "skipped": skipped,
            "importedCount": len(imported),
            "skippedCount": len(skipped)
        }
