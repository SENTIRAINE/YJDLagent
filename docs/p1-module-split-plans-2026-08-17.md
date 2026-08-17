# P1 大模块拆分计划

> 目标日期：2026-08-17  
> 适用范围：`app/agent/workflow.py`（约 1,877 行）、`app/agent/store.py`（约 1,355 行）  
> 核心约束：保持现有 Agent 图、Run API、SSE、Mongo/SQLite Store 契约不变；拆分后的模块必须可独立测试、可复用、依赖方向单向。

## 1. 当前结构判断

### Workflow 的职责混合

当前文件同时包含：

1. `AgentState`、JSON Schema 和路由契约；
2. 用户话术归一化、实体识别、价格/距离/道路条件解析；
3. Catalog 归一化和 Planner Schema 生成；
4. Geometry、Tool Result 一致性校验；
5. 地图结果组装、Housing 结果组装和回答摘要；
6. LangGraph 节点、分支、观测、Graph 编译。

其中前五类大多是纯函数，第六类是编排生命周期。它们现在共享同一个模块命名空间，导致修改一个业务规则时容易触发 Graph、SSE 和测试的广泛回归。

### Store 的职责混合

当前文件同时包含：

1. SQLite schema 初始化、迁移和 busy retry；
2. Run 幂等创建、状态读取和租约队列；
3. Conversation State、Memory 读写和窗口清理；
4. Event sequence、SSE replay 和 pending Tool；
5. Tool、Stage、Run、SSE、Quota 指标；
6. Legacy Checkpoint 搬迁；
7. `Memory + State + run.completed` 的原子完成事务。

拆分的关键不是按行数机械切文件，而是将“领域协议”和“存储实现”分开，同时保留事务边界。

## 2. 不可破坏的骨架

三套计划都必须遵守以下规则：

```text
API -> AgentRuntime -> Application Ports -> Mongo/SQLite Adapters
                         |
                         +-> LangGraph Graph Assembly
```

### 2.1 稳定入口

- `build_agent_graph(...)` 保持现有导入路径和参数兼容，内部改为调用 Graph Assembly。
- `AgentRuntime` 保持现有 `start_run`、`stream_events`、`cancel_run` 和 Worker 生命周期。
- `AgentStore` 保留兼容 Facade；Runtime 不直接依赖 SQLite 或 Mongo 具体类。
- `complete_run_with_memory(...)` 仍是一个原子应用命令，不能拆成三个独立提交。
- API/SSE/OpenAPI 事件名称、sequence、`run.completed` 可见时序不变。

### 2.2 依赖方向

- `contracts`、领域模型和 Protocol 不得导入 FastAPI、LangGraph、SQLite、PyMongo。
- 纯业务规则不得读取数据库、环境变量或全局 Runtime。
- Graph 节点只能依赖节点输入、`NodeServices` 和领域 Protocol，不得直接创建 Store/HTTP Client。
- Mongo/SQLite Adapter 可以依赖领域 Protocol，但领域层不能反向依赖 Adapter。
- 跨模块传递使用 TypedDict、dataclass 或 Pydantic Model，禁止用无约束的 `dict[str, Any]` 作为长期内部契约。

### 2.3 每一步的验收门禁

每个拆分 PR 必须同时通过：

1. 原有全量测试；
2. Mongo live、多实例、滚动恢复测试；
3. 原始连续对话测试；
4. `python -m compileall app tests`；
5. import-linter 或自定义依赖方向检查；
6. 新旧入口行为快照对比。

## 3. 计划 A：渐进式绞杀拆分（推荐）

### 定位

这是风险最低、最适合当前已上线骨架的方案。保留现有 `workflow.py` 和 `AgentStore` 作为兼容 Facade，每次只迁移一组职责，旧入口转发到新模块，确认稳定后再删除旧实现。

### 目标目录

```text
app/agent/
  workflow.py                 # 兼容入口，最终只保留 build_agent_graph/re-export
  workflow_contracts.py       # AgentState、NodeServices、Graph 配置
  workflow_rules.py           # 归一化、实体/条件/偏好解析
  workflow_catalog.py         # Catalog 归一化、压缩和 Planner schema
  workflow_results.py         # Geometry、Tool result、Map/Housing result
  workflow_nodes/
    routing.py
    retrieval.py
    planning.py
    execution.py
    answering.py
  graph_builder.py            # 节点装配、边和观测
  repositories/
    protocols.py
    run_repository.py
    conversation_repository.py
    event_repository.py
    metrics_repository.py
    lease_repository.py
  store.py                    # AgentStore 兼容 Facade
  sqlite_store.py              # SQLite Adapter
  mongo_store.py               # Mongo Adapter
```

### 实施顺序

#### A1：先抽纯函数，不改 Graph

- 从 `workflow.py` 迁移 `normalize_user_query`、价格/距离/道路条件解析、实体和偏好判断到 `workflow_rules.py`。
- 迁移 Catalog 和 Planner Schema 到 `workflow_catalog.py`。
- 迁移 Geometry、Tool result、Map/Housing result 到 `workflow_results.py`。
- `workflow.py` 暂时保留同名 re-export，所有旧测试路径继续有效。

