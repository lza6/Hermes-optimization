import os

class Config:
    PORT = int(os.getenv("PORT", 8000))
    HERMES_SECRET = os.getenv("HERMES_SECRET", "hermes-secret-key")
    DB_PATH = os.getenv("DB_PATH", "hermes.db")
    ENV = os.getenv("ENV", "production")
    
    # v4.0.0 版本
    VERSION = "4.0.0"
    
    # 缓存配置
    CACHE_TTL_PROVIDERS = int(os.getenv("CACHE_TTL_PROVIDERS", 30))  # 供应商缓存 30 秒
    CACHE_TTL_MODELS = int(os.getenv("CACHE_TTL_MODELS", 60))        # 模型列表缓存 60 秒
    CACHE_MAX_SIZE = int(os.getenv("CACHE_MAX_SIZE", 100))           # 最大缓存条目数
    
    # 断路器配置
    CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("CIRCUIT_FAILURE_THRESHOLD", 5))
    CIRCUIT_RECOVERY_TIMEOUT = int(os.getenv("CIRCUIT_RECOVERY_TIMEOUT", 30))
    
    # 日志批量配置
    LOG_BATCH_SIZE = int(os.getenv("LOG_BATCH_SIZE", 50))
    LOG_FLUSH_INTERVAL = int(os.getenv("LOG_FLUSH_INTERVAL", 5))

config = Config()

