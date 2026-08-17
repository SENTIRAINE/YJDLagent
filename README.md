# YJDL LangGraph Agent

面向大连市社区生活圈场景的 LangGraph Agent 编排服务。项目将本地 PDF 知识库、中文混合检索、自然语言意图路由、Spring Boot 地图 Tool 与生产级 Run 运行时组合在一起，为上层应用提供可追踪、可恢复的知识问答和地图分析能力。

当前知识库来源为《步行指数知识库——大连市社区生活圈案例》。Spring Boot 负责外部用户入口、认证、权限、业务规则和 GeoScene Tool；本项目负责 Agent 理解与编排、RAG 检索、Tool 参数构造、地图结果映射和 SSE 事件输出。

## 功能概览

### 1. PDF 知识库构建与问答

- 从 PDF 中抽取正文、表格和公式，清理页眉、页码、异常字符并修复已知跨行公式布局。
- 按章节层级切分文本，保留文档 ID、章节路径、页码、内容类型和来源文件。
- 对原文疑点、口径不一致和公式修复过程保留 `warning` 标记，不静默修改来源。
- 生成可审计的 `chunks.jsonl`、索引清单 `manifest.json` 和本地 SQLite 检索索引。
- 使用向量相似度、中文 BM25、数字命中三路融合，并对“公式”“原文疑点”等查询增加意图权重。
- 支持按文档 ID、内容类型过滤，返回可定位到章节和页码的 citation；对外 citation 默认不暴露原文摘录。

### 2. 多意图 LangGraph Agent

| 意图 | 具体能力 |
| --- | --- |
| `RAG_QA` | 使用知识库证据回答研究口径、公式、指标和论文结论问题 |
| `MAP_QUERY` | 调用结构化地图 Tool 查询住宅点、道路线等业务数据 |
| `HYBRID` | 同时使用知识库证据和地图 Tool，处理“指标解释 + 地图筛选”问题 |
| `CLARIFY` | 条件缺失、字段冲突、超出 Tool 能力或表达不确定时主动澄清 |
| `CONVERSATION` | 处理问候、确认和上下文延续等会话型请求 |

主图包含意图路由、知识检索、Catalog 加载、住宅搜索规划、通用地图规划、Tool 执行和答案生成等节点。跟进问题可以继承同一会话的结构化业务状态，但历史消息不能覆盖本轮明确条件或当前 Tool Catalog。

### 3. 地图查询与住宅道路联合分析

- 启动和就绪检查时读取 Spring Boot Agent Tool Catalog，并严格校验版本 `2026-07-29.1`。
- 使用确定性规则解析房价上限、行政区、便利度偏好、道路步行偏好、缓冲距离和返回数量，减少模型猜测。
- 住宅与道路联合查询只调用一次 `searchHousingCandidates`，避免在 Agent 中拆分调用或产生 N+1 查询。
- 将住宅点、贡献道路和道路缓冲区组装成 `map.result`，分别输出 point/polyline resultSet，并原样传递后端 buffer overlay。
- 不在 Agent 中重算便利度、道路 WS、百分位、距离或推荐分，这些业务结果由 Spring Boot/GeoScene 层提供。
- 校验几何类型、`wkid=4326`、图层顺序、结果数量和空结果语义；没有住宅时仍可保留道路和缓冲区。

### 4. 异步 Run 与流式事件

- `POST /api/v1/runs` 支持异步提交，`POST /api/v1/runs/stream` 直接返回具名 SSE。
- Run/Event 先持久化再发送，事件 ID 固定为 `{runId}:{sequence}`，断线后可按序号重放。
- 支持状态查询、诊断、取消、单终态约束、消息幂等和相同 `messageId` 的内容冲突检测。
- SSE 固定使用 `schemaVersion=1.1`，典型事件包括 `run.started`、`route.selected`、`tool.started`、`tool.completed`、`map.result`、`answer.delta` 和终态事件。
- 默认单次 Run 最长 180 秒，Tool HTTP 超时 125 秒，SSE 每 15 秒心跳，事件历史保留 24 小时。

### 5. 持久化、Worker 与配额治理

- 生产默认使用 MongoDB 持久化 Run、Event、Conversation、State 和 Checkpoint，并要求副本集或分片集群支持事务。
- 本地开发可使用 SQLite Agent Store；Run/Event 与 Checkpoint 应配置为不同数据库文件。
- Worker 使用租约和 lease generation 领取任务，支持 API 与 Worker 分进程部署。
- 支持 SQLite 写入重试、CAS 会话状态更新、Run 取消、用户/租户限流、活跃 Run 限制、排队限制和 Token 配额。
- 多实例部署可使用 Redis 进行分布式限流与配额控制；MongoDB 仍是业务状态和事件账本的事实来源。

