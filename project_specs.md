# Hermes AI Gateway - 项目规格文档

> **版本**: v4.0.0 (当前) | **更新时间**: 2026-01-30
> **项目定位**: AI API 网关与聚合器，统一管理多个 OpenAI 兼容服务提供商

---

## 1. 项目概述

Hermes 是一个高性能、轻量级的 AI API 网关，提供：
- **统一 OpenAI 兼容接口** - 标准 `/v1/chat/completions` 和 `/v1/models` API
- **多供应商聚合** - 支持无限上游供应商
- **智能路由与负载均衡** - EWMA 评分算法 + 自动故障转移
- **可视化管理后台** - Dashboard, Playground, 日志监控

---

## 2. 技术栈

| 层级 | 技术 | 版本 |
|:-----|:-----|:-----|
| Web 框架 | FastAPI | 0.115.0 |
| ASGI 服务器 | Uvicorn | 0.30.0 |
| HTTP 客户端 | httpx (HTTP/2) | 0.27.0 |
| 数据验证 | Pydantic | 2.8.0 |
| 数据库 | SQLite (aiosqlite) | 0.20.0 |
| 模板引擎 | Jinja2 | 3.1.4 |
| 日志 | Loguru | 0.7.2 |
| 限流 | slowapi + 自定义滑动窗口 | 0.1.9 |
| 前端 | TailwindCSS (CDN) | - |

---

## 3. 项目结构

```
hermes-optimization/
├── hermes/
│   ├── main.py                 # FastAPI 应用入口
│   ├── config.py               # 配置管理
│   ├── database.py             # SQLite 连接池 + 表定义
│   ├── controllers/
│   │   ├── chat.py             # /v1/models, /v1/chat/completions
│   │   └── admin.py            # /admin/* 管理 API
│   ├── models/
│   │   └── schemas.py          # Pydantic 数据模型
│   ├── services/
│   │   ├── provider_manager.py # 供应商管理 + 模型同步
│   │   ├── dispatcher_service.py # 智能路由调度
│   │   ├── proxy_service.py    # HTTP/2 代理转发
│   │   ├── routing_score_service.py # EWMA 评分算法
│   │   ├── auth_service.py     # API Key 认证
│   │   ├── config_service.py   # 配置读写
│   │   ├── log_service.py      # 日志 + 指标统计
│   │   └── rate_limiter.py     # 滑动窗口限流器
│   ├── templates/              # Jinja2 HTML 模板
│   │   ├── base.html           # 布局 + 暗黑模式
│   │   ├── dashboard.html      # 供应商管理
│   │   ├── chat.html           # 聊天 Playground
│   │   ├── logs.html           # 日志查看
│   │   ├── metrics.html        # 监控指标
│   │   └── settings.html       # 系统设置
│   └── utils/
│       ├── logger.py           # Loguru 配置
│       └── model_normalizer.py # 模型名称规范化
├── public/                     # 静态资源
│   ├── logo.png
│   └── Hermes.png
├── requirements.txt            # Python 依赖
├── start.bat                   # Windows 一键启动
└── hermes.db                   # SQLite 数据库
```

---

## 4. 核心功能模块

### 4.1 供应商管理 (ProviderManagerService)
- CRUD 操作 + 批量导入/导出
- 后台异步模型同步 (5秒 RPM 保护)
- 模型黑名单过滤
- 定时周期同步 (每小时)

### 4.2 智能调度 (DispatcherService)
- 冷却惩罚机制 (指数退避, 最大 4 小时)
- 自愈探测 (冷却期满后自动验证)
- 基于 RoutingScoreService 的评分选择
- 惩罚计数触发自动重同步

### 4.3 路由评分 (RoutingScoreService)
- EWMA 成功率 (α=0.2)
- EWMA 延迟
- 24 小时半衰期时间衰减
- 多因子加权: 成功率 50% + 延迟 30% + 新鲜度 20%

### 4.4 代理转发 (ProxyService)
- HTTP/2 支持 + 连接池复用
- 流式/非流式响应
- 错误检测: model_not_found, quota_exhausted
- 自动惩罚 + 重试

### 4.5 限流 (SlidingWindowLimiter)
- 滑动窗口算法 (12 槽 × 5 秒)
- 默认 60 RPM / IP
- 详细状态响应头

---

## 5. 数据库表结构

| 表名 | 用途 |
|:-----|:-----|
| `providers` | 供应商配置 (id, name, baseUrl, apiKey, models, status...) |
| `sync_logs` | 模型同步日志 |
| `request_logs` | 请求日志 |
| `hermes_keys` | API Key 管理 (SHA256 哈希) |
| `settings` | 键值配置 |
| `metrics_counters` | 计数器指标 |
| `metrics_models` | 模型使用统计 |
| `metrics_providers` | 供应商使用统计 |

---

## 6. API 端点

### 公开 API
- `GET /v1/models` - 获取可用模型列表
- `POST /v1/chat/completions` - 聊天完成 (流式/非流式)
- `GET /health` - 健康检查

### 管理 API (/admin)
- 供应商 CRUD: `GET/POST/PATCH/DELETE /admin/providers`
- 日志查询: `GET /admin/request-logs`, `GET /admin/sync-logs`
- 指标: `GET /admin/metrics`
- Key 管理: `GET/POST /admin/keys`
- 设置: 周期同步间隔, 重试次数, 调度参数

---

## 7. 当前版本特性 (v4.0.0)

✅ v4.0.0 新增:
- 断路器模式 (Circuit Breaker) - 供应商级别熔断保护
- 断路器管理 API - /admin/circuit-breaker/*
- 缓存管理 API - /admin/cache/*
- 增强版健康检查 - 包含断路器/供应商/延迟状态
- 路由决策响应头 - X-Hermes-Provider, X-Hermes-Score
- 模型列表 API 缓存 - 减少重复计算

✅ v3.0.0 (v4.0.0已包含):
- 内存缓存层 (LRU 缓存供应商/模型数据)
- 日志批量写入 (减少数据库 I/O)
- 请求追踪 ID (Trace ID)

✅ v2.0.0 (基础):
- SQLite 连接池 + WAL 模式
- HTTP/2 客户端池
- EWMA 路由评分
- 滑动窗口限流
- 暗黑模式 UI
- 延迟百分位统计 (P50/P90/P99)
- 模型名称规范化

---

## 8. 待优化项 (v5.0.0 升级目标)

### 性能优化
- [ ] 请求队列削峰
- [ ] 路由评分缓存优化

### 功能扩展
- [ ] WebSocket 实时推送状态
- [ ] 多租户支持
- [ ] 更丰富的统计图表

### 代码质量
- [ ] 单元测试覆盖
- [ ] 配置热更新

---

## 9. 配置参数

| 参数 | 默认值 | 说明 |
|:-----|:-----|:-----|
| `PORT` | 8000 | 服务监听端口 |
| `HERMES_SECRET` | hermes-secret-key | 后门密钥 |
| `DB_PATH` | hermes.db | 数据库路径 |
| `RATE_LIMIT_MAX` | 60 | 限流阈值 (RPM) |
| `RATE_LIMIT_WINDOW` | 60 | 限流窗口 (秒) |
| `dispatcher_initial_penalty_ms` | 30分钟 | 初始惩罚时长 |
| `dispatcher_max_penalty_ms` | 4小时 | 最大惩罚时长 |
| `dispatcher_resync_threshold` | 3 | 触发重同步阈值 |
| `periodicSyncIntervalHours` | 1 | 周期同步间隔 |
| `chatMaxRetries` | 3 | 聊天请求最大重试 |

---

*文档维护: 每次版本更新后同步更新此文档*