验收重点：纯函数测试数量不减少；输入输出 JSON 完全一致；不引入 LangGraph 或 HTTP 依赖。

#### A2：抽 Graph 节点，保留一个装配器

- 将 `route_intent`、`retrieve_knowledge`、`plan_housing_search`、`plan_map`、`execute_map_tools`、`compose_answer` 分别迁移。
- 引入只读依赖对象：

```python
@dataclass(frozen=True)
class NodeServices:
    llm: ChatModelPort
    rag: RagPort
    tools: ToolPort
    metrics: MetricsPort | None
    map_result_limit: int
```

- `graph_builder.py` 负责创建节点闭包、观察包装和 Graph edge。
- `workflow.py:build_agent_graph` 只负责兼容参数转换并调用 `graph_builder`。

验收重点：节点可脱离完整 Runtime 用 Fake Services 测试；Graph snapshot、SSE event sequence 和 Tool id 不变。

#### A3：先定义 Store Protocol，再包住旧 Store

- 在 `repositories/protocols.py` 定义 `RunRepository`、`ConversationRepository`、`EventRepository`、`LeaseRepository`、`MetricsRepository`。
- `AgentStore` 暂时实现全部 Protocol，Runtime 改为依赖 Protocol 组合对象。
- 不立即复制 SQLite SQL，避免产生两套实现。

#### A4：拆 SQLite/Mongo Adapter，事务通过 Unit of Work 保持

- SQLite 和 Mongo 分别实现相同 Protocol。
- 新增最小的 `CompletionUnitOfWork` 或 `complete_run_with_memory` 命令接口，事务内部依次完成 Memory、State CAS、Event/Run 更新。
- `AgentStore` 继续作为 Facade，负责组装 Adapter，兼容已有调用方。

### 优缺点

| 项目 | 评价 |
| --- | --- |
| 风险 | 最低，可逐 PR 回滚 |
| 迁移兼容 | 最好，旧 import 和 API 保留 |
| 低耦合 | 中高，需在 A3/A4 严格执行 Protocol |
| 交付周期 | 4 个小 PR，约 1.5 至 2 周 |
| 推荐 | **首选** |

## 4. 计划 B：按业务垂直切片拆分

### 定位

按用户业务闭环拆分，而不是按技术层拆分。每个 Slice 同时拥有自己的规则、Planner/节点和 Repository Port，适合未来增加道路、Housing、RAG 等独立业务能力。

### 目标目录

```text
app/agent/
  core/
    state.py
    ports.py
    events.py
  slices/
    conversation/
      router.py
      state_policy.py
      answer.py
      repository.py
    housing/
      rules.py
      planner.py
      result_mapper.py
      repository.py
    map_query/
      planner.py
      geometry.py
      result_mapper.py
      repository.py
    knowledge/
      retrieval.py
      answer.py
  orchestration/
    graph_builder.py
    runtime.py
  infrastructure/
    sqlite/
    mongo/
```

### 实施顺序

#### B1：建立 Core Kernel

- 将 `AgentState`、Intent、Event 名称、ToolPlan、MapSummary、ConversationBusinessState 放入 `core`。
- 定义 `ChatModelPort`、`ToolCatalogPort`、`ToolExecutionPort`、`RagPort` 和 `MetricsPort`。
- 任何 Slice 只能依赖 Core，不得依赖另一个 Slice 的具体实现。

#### B2：迁移 Housing Slice

- 迁移 `is_housing_search_query`、Housing 参数归一化、联合搜索 Planner、Housing result mapper。
- Conversation state 只通过 `ConversationStatePort` 读写，不直接访问 Store。
- 用“原始两轮真实话术”作为 Housing Slice 的发布门禁。

#### B3：迁移 Map Query 和 Knowledge Slice

- Map Query 独立负责 Catalog、点/线 Planner、Geometry 校验和 Map result。
- Knowledge 只负责 RAG 检索和证据回答，不拥有 Run/Event 持久化。
- Hybrid 由 Orchestration 组合两个 Slice 的结果，不让 Slice 互相调用。

#### B4：按 Slice 拆 Repository Port，统一 Adapter

- Housing/Map/Conversation 的业务仓储接口只表达领域动作，例如 `commit_turn`、`load_query_context`、`save_result_reference`。
- Mongo/SQLite Adapter 实现共享的底层 `StorageSession` 和事务 Protocol。
- 复杂事务只在 Orchestration 的 Completion Command 中协调，不在 Slice 内互相嵌套事务。

### 优缺点

| 项目 | 评价 |
| --- | --- |
| 风险 | 中等，业务边界清晰但迁移面较大 |
| 可复用性 | 高，Housing/Map Slice 可被批处理或离线推荐复用 |
| 低耦合 | 高，前提是禁止 Slice 互相导入 |
| 交付周期 | 4 个阶段，约 2 至 3 周 |
| 适合场景 | 业务能力将持续增加、需要团队并行开发 |

## 5. 计划 C：六边形架构重构

### 定位

