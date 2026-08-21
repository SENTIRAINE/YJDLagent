# LangGraph API v1.1 接口说明

本文是 LangGraph v1.1 接口的人工阅读指南。字段类型、必填项和枚举值以
[agent-api-v1.openapi.yaml](./agent-api-v1.openapi.yaml) 为唯一机器契约；Tool 参数以运行时
`GET /internal/agent-tools/catalog` 返回的 Catalog 为准。本文不复制完整 JSON Schema，避免出现两份契约。

当前实现状态、验证证据和已知限制见 [当前项目报告](../current-project-report-2026-08-21.md)。

## 1. 版本与职责边界

- SSE `schemaVersion` 固定为 `1.1`。
- Agent Tool Catalog 版本必须严格等于 `2026-08-21.1`。
- LangGraph 负责理解自然语言、生成合约参数、调用 Tool 和组装 SSE。
- Spring Boot Tool 负责便利度、道路 WS、百分位、距离、缓冲区和推荐分数计算。
- LangGraph 不下载图层重算空间结果，也不人为维护便利度或步行指数的绝对分数分级。

用户说“高一点”时，LangGraph 使用 `PREFER_HIGH`；用户明确说“必须高”或“很高”时，使用
`HIGH` 或 `VERY_HIGH`。对应阈值和 P75/P90 由后端版本化策略解析并在 Tool 响应中返回。

常见的价格上限、便利度、道路步行、行政区、缓冲距离和返回数量由确定性规则构造，不依赖 LLM 猜测。
只有显式权重或 GVI/NOI/WS 数值条件进入结构化 LLM Planner，输出仍会经过相同的确定性校验。

## 2. LangGraph 服务接口

默认开发地址：`http://127.0.0.1:8000`。

所有 `/api/v1` 请求使用以下请求头：

| 请求头 | 必填 | 说明 |
| --- | --- | --- |
| `Authorization: Bearer <token>` | 是 | Spring Boot 调用 LangGraph 的服务凭证 |
| `X-Trace-Id` | 是 | 跨服务追踪 ID |
| `X-Tenant-Id` | 是 | 租户 ID |
| `X-User-Id` | 是 | 用户 ID，必须与请求体 `user.userId` 一致 |

### 2.1 创建异步 Run

`POST /api/v1/runs`

成功返回 HTTP `202` 和当前 Run 状态。相同租户、用户和 `messageId` 的相同请求会附着到原 Run；
相同 `messageId` 但请求内容不同会返回 `MESSAGE_CONFLICT`。

### 2.2 创建并订阅 Run

`POST /api/v1/runs/stream`

成功返回 HTTP `200`、`Content-Type: text/event-stream` 和 `X-Run-Id`。请求体示例：

```json
{
  "conversationId": "11111111-1111-4111-8111-111111111111",
  "messageId": "22222222-2222-4222-8222-222222222222",
  "query": "帮我挑一套房价12000以内，便利度和周边道路步行指数高一点的房子",
  "context": {
    "locale": "zh-CN",
    "map": {
      "visibleLayerIds": [0, 1, 2, 3, 4, 5],
      "zoom": 13,
      "extent": null
    },
    "businessObjectIds": []
  },
  "user": {
    "userId": "user-1",
    "tenantId": "tenant-1",
    "roles": ["USER"]
  }
}
```

### 2.3 查询、重放和取消

| 方法与路径 | 用途 |
| --- | --- |
| `GET /api/v1/runs/{runId}` | 获取 Run 当前状态 |
| `GET /api/v1/runs/{runId}/events?afterSequence=N` | 只重放 `sequence > N` 的事件并继续订阅 |
| `POST /api/v1/runs/{runId}/cancel` | 幂等取消 Run，请求体为 `{"reason":"user_requested"}` |

事件历史过期时重放返回 HTTP `410`。终态 Run 再次取消不会产生第二个终态。

### 2.4 Run 诊断指标

