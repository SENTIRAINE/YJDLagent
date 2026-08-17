# Agent v1.1 发布完成性审计（2026-07-31）

本文件是 `agent-v1.1-release-gate-2026-07-30.md` 的审计更新，记录当前可证明的完成项与仍需真实环境证明的项。

## 已验证的 LangGraph 交付

- `.github/workflows/agent-v1.1-release-gate.yml` 在 PR/主分支执行确定性门禁；`v*` 发布标签和受保护手动流程执行真实模型生产门禁。
- `AGENT_TOOL_TIMEOUT_SECONDS` 默认 125，配置必须满足 `120 < Tool timeout < Run timeout`；不满足时 ASGI 启动失败。`/readyz.runtimePolicy` 暴露正在运行的实际值，Live 会复核范围和生产值。
- `/readyz` 每次实时刷新 Catalog 和 Tool health；`housingSnapshot` 非 `READY`、缺失或 Catalog 不一致会返回 503，不复用旧 READY。
- A01-A11 的 Tool/SSE fixture 具备 Catalog、Housing policy、GeoScene Schema 指纹和 spatial index 生命周期约束。`Live` 发现其中任一实际版本漂移时拒绝发布；`Regenerate` 会同步生成新的 Catalog、fixture、真实 capture 和性能基线。
- 每个 Run 保存 Tool 参数及哈希、Tool/编排耗时、SSE 字节/耗时。P95 超过基线两倍时生成阶段化诊断并拒绝发布，明确禁止先提高超时。
- A09/A10、A07/A11、后端结果不改写和前端外部证据均已由验收器/流水线约束。

## 本地验证结果

2026-07-31 已通过：

```text
78 passed, 5 skipped
scripts/agent-v1.1-sse-acceptance.ps1 -Mode Deterministic
status=PASSED, catalog=2026-07-29.1, cases=11
```

## 尚未可证明的正式环境事项

本机没有可访问的 Agent (`:8000`) 或 Spring Tool (`:8080`) 服务，也未提供前端桌面/移动回归证据路径。因此尚未执行、也不能宣称完成：

1. 真实模型 `Regenerate`，生成包含实际 `spatialIndexVersion` 的受控生产 fixture 与性能基线；
2. 带 `-RequireProductionFixtures -RequireFrontendEvidence` 的 Live A01-A11 冒烟；
3. 部署后的 actuator、Tool health、Agent readyz、完整 GeoScene 探针、P75/P90 SSE 汇总归档；
4. 桌面和移动端 buffer/道路/住宅三层显示及清除回归。

这些不是可由本地单元测试替代的事项。服务与前端证据可用后，执行：

```powershell
$env:AGENT_ACCEPTANCE_EXPECTED_MODEL='<production-model>'
.\scripts\agent-v1.1-sse-acceptance.ps1 -Mode Regenerate
.\scripts\agent-v1.1-sse-acceptance.ps1 `
  -Mode Live `
  -RequireProductionFixtures `
  -RequireFrontendEvidence `
  -FrontendEvidencePath '<frontend-evidence.json>'
```

只有第二条命令及受保护发布流水线成功，才可将本目标判定为正式完成。
