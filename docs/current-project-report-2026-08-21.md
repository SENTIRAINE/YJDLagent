# YJDL LangGraph Agent 当前项目报告

> 报告版本：`2026-08-21`  
> Agent Tool Catalog：`2026-08-21.1`  
> SSE Schema：`1.1`  
> 适用范围：本仓库 Agent/RAG 服务，以及与 Spring Boot Tool 服务的接口边界

## 1. 执行摘要

YJDL Agent 已形成一套可运行的 LangGraph 编排服务：FastAPI 接收用户请求并创建异步 Run，LangGraph 在知识问答、地图查询、联合查询、澄清和会话响应之间路由；RAG 从本地 PDF 索引检索可追溯证据；地图业务通过 Spring Boot Tool Catalog 和只读 Tool 执行；结果通过可重放 SSE 返回并持久化。

截至本报告版本，已完成的核心能力包括：

- PDF 清洗、章节切分、混合检索和 citation 定位；
- 知识问题、地图问题、住宅道路联合搜索和连续对话路由；
- Catalog 驱动的 Tool 参数校验，当前严格要求版本 `2026-08-21.1`；
- MongoDB/SQLite Run Store、MongoDB/SQLite Checkpoint、租约 Worker 和 SSE 重放；
- 结构化 Conversation State、依赖串行、完成事务和配额记账；
- Tool/模型错误处理、Run 诊断和受控降级答案。

本轮针对 RAG 回归完成了专项修复：知识定义、含义、公式和计算方式问题现在会在会话业务条件继承前确定性进入 `RAG_QA`；每轮清理上一轮 Checkpoint 临时产物；模型 HTTP `429` 最多重试 3 次；答案模型持续失败时返回带文档、章节、页码和 citation 的检索降级结果。

当前结论是：代码层回归门禁通过，既有真实 RAG 链路已成功验证；但本次文档整理时 `/readyz` 返回 `503 TOOL_EXECUTION_FAILED`，说明当时 Agent 无法通过 Spring Tool 就绪检查。恢复 Spring Tool 连通性并重新通过 `/readyz` 之前，不应将当前进程视为可发布实例。

## 2. 系统边界

| 组件 | 主要职责 | 不负责 |
| --- | --- | --- |
| 前端 | 用户输入、SSE 消费、地图图层展示 | Agent 路由、空间业务计算 |
| Spring Boot | 用户入口、认证权限、业务规则、GeoScene 查询、指标与空间计算 | PDF RAG、LangGraph 编排 |
| YJDL Agent | 意图理解、对话状态、RAG、Tool 参数构造、结果映射、SSE | 下载图层后重算距离、百分位、便利度或推荐分 |
| 模型服务 | 结构化 Planner 和证据约束回答 | 作为业务事实来源 |
| MongoDB/SQLite | Run、Event、Conversation、Checkpoint 持久化 | 地图业务事实计算 |
| Redis | 多实例限流和配额协调 | Run/Event 事实账本 |

前端地图 Canvas 与发送按钮重叠问题不属于本次后端修复范围，当前报告不将其标记为已解决。

## 3. 总体架构

```text
用户 / 前端
    |
    v
Spring Boot 认证与业务入口 (127.0.0.1:8080)
    |
    | Bearer Token + Trace/Tenant/User Headers
    v
FastAPI Agent API (127.0.0.1:8000)
    |
    +--> Run/Event Store --> SSE 持久化与重放
    |
    +--> Leased Worker --> LangGraph
                            |
                            +--> RAG 混合检索 --> 本地 SQLite 索引
                            |
                            +--> Planner --> Spring Tool Catalog/Invoke
                            |
                            +--> Answer --> 模型服务或确定性降级
                            |
                            +--> Conversation State / Checkpoint
```

### 3.1 Agent 主图

主图的核心阶段为：

1. 标准化用户输入并确定意图；
2. 对需要知识证据的请求执行 RAG；
3. 加载并规范化当前 Tool Catalog；
4. 通过住宅专用 Planner 或通用地图 Planner 构造计划；
5. 校验并执行 Spring Tool；
6. 组装 `map.result`、citation 和回答；
7. 以单终态完成 Run，并原子提交会话状态与完成事件。

意图语义：

| 意图 | 触发与行为 |
| --- | --- |
| `RAG_QA` | 研究定义、公式、计算方法、指标解释；只使用知识库证据 |
| `MAP_QUERY` | 结构化地图筛选、住宅推荐或道路查询；调用 Spring Tool |
| `HYBRID` | 同时需要知识解释和地图结果；组合 RAG 与 Tool 证据 |
| `CLARIFY` | 条件冲突、字段不受支持或关键信息缺失；不猜测业务参数 |
| `CONVERSATION` | 问候、确认、总结等确定性会话；不调用 Tool/RAG |

### 3.2 RAG 数据流

```text
PDF
 -> 文本/表格/公式抽取
 -> 清洗与已知版式修复
 -> 章节切分 + 来源/页码/warning
 -> chunks.jsonl + manifest.json
 -> Dense + 中文 BM25 + 数字命中融合索引
 -> Top-K 证据 + citation
 -> 证据约束回答
```