`GET /api/v1/runs/{runId}/diagnostics`

该接口只允许携带内部服务身份调用，不向浏览器公开。响应包含实际 Tool 参数及哈希、Tool/编排耗时、重试与错误码，以及每次 SSE 连接的字节数、耗时和序列范围。发布验收使用该接口比较 Tool 参数；Tool 参数不会加入 SSE，避免扩大浏览器事件契约。

### 2.5 就绪与运行时策略

`GET /readyz` 每次请求都会重新读取 Tool Catalog 和 Tool health。只有顶层 health 与 `housingSnapshot.status` 均为 `READY`，且 Catalog 版本一致时才返回 `200`；`WARMING`、`DEGRADED`、`STALE`、缺失 snapshot 或版本不一致均返回 `503`。响应中的 `runtimePolicy` 是当前进程实际使用的 Run/Tool 超时，发布验收会校验 Tool 超时大于 120 秒且小于 Run 超时。

常见 readiness 原因：

| reason | 含义与检查方向 |
| --- | --- |
| `TOOL_EXECUTION_FAILED` | Spring Tool health/Catalog 请求失败；检查 Base URL、服务 Token、Spring 状态和网络 |
| `TOOL_CATALOG_VERSION_MISMATCH` | Catalog 版本不是 `2026-08-21.1`；两端必须同步升级 |
| `DATA_NOT_READY` | housing snapshot 未达到 `READY`；等待或修复 Spring 数据装载 |

## 3. SSE 约束

每个事件包含 `id`、`event` 和单行 JSON `data`。`data` 必须满足：

- `schemaVersion="1.1"`；
- `id` 等于 `{runId}:{sequence}`；
- `sequence` 在 Run 内严格递增；
- 每个 Run 只有一个终态：`run.completed`、`run.failed` 或 `run.cancelled`；
- `tool.completed` 必须包含 `durationMs`；
- `answer.delta` 拼接结果必须等于 `run.completed.payload.answer`。

典型联合购房查询事件顺序：

```text
run.started
route.selected
tool.started
tool.completed
map.result
answer.delta
run.completed
```

完整示例见 [agent-sse-housing-buffer.txt](./examples/agent-sse-housing-buffer.txt)。

### 3.1 RAG 路由与降级语义

- 定义、含义、公式、口径和计算方法类问题在会话业务状态继承前确定性进入 `RAG_QA`。
- 纯知识问题不会继承上一轮住宅行政区、价格、偏好或地图结果。
- 每个新 turn 清空上一轮检索、Tool、地图和回答等执行产物，避免 Checkpoint 交叉污染。
- 包含筛选、查找、显示、定位、推荐等明确地图动作时，不应用纯知识路由覆盖业务意图。
- 模型服务返回 HTTP `429` 时最多尝试 3 次；检索完成但答案生成持续失败时，终态仍返回文档、章节、页码和 citation，并包含 `ANSWER_GENERATION_DEGRADED` warning。

直接检索接口为 `POST /api/v1/rag/search`。该接口适合检索诊断，不替代完整 Run 的路由、回答、配额与事件语义。

## 4. Spring Boot Agent Tool 接口

LangGraph 使用服务凭证调用以下内部接口：

| 方法与路径 | 用途 |
| --- | --- |
| `GET /internal/agent-tools/catalog` | 获取唯一有效的 Tool Schema 和版本 |
| `POST /internal/agent-tools/tools/{toolName}/invoke` | 执行只读 Tool |
| `GET /internal/agent-tools/executions/{toolCallId}` | 超时后查询同一逻辑调用状态 |
| `GET /internal/agent-tools/health` | 查询 Tool 服务健康状态 |

Tool 调用额外携带 `X-Run-Id`。每个逻辑调用生成一个 UUID `toolCallId`；超时恢复必须复用完全相同的
`toolCallId` 和参数，不得生成新 ID 重算。

## 5. 模糊购房搜索