### 6. 安全与可观测性

- 所有 `/api/v1` 接口要求 Bearer Token，以及 `X-Trace-Id`、`X-Tenant-Id`、`X-User-Id`。
- 校验 Header 身份与请求体 `user` 身份一致，缺失或不一致时拒绝请求。
- Tool 调用使用独立服务 Token；模型密钥、数据库路径和底层异常不会写入错误响应。
- Run 诊断接口记录 Tool 参数及哈希、阶段耗时、重试、错误码和 SSE 连接字节数，便于联调与验收。

## 架构与数据流

```text
PDF 文档
   │  pdfplumber / pypdf
   ▼
清洗、表格/公式修复、章节切分、来源标记
   │
   ├── chunks.jsonl + manifest.json
   └── SQLite 向量/词法索引
           │
用户请求 ──► FastAPI 鉴权与 Run Runtime
                    │
                    ▼
             LangGraph 意图路由
              ┌─────┼──────────┐
              │     │          │
           RAG_QA MAP_QUERY  HYBRID
              │     │          │
       混合检索  Spring Tool Catalog/Invoke
              └─────┴──────┬───┘
                           ▼
              证据、地图结果、答案与 SSE
                           │
                 MongoDB / SQLite / Redis
```

## 技术栈

- Python 3.11+
- FastAPI、Uvicorn、LangGraph
- pdfplumber、pypdf、NumPy
- SQLite、MongoDB、Redis
- Pydantic、JSON Schema、httpx
- pytest、mongomock

## 快速开始

### 1. 安装依赖

推荐 Python 3.11 或 3.12：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

复制 `.env.example` 为 `.env`，并填写模型与内部服务凭据。核心配置示例：

```powershell
$env:OPENAI_API_KEY="..."
$env:OPENAI_MODEL="gpt-5.4"
$env:OPENAI_CHAT_COMPLETIONS_URL="https://kuaipao.pro/v1/chat/completions"
$env:LANGGRAPH_SERVICE_TOKEN="..."
$env:AGENT_TOOL_SERVICE_TOKEN="..."
$env:SPRING_BOOT_BASE_URL="http://127.0.0.1:8080"
```

缺少 `LANGGRAPH_SERVICE_TOKEN` 或 `AGENT_TOOL_SERVICE_TOKEN` 时服务会失败关闭，不会降级为无认证调用。

### 2. 构建知识库

离线基线使用内置 hash embedding，不依赖外部模型：

```powershell
python -m app.cli.build_index
```

也可以显式指定 PDF：

```powershell
python -m app.cli.build_index .\步行指数知识库.pdf
```

构建结果：

- `data/processed/chunks.jsonl`：清洗、切分与来源信息。
- `data/processed/manifest.json`：文档哈希、版本和块统计。
- `data/index/rag.sqlite3`：本地检索索引。

生产环境可切换到 OpenAI-compatible Embedding 服务：

```powershell
$env:RAG_EMBEDDING_PROVIDER="openai-compatible"
$env:RAG_EMBEDDING_MODEL="BAAI/bge-m3"
$env:RAG_EMBEDDING_DIMENSION="1024"
$env:RAG_EMBEDDING_BASE_URL="http://embedding-service:7997/v1"
python -m app.cli.build_index
```

更换 Embedding Provider 或维度后必须重新构建索引。

### 3. 查询验证

```powershell
python -m app.cli.query "生活圈平均道路交叉口密度是多少？" --top-k 5
python -m app.cli.query "步行指数如何计算？" --context
```

### 4. 启动服务

Windows 推荐使用根目录脚本：

```bat
start.bat
```

默认监听 `0.0.0.0:8000`，也可以指定端口或拆分 API/Worker：

```bat
start.bat 8001
start.bat api 8001
start.bat worker
```

`start.bat api` 关闭内置 Worker，适合与独立 `start.bat worker` 配合。也可以直接启动：

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 5. 运行测试

```powershell
pytest
```

普通测试使用 Fake LLM/Tool，不消耗模型额度。真实模型与 Spring Tool 联调默认跳过，按需启用：

```powershell
$env:RUN_LIVE_LLM="1"
pytest tests/test_live_llm.py -q -s

$env:RUN_SPRING_E2E="1"
$env:AGENT_TOOL_SERVICE_TOKEN="..."
pytest tests/test_live_spring.py -q -s
```