一次性建立 Application Core、Ports 和 Infrastructure Adapters。Graph、HTTP、Mongo、SQLite 都成为外部适配器，核心用例不感知 LangGraph 和具体数据库。架构收益最高，但迁移风险和测试改造成本也最高。

### 目标目录

```text
app/agent/
  domain/
    model.py
    policies.py
    errors.py
  application/
    commands.py              # StartRun、ExecuteRun、CompleteRun、CancelRun
    services.py
    ports.py
  adapters/
    graph/
      nodes.py
      builder.py
    persistence/
      mongo/
      sqlite/
    transport/
      spring_tools.py
      llm.py
      rag.py
  composition/
    runtime_factory.py
    graph_factory.py
  workflow.py                # 仅旧入口兼容层
  store.py                   # 仅旧入口兼容层
```

### 实施顺序

#### C1：定义 Application Commands

将 Runtime 的业务动作显式建模：

- `StartRunCommand`：幂等创建、依赖计算、配额预留；
- `ExecuteRunCommand`：执行 Graph、Tool 和回答；
- `CompleteRunCommand`：Memory、State、Event 原子提交；
- `CancelRunCommand`：租约 fencing 和终态事件。

Command 只依赖 Ports，所有输出使用 typed result，不返回数据库 Row 或 LangGraph snapshot。

#### C2：建立 Persistence Unit of Work

```python
class PersistenceUnitOfWork(Protocol):
    runs: RunRepository
    conversations: ConversationRepository
    events: EventRepository
    metrics: MetricsRepository

    def complete_run(self, command: CompleteRunCommand) -> CompletedRun: ...
```

Mongo 和 SQLite 各自实现 Unit of Work。`complete_run` 内部保留单事务，禁止由 Application Service 拼接多个 repository 的独立 commit。

#### C3：Graph 适配器化

- LangGraph 节点只做输入/输出转换和 Port 调用。
- Graph Builder 根据 `GraphDependencies` 装配节点。
- 可用一个非 LangGraph 的同步 Fake Executor 测试 Application Command，减少业务测试对 Graph snapshot 的依赖。

#### C4：Runtime 和 Store 变成兼容适配器

- `AgentRuntime` 只负责 Worker、lease 和 Stream transport；业务提交委托 Application Service。
- `AgentStore` 保留旧方法签名，将调用转发给 Persistence Unit of Work。
- API、Worker CLI 和 live tests 逐步切到 Composition Root。

#### C5：删除重复 Facade 实现

只有在所有调用方和迁移数据验证完成后，删除 `workflow.py`、`store.py` 中的重复实现。该步骤必须单独发布，禁止和 C1-C4 同 PR 合并。

### 优缺点

| 项目 | 评价 |
| --- | --- |
| 风险 | 最高，需要长期双轨验证 |
| 可复用性 | 最高，核心用例可用于 API、Worker、批处理和测试 |
| 低耦合 | 最高，边界最清晰 |
| 交付周期 | 5 个阶段，约 4 至 6 周 |
| 适合场景 | 团队已有架构治理能力，且准备长期投入 |

## 6. 推荐决策

推荐采用 **计划 A 作为当前主线**，吸收计划 B 的业务 Slice 命名，暂不进行计划 C 的一次性重构。

推荐落地顺序：

```text
A1 纯函数与契约
  -> A2 Graph 节点与装配
  -> A3 Store Protocol 与兼容 Facade
  -> A4 Mongo/SQLite Adapter 与 Completion Unit of Work
  -> B1 Core Kernel 的 Ports 收敛
```

这样可以先降低文件复杂度和测试回归面，再逐步获得垂直 Slice 和 Ports/Adapters 的收益。每个阶段都能独立发布和回滚，不会把现有 Mongo 事务、Worker 租约或连续对话状态一次性暴露在大范围重构风险中。

## 7. 代码审查门禁

拆分完成后，评审必须拒绝以下情况：

- 新模块重新导入整个 `workflow.py` 或 `store.py`，形成反向依赖；
- 为了复用而暴露 SQLite connection、Mongo session 或 LangGraph state；
- 将 `complete_run_with_memory` 拆成可被调用方分别提交的三步；
- 在业务 Slice 中直接读取环境变量或创建 HTTP Client；
- 用 `Any` 替代已经存在的 typed state、ToolPlan 和 MapSummary；
- 只迁移实现而没有迁移对应测试；
- 用“大文件变小”作为唯一验收，而没有证明 SSE、事务和跨实例行为不变。

## 8. 完成定义

一次合格的 P1 拆分必须同时满足：

1. `workflow.py` 和 `store.py` 只剩兼容入口或明确的 Composition Facade；
2. 新模块可以独立导入和单元测试；
3. Mongo/SQLite 共享同一领域 Protocol，行为差异只存在于 Adapter；
4. `Memory + State + run.completed` 仍在单个事务中；
5. Worker 租约、依赖队列、SSE sequence 和 Checkpoint transient/tracked 边界不变；
6. 全量、Mongo live、Spring live、LLM live 和真实连续对话门禁全部通过；
7. 依赖方向检查无环，新增模块不依赖兼容 Facade。

