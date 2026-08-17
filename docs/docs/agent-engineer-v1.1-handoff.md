# Agent Contract v1.1 后续工程要求

更新时间：`2026-07-29`

本文面向 LangGraph/Agent 工程师。Spring Boot、前端和契约侧已完成 v1.1 实现；Agent 仓库必须按本文完成编排、事件组装和联调，不得回退到 v1.0 或自行重算空间业务结果。

## 1. 唯一契约来源

1. 启动时读取 `GET /internal/agent-tools/catalog`，要求版本严格等于 `2026-07-29.1`。
2. HTTP、SSE 和 DTO 以 [agent-api-v1.openapi.yaml](./agent-api-v1.openapi.yaml) 为准。
3. Catalog 固定样例使用 [agent-tool-catalog-2026-07-29.1.json](./examples/agent-tool-catalog-2026-07-29.1.json)。
4. 联合查询事件以 [agent-sse-housing-buffer.txt](./examples/agent-sse-housing-buffer.txt) 为准。
5. Catalog 版本不匹配时应阻止住宅道路联合搜索并暴露健康错误，不得继续使用旧缓存 Schema。

## 2. Planner 映射规则

| 用户表达 | Tool 参数 | 禁止行为 |
| --- | --- | --- |
| 房价不超过/以内 | `hardFilters.priceMax` | 不得转成软偏好 |
| 价格尽量低 | `preferences.price=PREFER_LOW` | 不得猜测 `priceMax` |
| 便利度高一点 | `preferences.convenience=PREFER_HIGH` | 只能映射 `归一化总分` |
| 便利度必须高/很高 | `HIGH`/`VERY_HIGH` | 不得由 LLM 猜绝对阈值 |
| 道路步行高一点 | `roadWalkability=PREFER_HIGH` | `WS` 只来自道路 3-5 层 |
| 高/很高 WS 道路附近 | `BUFFER_FILTER` + `HIGH`/`VERY_HIGH` | 不得使用点字段 `新步行` |
| 未指定行政区 | `districts=[]` | 不得逐区排名后拼接 |
| 未指定附近距离 | 省略 `spatial.bufferMeters` | 不得由 Agent 先写入 100 后伪装成后端默认 |

`新步行`、`归一化总分`、道路 `WS` 是三个不同字段。Agent 不得互相替代，也不得在 Tool 返回后重新计算便利度、道路 WS、百分位或推荐总分。

## 3. 参数构造

- 每次都提交 `price`、`convenience`、`roadWalkability` 三个偏好对象。
- 禁用价格偏好时使用 `enabled=false, level=PREFER_LOW, weight=0`。
- 便利度和道路步行同时使用默认基线时，省略二者 `weight`，由后端应用版本化 `0.5/0.5` 并返回 `PREFERENCE_WEIGHTS`。
- 只有一个偏好启用时显式传 `weight=1`；用户明确调整权重时先归一化，启用项之和必须为 1，禁用项必须为 0。
- 未给距离时只传 `spatial.relation=WITHIN_ROAD_BUFFER`；显式距离必须在 20-2000 米内。
- `BUFFER_FILTER` 未给 `roadCriteria.wsMin` 时，道路偏好必须启用且 level 为 `HIGH` 或 `VERY_HIGH`。
- 超过 2000 米、冲突硬条件、未知指标映射必须拒绝或澄清，禁止截断、放宽或替换字段。

## 4. Tool 调用与重试

1. 住宅和道路同时出现时只调用 `searchHousingCandidates`，不得拆成 `queryMapPoints` + `queryMapLines` 后在 Agent 内连接。
2. 一个逻辑调用生成一个 UUID `toolCallId`。网络超时重试必须复用原 ID 和完全相同的参数。
3. 超时且终态未知时查询 `GET /internal/agent-tools/executions/{toolCallId}`，不得立即生成新 ID 重算。
4. `INVALID_BUFFER_DISTANCE`、`INVALID_HOUSING_SEARCH_ARGUMENT`、`TOOL_CALL_CONFLICT` 不重试。
5. `METRIC_STATISTICS_UNAVAILABLE`、`DATA_VERSION_MISMATCH` 可提示稍后重试，但不得降级为无百分位或无空间条件查询。

## 5. map.result 组装

- 按 `layerId` 对 `housingCandidates` 分组，生成 `role=HOUSING_CANDIDATES` 的 point resultSet。
- 按 `layerId` 对 `roadFeatures` 分组，生成 `role=CONTRIBUTING_ROADS` 的 polyline resultSet。
- `bufferOverlays` 原样传到 `payload.overlays`，不得在 Python 或前端重新 buffer/dissolve。
- 图层顺序使用 `ROAD_BUFFER, CONTRIBUTING_ROADS, HOUSING_CANDIDATES`。
- 所有几何保留 `spatialReference.wkid=4326`；缺失或非法几何视为契约失败，禁止猜坐标系。
- 可把 Tool 的子分复制到地图 feature attributes 供展示，但不得覆盖原始属性或改变数值。
- 空住宅结果仍可返回贡献道路与缓冲区，并保留 `NO_HOUSING_IN_BUFFER`。

所有事件必须使用 `schemaVersion=1.1`。`tool.completed` 必须包含 `durationMs`；每个 Run 只有一个终态；重放只发送 `sequence > afterSequence` 的事件。

## 6. 性能要求

- 联合搜索只允许一次业务 Tool 调用，不得按住宅、道路或行政区产生 N+1 Tool fan-out。
- 不得下载六个图层到 Agent 进程做百分位、距离或 polygon 计算。
- `map.result` 总 feature 数不得超过 200；优先使用 Tool 已返回的 Top-K 和展示上限。
- Catalog 可按版本缓存，但健康检查发现版本变化后必须原子替换，不能混用两个版本。
- 默认场景联调目标：20 个住宅结果时 Tool P95 小于 3 秒；记录 Tool、Agent 编排和 SSE 发送三个分段耗时。

## 7. 必须补齐的 Agent 测试

- A01 房价硬过滤 + 便利度/道路默认软偏好。
- A02 未指定行政区，使用统一支持区域百分位。
- A03 `BUFFER_FILTER` 默认 100 米和 P75。
- A04 用户显式调整权重。
- A05 `VERY_HIGH` 使用 P90。
- A06 `PREFER_LOW` 不生成 `priceMax`。
- A07 10000 米返回 `INVALID_BUFFER_DISTANCE`，不截断为 2000。
- A08 空住宅仍输出道路和 buffer。
- A09/A10 相同 `toolCallId` 的幂等与冲突。
- A11 未知指标、`新步行` 替代 WS、禁用道路偏好执行 BUFFER_FILTER 均被澄清或拒绝。
- SSE 创建、取消、重放、单终态和 v1.1 polygon fixture 全部通过。

## 8. 联调完成标准

- LangGraph 启动日志确认 Catalog `2026-07-29.1`，且无 v1.0 事件。
- 真实 A01-A11 请求固化为请求、Tool 响应、SSE 三类 fixture。
- 桌面和移动端均显示 buffer、道路、住宅三层；清除结果后三层均为空。
- 真实 GeoScene 数据验证全域/单区 P75/P90、99.9/100/100.1 米边界、重叠道路去重和空结果。
- 输出 P50/P95 延迟、SSE 字节数、超时重试次数及错误码分布，达到性能目标后方可发布。