检索默认权重为 Dense `0.55`、Lexical `0.35`、Number `0.10`，三者之和必须为 1。默认使用 768 维 hash embedding；切换 OpenAI-compatible Embedding Provider 或修改维度后必须重新构建索引。

知识库只能回答 PDF 已包含的口径、公式、统计和论文结论，不能据此生成 3712 个小区逐条数据或执行新的 GIS 计算。原文疑点详见 [知识库数据质量说明](./source-data-quality.md)。

### 3.3 地图与住宅联合查询

地图规划严格使用运行时 Catalog 中的 Tool、layer、字段和 JSON Schema。住宅与道路联合查询仅调用一次 `searchHousingCandidates`，不在 Agent 中拆成点线查询后自行关联。

字段边界：

- `归一化总分` 是住宅便利度；
- `新步行` 是住宅点字段；
- `WS归一化` 是道路 0-100 指标，`null` 表示不可用；
- `GVI`、`NOI` 是等级字段；
- `vegetation`/`绿视率原始值` 和 `noise`/`道路噪声原始值` 才是物理量阈值来源。

Tool 返回的住宅点、贡献道路和 buffer overlay 分别映射为 `HOUSING_CANDIDATES`、`CONTRIBUTING_ROADS` 和 `ROAD_BUFFER`。Agent 校验 `wkid=4326`、geometry 类型、图层顺序和结果上限，但不重新计算空间结果。

## 4. 运行时与持久化

### 4.1 Run 与 SSE

- `POST /api/v1/runs` 创建异步 Run，成功返回 HTTP `202`；
- `POST /api/v1/runs/stream` 创建 Run 并直接订阅 SSE；
- Event 先持久化再发送，ID 为 `{runId}:{sequence}`；
- `sequence` 严格递增，断线后使用 `afterSequence` 重放；
- 每个 Run 只能有一个 `completed`、`failed` 或 `cancelled` 终态；
- 默认 Run 超时 180 秒，Tool 超时 125 秒，SSE 心跳 15 秒，Event 保留 24 小时。

### 4.2 Conversation 与 Checkpoint

成功地图请求会提交结构化业务状态，包括实体、行政区、硬筛选、偏好、最后 Tool 参数和轻量结果引用。澄清、寒暄和总结不会覆盖最后一次成功业务条件。

快速连续请求通过 `depends_on_run_id` 串行，后续 Run 在前序终态提交后读取新的 state version。成功完成时，Memory、Conversation State 和 `run.completed` 在同一事务中提交，避免客户端已看到完成但下一轮仍读到旧状态。

每轮路由会清空检索结果、citation、Tool 计划/输出、地图结果和回答等执行产物，防止共享 Checkpoint 把上一轮证据带入当前问题。完整地图 feature 和 Tool 输出属于本轮临时状态，Checkpoint 只保留轻量摘要。

### 4.3 部署模式

| 模式 | Store | Checkpoint | Worker | 适用场景 |
| --- | --- | --- | --- | --- |
| 单机开发 | SQLite | SQLite 独立文件 | 内置 | 本地调试、单进程测试 |
| 单实例生产基线 | MongoDB 事务 | MongoDB | 内置或独立 | 小流量受控运行 |
| 多实例生产 | MongoDB 事务 | MongoDB | 独立租约 Worker | 配合 Redis 分布式配额和限流 |

MongoDB 模式要求副本集或分片集群支持事务。SQLite 的 Run Store 和 Checkpoint 不得共用同一数据库文件。

## 5. API 与契约基线

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/healthz` | 进程存活检查 |
| `GET` | `/readyz` | Catalog、Tool health、数据快照和策略就绪检查 |
| `POST` | `/api/v1/runs` | 创建异步 Agent Run |
| `POST` | `/api/v1/runs/stream` | 创建并订阅 SSE |
| `GET` | `/api/v1/runs/{runId}` | 查询 Run |
| `GET` | `/api/v1/runs/{runId}/events` | 重放并继续订阅 Event |
| `GET` | `/api/v1/runs/{runId}/diagnostics` | 内部诊断 |
| `POST` | `/api/v1/runs/{runId}/cancel` | 幂等取消 |
| `POST` | `/api/v1/rag/search` | 直接 RAG 检索 |

所有 `/api/v1` 调用需要服务 Bearer Token，以及 `X-Trace-Id`、`X-Tenant-Id`、`X-User-Id`。请求 Header 中的租户/用户身份必须与 body 一致。

机器契约以 [OpenAPI v1.1](./docs/agent-api-v1.openapi.yaml) 为准；人工说明见 [LangGraph API v1.1](./docs/langgraph-api-v1.1.md)；Tool 参数始终以运行时 Catalog 为准。

## 6. 2026-08-21 RAG 回归修复

### 6.1 原因

连续对话中，知识解释问题可能先继承上一轮住宅行政区、价格和偏好，再进入通用模型路由。这会把本应纯 RAG 的问题污染成地图或联合查询。共享 Conversation Checkpoint 还可能保留上一轮检索、Tool 和地图执行产物；当模型服务限流时，最终回答又缺少足够的检索定位信息。

### 6.2 已落地行为

1. 知识定义、含义、公式和计算方法问题在业务状态继承前确定性进入 `RAG_QA`；
2. 包含明确筛选、查找、显示、定位、推荐等地图动作的问题仍保留地图语义；
3. 知识问题不继承上一轮住宅行政区、价格或偏好；
4. 每个新 turn 清除旧执行产物；
5. 模型 HTTP `429` 尊重有限的 `Retry-After`，最多尝试 3 次；
6. 答案生成持续失败时，根据 retrieval results 生成文档、章节、页码和 citation 列表，并附加 `ANSWER_GENERATION_DEGRADED` warning。

## 7. 验证记录

### 7.1 本次文档更新复验

执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_agent_runtime.py tests\test_llm.py -q
```