## API 入口

默认开发地址：`http://127.0.0.1:8000`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 进程健康检查 |
| `GET` | `/readyz` | Tool Catalog、数据快照和运行时就绪检查 |
| `POST` | `/api/v1/runs` | 异步创建 Agent Run |
| `POST` | `/api/v1/runs/stream` | 创建并订阅 SSE Run |
| `GET` | `/api/v1/runs/{runId}` | 查询 Run 状态 |
| `GET` | `/api/v1/runs/{runId}/events?afterSequence=N` | 重放并继续订阅事件 |
| `GET` | `/api/v1/runs/{runId}/diagnostics` | 查询内部诊断指标 |
| `POST` | `/api/v1/runs/{runId}/cancel` | 幂等取消 Run |
| `POST` | `/api/v1/rag/search` | 直接调用 RAG 检索 |
| `GET` | `/docs` | FastAPI OpenAPI 文档 |

内部调用必须携带：

```text
Authorization: Bearer <LANGGRAPH_SERVICE_TOKEN>
X-Trace-Id: <trace-id>
X-Tenant-Id: <tenant-id>
X-User-Id: <user-id>
```

机器契约以 [OpenAPI v1.1](docs/docs/agent-api-v1.openapi.yaml) 为准；人工接口说明见 [LangGraph API v1.1](docs/docs/langgraph-api-v1.1.md)。

## 项目结构

```text
app/
├── api/          FastAPI 应用、认证、RAG 和 Run 接口
├── agent/        Agent 主图、Planner、LLM、Runtime、Store、配额和会话状态
├── graph/        可复用 RAG 子图和 LangGraph 导出入口
├── rag/          PDF 清洗、切分、Embedding、索引和混合检索
├── tools/        Spring Boot Agent Tool 客户端
└── cli/          索引构建、查询、迁移和 Worker 命令
docs/
├── docs/         API/OpenAPI、Tool Catalog 和联调示例
├── runbooks/     MongoDB 备份恢复等运维手册
└── source-data-quality.md  知识库原文限制与疑点
tests/            单元、契约、运行时、SSE 和可选真实联调测试
```

`langgraph.json` 同时导出两个图：

- `agent`：完整多意图 Agent 主图。
- `rag`：可嵌入其他业务 Agent 的独立 RAG 子图。

独立使用 RAG 子图：

```python
from app.graph import build_rag_graph

graph = build_rag_graph()
result = graph.invoke({"query": "步行指数的平均值是多少？", "top_k": 5})
```

## 特色亮点

1. **证据可追溯**：每个检索块包含文档哈希、章节路径、页码、内容类型和稳定 citation，方便审计与复现。
2. **中文与数字混合召回**：向量、中文 BM25、数字命中和意图加权联合排序，适合公式、比例、年份和指标数值问题。
3. **来源疑点显式化**：PDF 中的半径、衰减率、公式变量和统计比例疑点会进入回答约束，避免把原文缺陷包装成确定事实。
4. **确定性规划优先**：常见业务条件由规则解析；复杂权重或数值条件才进入结构化 LLM Planner，并接受 JSON Schema 和业务规则二次校验。
5. **空间计算职责清晰**：Agent 只调用 Tool 和映射结果，不下载图层、不重算距离、百分位或推荐分，保证业务口径一致。
6. **可恢复流式执行**：事件持久化后发送，支持幂等、取消、心跳、断线重放和单终态，适合长耗时地图任务。
7. **支持水平扩展**：MongoDB 事务存储、租约 Worker、SQLite 本地回退和 Redis 分布式限流覆盖开发到多实例部署。
8. **契约驱动联调**：Catalog 版本、SSE schema、几何坐标系、Tool 重试 ID 和错误码都有明确约束，并配套 A01-A11 场景。

## 数据范围与已知限制

当前知识库适合回答研究口径、步行指数与覆盖率公式、评分区间、汇总数量和论文结论；不包含 3712 个小区逐条指标、地图图层，也不能替代原始 POI/道路数据进行新的 GIS 计算。

原文疑点和数据边界见 [知识库数据质量说明](docs/source-data-quality.md)。Agent v1.1 的 Tool 参数、SSE 事件和联调要求见 [工程交接文档](docs/docs/agent-engineer-v1.1-handoff.md)。

## 许可证与使用说明

仓库当前未声明公开许可证。项目默认作为 YJDL 内部 Agent 服务使用，部署时请确认模型服务、PDF 数据及 GeoScene/Spring Boot 数据源的授权范围。
