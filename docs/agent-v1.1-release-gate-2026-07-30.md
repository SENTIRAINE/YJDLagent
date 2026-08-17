# Agent v1.1 发布门禁版本说明

版本日期：2026-07-31  
范围：仅 LangGraph Agent 工程，不包含 Spring Boot 和前端代码。

## 本版目标

本版把 Agent v1.1 的运行时约束、A01-A11 契约、真实模型冒烟、性能回归和发布证据统一为可执行门禁。模型只参与受约束的 Planner 和回答文案；百分位、阈值、距离、buffer、道路匹配、权重与排序仍由 Spring Tool 负责。

## 已完成

| 要求 | LangGraph 侧实现与证据 |
| --- | --- |
| 发布前验收 | `scripts/agent-v1.1-sse-acceptance.ps1` 支持 `Deterministic`、`Live`、`Regenerate`，并已纳入 `.github/workflows/agent-v1.1-release-gate.yml`。 |
| Tool 超时 | `AGENT_TOOL_TIMEOUT_SECONDS` 默认 125 秒，必须大于 Catalog 120 秒且小于 Run 超时；非法配置直接导致 ASGI 启动失败。 |
| Fixture 生命周期 | manifest 绑定 Catalog、Housing policy、GeoScene Schema 指纹和空间索引版本；`Regenerate` 生成 A01-A11 真实 Tool/SSE fixture 和性能基线。 |
| Readiness | `/readyz` 每次实时检查 Spring Tool health 和 Catalog；`housingSnapshot` 非 `READY`、缺失或版本不一致均返回 503，不长期复用旧 READY。 |
| 指标 | SQLite 持久化每次 Tool 参数、参数哈希、耗时、重试、错误码，Agent 编排耗时及每条 SSE 的字节数与耗时。内部接口为 `GET /api/v1/runs/{runId}/diagnostics`。 |
| 性能门禁 | Live 输出 P50/P75/P90/P95、SSE 字节、重试和错误分布；P95 超过受控基线两倍时失败。 |
| A09/A10 幂等 | Agent 相同 `messageId` 返回相同 `runId/toolCallId`，请求变化返回 `MESSAGE_CONFLICT`；Spring `toolCallId` 冲突由 A10 验证。 |
| A07/A11 失败语义 | 验收器要求唯一 `run.failed` 终态和固定错误码，不允许成功空结果或澄清文案掩盖契约错误。 |
| 后端结果保护 | Live 逐字段比较住宅、道路、评分、空间证据、几何和 buffer，模型或 Agent 不得改写 Spring Tool 结果。 |
| 发布证据 | Live 保存 Spring actuator health、Tool health、Catalog、Agent `/readyz`、逐场景 SSE/diagnostics/Tool execution、完整 GeoScene 探针及 P75/P90 SSE 汇总。 |

## Fixture 与版本变更

以下任一项变化都必须重新执行受控 `Regenerate`，不能只跑单元测试：

- Catalog version
- Housing policy version
- GeoScene 图层字段或输入/输出 Schema 指纹
- spatial index/data version

命令：

```powershell
$env:LANGGRAPH_SERVICE_TOKEN='<token>'
$env:AGENT_TOOL_SERVICE_TOKEN='<token>'
$env:AGENT_ACCEPTANCE_EXPECTED_MODEL='<production-model>'
.\scripts\agent-v1.1-sse-acceptance.ps1 -Mode Regenerate
```

生成后必须评审并提交 `tests/fixtures/agent-v1.1/` 下的 manifest、真实 Tool fixture、真实 Agent SSE fixture 与性能基线，再执行正式 `Live`。

## 正式发布门禁

正式流水线使用受保护环境 `agent-production-acceptance` 和标签为 `production-agent` 的 Windows 自托管 runner。需要配置：

- `AGENT_BASE_URL`、`SPRING_BOOT_BASE_URL`
- `OPENAI_MODEL`，必须与 `/readyz.model` 一致
- `LANGGRAPH_SERVICE_TOKEN`、`AGENT_TOOL_SERVICE_TOKEN`
- `AGENT_ACCEPTANCE_TENANT_ID`、`AGENT_ACCEPTANCE_USER_ID`
- `FRONTEND_EVIDENCE_PATH`

前端证据必须是 JSON 文件，同时证明桌面端和移动端的 `bufferVisible`、`roadsVisible`、`housingVisible`、`clearRemovesAll` 均为 `true`。该证据由前端工程产生，LangGraph 验收器只校验并归档，不能用后端测试代替。

正式命令：

```powershell
.\scripts\agent-v1.1-sse-acceptance.ps1 `
  -Mode Live `
  -RequireProductionFixtures `
  -RequireFrontendEvidence `
  -FrontendEvidencePath '<frontend-evidence.json>'
```

## 2026-07-31 审计补充

本轮完成性审计补强了两个不能绕过的版本门禁：

- `Live` 除了比较事件结构和 Tool 参数，还必须比较远端 Catalog version、GeoScene Schema 指纹、Housing policy version 与 spatial index/data version；任一差异都会失败并要求先执行受控 `Regenerate`。
- `Regenerate` 会同步生成版本化 Catalog、静态 A01-A11 Tool/SSE fixture 元数据、真实 Tool/SSE fixture 与 P50/P75/P90/P95 基线。下一次确定性门禁直接校验这四类版本字段，不能继续使用旧 Catalog 文件。
- `/readyz.runtimePolicy` 暴露实际运行中的 Run/Tool 超时；Live 要求 Tool 超时严格落在 `(120, Run)`，并与生产流水线配置的 `125` 秒一致，防止只给验收脚本设置超时而服务实际配置错误。

性能报告新增 Tool、Agent 编排、Agent 非 Tool、SSE 流和传输/SSE 阶段的 P95。总 P95 超过基线两倍时，会生成 `performance-regression-diagnosis.json`，明确阻止发布并禁止以提高超时作为处置方式。

正式 Live 门禁会在受保护的手动触发流程和 `v*` 发布标签上执行；缺少真实模型标识、前端证据或生产 fixture 时，发布标签同样失败。

## 当前验证状态

本地确定性门禁可在无真实模型、无 Spring 服务时执行。正式 `Live` 和 `Regenerate` 必须连接真实 Agent、Spring Tool、GeoScene 数据与模型服务；其通过结果才构成生产发布证据。

截至本文创建时，代码与流水线门禁已经完成，但当前工作区未连接 `127.0.0.1:8080` 的 Spring 服务，因此未伪造或宣称以下外部结果已完成：

- 真实模型受控冒烟
- 新空间索引对应的生产 fixture 与性能基线
- 桌面/移动端三层显示和清除回归
- 正式部署后的完整健康、GeoScene 与 P75/P90 SSE 证据归档

只有受保护的正式流水线全部通过后，才允许发布。