结果：`81 passed`。另有 FastAPI/httpx、LangGraph serializer 的依赖弃用提示，以及当前受限工作区无法写 `.pytest_cache` 的 warning；不影响测试通过结论。

本次就绪检查：

```text
GET http://127.0.0.1:8000/readyz
HTTP 503
{"status":"NOT_READY","reason":"TOOL_EXECUTION_FAILED"}
```

这表示 Agent 进程可响应，但当时 Spring Tool health/Catalog 链路未满足就绪条件。应检查 `SPRING_BOOT_BASE_URL`、服务 Token、Spring `/internal/agent-tools/health` 与 Catalog 版本后复验。

### 7.2 已完成的真实 RAG 链路证据

在修复后的真实连续请求中：

| 场景 | Run ID | 结果 |
| --- | --- | --- |
| 前序住宅查询 | `3353ae1a-028d-428f-ad7e-beb142a8350c` | 成功建立业务会话状态 |
| 后续知识问题 | `0a3e2d5f-2cc6-46e3-825f-607e6e08a779` | `RAG_QA`，5 条检索结果，0 次 Tool，2 条 citation，`SUCCEEDED` |

当时 `/readyz` 数据快照为 1 个文档、46 个 chunks。该证据说明修复后的路由和 RAG 主链路曾成功运行，但不能替代当前环境重新通过 readiness。

## 8. 已知限制与风险

| 优先级 | 项目 | 当前影响 | 建议 |
| --- | --- | --- | --- |
| P0 | 当前 `/readyz` 为 `TOOL_EXECUTION_FAILED` | 当前实例不能视为发布就绪 | 恢复 Spring Tool 链路并重跑 readiness/live tests |
| P1 | `workflow.py`、`store.py` 职责仍较集中 | 修改路由或存储时回归面较大 | 渐进拆出规则、结果、节点与 Repository Protocol |
| P1 | 多实例未配置 Redis 时配额仍是进程内 | 租户限流可被跨副本绕过 | 多实例部署强制要求 `AGENT_REDIS_URL` |
| P1 | Checkpoint 缺少完整会话级保留策略 | 长期运行存在容量增长 | 增加压缩、TTL 或最近 N 个稳定点策略 |
| P1 | 真实 A01-A11 fixture 尚需随当前 Catalog 固化 | 发布证据不完整 | 保存请求、Tool 响应、SSE 和诊断指标 |
| P1 | 尚缺正式多实例容量基线 | P95、队列和恢复能力未知 | 固定拓扑压测并记录延迟、SSE 字节和错误分布 |
| P2 | 复杂 ordinal reference 支持有限 | “第二个结果”等可能需要澄清 | 保存候选 ID 与用户选择指针 |
| P2 | 依赖存在弃用 warning | 后续升级可能有兼容成本 | 建立依赖升级和序列化配置任务 |

## 9. 发布检查清单

- [ ] `/healthz` 返回成功；
- [ ] `/readyz` 返回 `READY`，Catalog 为 `2026-08-21.1`；
- [ ] Agent API 与 Worker 使用预期 Store/Checkpoint backend；
- [ ] MongoDB 事务与备份恢复演练通过；
- [ ] 多实例时 Redis 配额和限流已启用；
- [ ] 默认单元/契约测试通过；
- [ ] RAG 连续对话回归通过，知识轮为 0 Tool；
- [ ] Spring A01-A11 真实场景和 SSE 重放/取消通过；
- [ ] 记录 Tool、编排、SSE P50/P95、错误码与重试分布；
- [ ] 前端分别验证住宅、道路、buffer 图层展示和清除行为。

## 10. 文档维护规则

本文件是 `docs` 根目录唯一的当前版本报告。架构审查、整改计划、测试结论和版本门禁更新应合并到本文件，不再新增按日期分散的报告。

以下内容保持独立维护：

- OpenAPI 和 Tool Catalog：机器契约；
- LangGraph API 与 Agent 工程交接：开发和联调说明；
- `examples/`：可执行或可比较的契约 fixture；
- `source-data-quality.md`：知识库来源事实和疑点；
- `runbooks/`：生产运维步骤。
