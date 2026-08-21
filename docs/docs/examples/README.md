# Agent v1.1 契约样例

| 文件 | 用途 |
| --- | --- |
| [agent-sse-housing-buffer.txt](./agent-sse-housing-buffer.txt) | Housing candidates, contributing roads, and road-buffer overlay |
| [agent-tool-catalog-2026-08-21.1.json](./agent-tool-catalog-2026-08-21.1.json) | Current Spring Boot Tool Catalog fixture |
| [rag-search-fixture.json](./rag-search-fixture.json) | LangGraph RAG search fixture |

所有当前 SSE 样例使用 `schemaVersion=1.1`。每个事件块包含 `id`、`event` 和单行 JSON `data`。Tool Catalog 样例只保留当前版本，不应使用历史 Catalog 生成 Planner 参数。