住宅推荐或住宅与道路联合查询只调用 `searchHousingCandidates`。下面是用户示例对应的参数形态；
实际请求在发送前必须通过当前 Catalog 的 `inputSchema` 校验。

```json
{
  "mode": "RANK",
  "districts": [],
  "hardFilters": {
    "priceMax": 12000
  },
  "preferences": {
    "price": {
      "enabled": false,
      "level": "PREFER_LOW",
      "weight": 0
    },
    "convenience": {
      "enabled": true,
      "level": "PREFER_HIGH"
    },
    "roadWalkability": {
      "enabled": true,
      "level": "PREFER_HIGH"
    }
  },
  "roadCriteria": {},
  "spatial": {
    "relation": "WITHIN_ROAD_BUFFER"
  },
  "display": {
    "includeRoads": true,
    "includeBuffers": true
  },
  "limit": 20
}
```

关键映射：

| 用户表达 | LangGraph 参数 |
| --- | --- |
| 房价 12000 以内 | `hardFilters.priceMax=12000` |
| 价格尽量低 | `preferences.price.level=PREFER_LOW`，不生成 `priceMax` |
| 便利度高一点 | `preferences.convenience.level=PREFER_HIGH` |
| 道路步行指数高一点 | `preferences.roadWalkability.level=PREFER_HIGH` |
| 高 WS 道路附近 | `mode=BUFFER_FILTER`、道路偏好 `HIGH` |
| 很高 WS 道路附近 | `mode=BUFFER_FILTER`、道路偏好 `VERY_HIGH` |
| 未指定行政区 | `districts=[]` |
| 未指定距离 | 省略 `spatial.bufferMeters`，由后端应用默认 100 米 |
| 未指定返回数量 | `limit=20`；明确“推荐 5 套”时使用 5，合法范围 1–50 |

`新步行`、住宅 `归一化总分` 和道路 `WS` 不能互相替代。显式缓冲距离必须在 20–2000 米；
超范围、冲突硬条件和未知指标必须拒绝或澄清，不能截断或换成相近字段。

## 6. map.result 组装

- `housingCandidates` 按 `layerId` 组成 `HOUSING_CANDIDATES` point resultSet。
- `roadFeatures` 按 `layerId` 组成 `CONTRIBUTING_ROADS` polyline resultSet。
- `bufferOverlays` 原样放入 `payload.overlays`。
- 图层顺序固定为 `ROAD_BUFFER, CONTRIBUTING_ROADS, HOUSING_CANDIDATES`。
- 所有几何必须是 `wkid=4326`；联合查询地图 feature 总数不得超过 200。
- 空住宅结果仍保留道路、buffer 和 `NO_HOUSING_IN_BUFFER`。

## 7. 错误与重试

以下错误不重试：

- `INVALID_BUFFER_DISTANCE`
- `INVALID_HOUSING_SEARCH_ARGUMENT`
- `TOOL_CALL_CONFLICT`

`METRIC_STATISTICS_UNAVAILABLE` 和 `DATA_VERSION_MISMATCH` 可以使用同一逻辑调用稍后重试，但不得降级为
无百分位或无空间约束查询。Catalog 版本不是 `2026-08-21.1` 时，LangGraph readiness 返回
`TOOL_CATALOG_VERSION_MISMATCH`，并阻止使用不匹配的 Schema。

## 8. 验证命令

本地合约测试：

```powershell
.venv\Scripts\python.exe -m pytest -q
```

真实 Spring Tool 测试需要启动 Spring Boot，并设置：

```powershell
$env:RUN_SPRING_E2E='1'
$env:AGENT_TOOL_SERVICE_TOKEN='<service-token>'
.venv\Scripts\python.exe -m pytest tests\test_live_spring.py -q
```

发布前仍需按 [agent-engineer-v1.1-handoff.md](./agent-engineer-v1.1-handoff.md) 固化真实 A01–A11 的请求、
Tool 响应和 SSE fixture，并输出 P50/P95 与错误分布。
