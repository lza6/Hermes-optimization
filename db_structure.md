# Hermes 数据库结构 (db_structure.md)

| 表名 | 描述 | 关键列 |
|:-----|:-----|:-----|
| `providers` | 供应商核心配置 | `id`, `name`, `baseUrl`, `apiKey`, `models` (JSON), `status`, `lastSyncedAt`, `lastUsedAt`, `createdAt`, `modelBlacklist` (JSON) |
| `sync_logs` | 模型同步历史记录 | `id`, `providerId`, `providerName`, `model`, `result`, `message`, `createdAt` |
| `request_logs` | API 请求访问日志 | `id`, `method`, `path`, `model`, `status`, `duration`, `ip`, `createdAt` |
| `hermes_keys` | 网关 API Key 管理 | `id`, `key_hash`, `description`, `createdAt`, `lastUsedAt` |
| `settings` | 全局持久化设置 (KV) | `key`, `value` |
| `metrics_counters` | 通用统计计数器 | `key`, `value` |
| `metrics_models` | 各模型调用频次统计 | `model`, `count` |
| `metrics_providers` | 各供应商调用/错误统计 | `id`, `name`, `count`, `errors` |

---

### 表定义详情

#### providers
- `id` (TEXT PRIMARY KEY): 供应商唯一标识
- `models`: 存储上游支持的模型列表（JSON 字符串）
- `modelBlacklist`: 禁用的模型列表（JSON 字符串）
- `status`: 当前状态 (active, pending, error)

#### hermes_keys
- `key_hash`: 密钥的 SHA256 哈希值，用于验证请求

#### request_logs
- `duration`: 请求响应时间（毫秒），供 RoutingScoreService 计算 EWMA
