# Silver Residence Spring 综合测试报告

测试日期：`2026-08-02`  
测试对象：本仓库 Spring Boot 服务（LangGraph、MongoDB、GeoScene 处于真实运行状态）  
结论：**Spring 核心 Agent Tool 与地图数据链路通过，但用户认证接口存在可稳定复现的 500，当前不建议直接发布。**

## 1. 项目理解与系统边界

Silver Residence 是面向适老居住选择的地图与智能问答系统。用户在浏览器登录后，用自然语言描述房价、便利度、道路步行性和距离偏好；系统将这些需求转换为住宅、道路和缓冲区查询，并在地图上展示候选结果，同时生成解释性回答。

当前主链路为：

```text
浏览器
  -> Spring Boot：登录会话、SSE 网关、Tool 契约校验、住宅/道路空间计算
  -> LangGraph：意图路由、任务编排、RAG、Tool 选择、回答生成
  -> Spring Boot Agent Tools：结构化地图查询与 searchHousingCandidates
  -> GeoScene：0-5 层真实住宅/道路数据
```

Spring 是浏览器的唯一后端入口，也是业务数据与空间计算的权威来源；LangGraph 不应直接访问 GeoScene，也不应自行重算 Spring 已返回的价格、便利度、道路 WS、百分位或缓冲区。v1.1 当前只提供只读地图查询、住宅道路联合搜索与 RAG。

主要实现区域：

| 区域 | 职责 |
| --- | --- |
| `controller/` | 用户登录/注册、Assistant SSE 网关、Map 与 Agent Tool HTTP 接口 |
| `agent/` | Spring 到 LangGraph 的运行、续流、取消、会话身份和 SSE 契约 |
| `housing/` | 住宅筛选、偏好评分、道路空间关联、快照与百分位 |
| `map/` | GeoScene 图层定义、查询、属性/几何转换 |
| `api/` | v1.1 DTO、错误响应与全局异常处理 |
| `static/` | 登录页、用户地图、聊天和结果渲染 |
| `docs/` | OpenAPI、Agent 稳定契约、发布基线与设计说明 |

## 2. 测试环境与方法

| 项目 | 实测值 |
| --- | --- |
| Spring | `http://127.0.0.1:8080`，`/actuator/health` HTTP 200 |
| LangGraph | `http://127.0.0.1:8000`，`/healthz`、`/readyz` HTTP 200 |
| LangGraph 模型 | `gpt-5.4` |
| Tool Catalog | `2026-07-29.1` |
| Housing snapshot | `READY`，`geoscene-snapshot-2026-08-02T07:41:41Z` |
| MongoDB | `127.0.0.1:27017` |
| 测试 JDK / Maven | JDK 21 / Maven 3.9.14（本机缓存发行版） |
| 浏览器 | Microsoft Edge，桌面与 `390 x 844` 移动视口 |

测试采用四层证据：

1. Spring 单元、集成与契约回归。
2. GeoScene 六层真实 HTTP 探针与完整数据量核对。
3. 直接调用 Spring Agent Tool 的 A01-A11 契约与性能验收。
4. 通过真实注册、登录和 Assistant SSE 的用户场景测试，覆盖标准表达、老人日常说法、赘词、错别字、极短表达、复合需求和输入边界；再使用浏览器验证地图与移动布局。

本轮没有修改生产代码。新增的可复用场景脚本为 `scripts/spring-user-scenarios-acceptance.ps1`。

## 3. 总体结果

| 测试域 | 结果 | 关键数字 |
| --- | --- | --- |
| Spring 自动化回归 | 通过 | 49 tests，0 failures，0 errors，1 skipped |
| GeoScene 完整探针 | 通过 | 24/24 检查通过；六层共 2972 条，无传输截断 |
| Housing Tool A01-A11 | 通过 | 所有正常、拒绝、幂等和冲突场景符合预期 |
| Housing Tool 性能 | 通过 | P75 Tool P95 21 ms；P90 Tool P95 17 ms；目标 < 3000 ms |
| Spring 边界与安全契约 | 部分通过 | 5 项通过，错误密码与空登录体 2 项失败 |
| 真实用户自然语言 | 有明显 LangGraph 行为问题 | U01-U12 中 3 项进入有效非澄清路径，9 项进入 `CLARIFY` |
| 浏览器桌面/移动端 | 基础布局通过 | 地图实际绘制 20 住宅、30 道路、5 缓冲区；未见不连贯重叠 |
| 构建复现 | 失败 | `mvnw.cmd` 无法启动 Maven；缓存 Maven 可完成测试 |

发布判断：**暂缓发布。** Spring Tool、GeoScene、地图渲染和 SSE 传输基础是健康的，但认证接口可由常见用户输入触发 HTTP 500，且密码存储仍使用 MD5。至少应完成 S-01、S-02、S-04 并回归后再进入发布候选。

## 4. 已通过的 Spring 验证

### 4.1 自动化与真实数据

- 49 个测试中 48 个通过、1 个跳过；跳过项依赖真实 LangGraph，本轮已另外通过运行中的 LangGraph 完成端到端覆盖。
- GeoScene 图层 0-5 的 metadata、空 count、真实 count 和完整属性/几何查询全部成功。
- 六层数据量依次为 `564 / 288 / 483 / 542 / 445 / 650`，总计 `2972`；完整返回量与 count 一致。
- `searchHousingCandidates` 的价格硬过滤、P75/P90、默认 100 米、显式 WS/距离、便利度权重、低价软偏好、空住宅保留道路/缓冲、幂等重试、调用冲突和非法字段拒绝均符合契约。
- 热路径性能远低于 3 秒门槛：P75 Tool P95 `21 ms`，P90 Tool P95 `17 ms`。

### 4.2 HTTP、身份与输入边界

- 未知路由返回 HTTP 404 / `NOT_FOUND`。
- 原始 `where` 被 HTTP 400 / `RAW_WHERE_NOT_ALLOWED` 拒绝。
- 未带服务 Token 的内部 Catalog 返回 HTTP 401 / `INVALID_SERVICE_IDENTITY`。
- 未登录调用 Assistant 返回 HTTP 401 / `AUTHENTICATION_REQUIRED`。
- 4001 字查询返回 HTTP 400 的 SSE `preflight.failed` / `INVALID_AGENT_QUERY`。
- 登录成功响应不会返回密码；注销成功并使原会话失效。
- 匿名读取地图配置成功，符合当前设计。
- Assistant SSE 连接、命名事件、地图结果透传与前端绘制链路可工作；LangGraph 终端失败发生时，Spring 连接仍为 HTTP 200 SSE，地图数据已经到达前端。

## 5. Spring 缺陷与修复方法

### S-01 错误密码返回 HTTP 500，而不是受控认证失败

严重度：**高**  
复现：`POST /user/login`，使用存在的用户名和错误密码。  
实际：HTTP 500，`error.code=INTERNAL_ERROR`，消息为“服务暂时无法处理请求”。  
期望：HTTP 401，使用稳定、非枚举式的认证错误码和用户可理解消息。

根因：`UserServiceImpl.login()` 在用户名或密码不匹配时抛出裸 `RuntimeException`（第 53-59 行）；`UserController.login()` 第 45-57 行没有转换认证异常；最终由 `MapContractExceptionHandler` 第 75-95 行按未知异常包装为 500。

修复方法：

1. 定义明确的认证异常，例如 `AuthenticationFailedException`，服务层对用户名不存在和密码不匹配都抛出同一异常，避免账号枚举。
2. 在统一异常处理器中将该异常映射为 HTTP 401，并返回稳定错误码，例如 `AUTHENTICATION_FAILED`，`retryable=false`。
3. 前端登录失败同时兼容当前 `Result` 与契约错误体，显示“用户名或密码不正确”，不要把 401 当作网络异常。
4. 增加 Controller 集成测试：正确密码 200、错误密码 401、未知用户名 401，且两种失败对外消息一致、都不创建 Session。

### S-02 空登录体触发空指针并返回 HTTP 500

严重度：**高**  
复现：`POST /user/login`，JSON 为 `{}`。  
实际：HTTP 500 / `INTERNAL_ERROR`。  
期望：HTTP 400 / 明确的请求校验错误。

根因：接口直接接收实体 `User`，没有服务端 Bean Validation；空密码到达 `password.getBytes()`（`UserServiceImpl` 第 56 行）后抛出异常。浏览器校验不能保护 API 被直接调用的情况。

修复方法：

1. 新建最小登录 DTO，给 `username`、`password` 添加 `@NotBlank` 和合理长度上限；Controller 使用 `@Valid @RequestBody LoginRequest`。
2. 增加 `MethodArgumentNotValidException` 处理，将校验失败统一映射为 HTTP 400 / `INVALID_REQUEST`，且不得回显密码。
3. 服务层仍保留 null/blank 防御，避免绕过 Controller 后出现 NPE。
4. 增加 `{}`、缺用户名、缺密码、空白字符串、超长字段、非法 JSON 六类集成测试，并断言服务和仓库不会在校验失败时执行。

### S-03 重复注册的后端字段与前端读取字段不一致

严重度：**中**  
复现：在真实注册页再次提交已存在用户名。  
实际：后端 HTTP 200 返回 `{code:0,msg:"用户名已存在，请更换！"}`，页面读取 `data.message`，最终只显示“注册失败，请稍后再试。”  
期望：用户看到准确、可操作的冲突提示。

根因：注册 Controller 捕获异常后返回 `Result.error()`（`UserController` 第 28-36 行），结果字段为 `msg`；前端 `main.js` 第 151-154 行只读取 `data.message`。登录失败分支第 108-111 行存在同样字段风险。

修复方法：

1. 确定一个响应契约并全局统一。短期前端读取 `data.msg || data.message`；长期建议注册冲突返回 HTTP 409 和稳定错误码，而不是 HTTP 200 的业务失败。
2. 用注册 DTO 增加 `@NotBlank`、邮箱格式、手机号格式和密码长度校验；数据库为用户名、邮箱建立唯一索引，避免并发注册竞态。
3. 不直接把任意异常消息返回浏览器；只映射已知业务异常，未知异常进入统一 500 并记录 traceId。
4. 增加真实 DOM 测试，断言重复用户名与重复邮箱分别显示准确提示，而不是通用兜底消息。

### S-04 密码使用无盐 MD5 存储

严重度：**高（安全债务）**  
证据：`UserServiceImpl` 第 34-36、56 行使用 `DigestUtils.md5DigestAsHex()` 注册和登录。

风险：MD5 速度快、无盐，不能抵抗离线字典和彩虹表攻击，不适合密码存储。

修复方法：

1. 使用 Spring Security `PasswordEncoder`，优先 BCrypt 或 Argon2；新注册只写强哈希。
2. 为已有 MD5 增加可识别的哈希版本。老用户登录验证成功后立即重哈希升级，不要求一次性获得明文密码迁移。
3. 禁止日志、响应和测试产物保存明文密码；部署前轮换本轮测试账号或删除该账号。
4. 增加测试：同一密码两次编码结果不同但都能匹配；错误密码不匹配；旧 MD5 登录后完成升级；响应与日志不含密码。

### S-05 Maven Wrapper 在当前 PowerShell 无法运行

严重度：**中**  
复现：执行 `.\mvnw.cmd -v`。  
实际：`icm : Cannot index into a null array`，随后输出 `Cannot start maven from wrapper`。  
影响：文档给出的标准测试命令不可复现；本轮只能用本机缓存 Maven 3.9.14 完成 49 项测试。

修复方法：

1. 使用官方 Maven Wrapper 重新生成并提交完整 wrapper 文件，固定受支持的 Maven 发行版；不要保留依赖特定 PowerShell 注入逻辑的定制启动片段。
2. 在干净 Windows PowerShell、`cmd.exe` 和 CI 环境分别执行 `mvnw.cmd -v`、`mvnw.cmd test`。
3. CI 必须只依赖仓库 wrapper 与声明的 JDK，不应依赖开发机全局 Maven 缓存。

### S-06 Java 版本声明与测试文档不一致

严重度：**中**  
证据：`pom.xml` 第 29-31 行声明 Java 17；`docs/README.md` 第 62-70 行要求 JDK 21，并说明 Java 17 在当前机器缺少可用 Attach Provider。

修复方法：统一唯一基线。按当前已验证环境，建议将构建、CI、部署和文档统一到 JDK 21，并用 Maven Enforcer 或 Toolchains 在版本不符时快速失败；若产品必须兼容 Java 17，则需要先在干净 JDK 17 环境解决 Mockito Attach 问题并重新完成全部门禁，再修改文档。

### S-07 登录页缺少 favicon，产生浏览器 404

严重度：**低**  
实际：访问登录页时浏览器请求 `/favicon.ico` 并得到 404。`user.html` 已使用 `<link rel="icon" href="data:,">`，`index.html` 未配置。

修复方法：在 `index.html` 的 `<head>` 增加与 `user.html` 一致的 data favicon，或提供真实静态 favicon；增加浏览器控制台断言，登录页不应出现资源 404。

## 6. LangGraph 观测记录（仅留证，不提供修复方法）

本节只记录 LangGraph 在真实运行时的可复现行为与 Spring 侧归属证据，**不包含 LangGraph 修复建议**。

### 6.1 自然语言场景汇总

U01-U12 共 12 个用户表达，只有 U01、U06、U08 进入有效非澄清路径；其余 9 个进入 `CLARIFY`。U08 的非专业指标解释清楚、带引用，适合普通和老年用户。其余重点证据如下：

| 场景 | 输入摘要 | 实际行为 |
| --- | --- | --- |
| U01 | 中山区、房价不超过 1.5 万、便利一点 | 调用了 `searchHousingCandidates`，但结果包含 19027 等超过上限的房源，`appliedFilters` 为空；回答也承认价格与便利条件未应用 |
| U02 | 腿脚不好、走路方便、1.2 万/㎡以内 | 要求先补充区域；稳定契约允许未指定行政区时使用全支持区域 |
| U03 | 别太吵、买菜方便 | 要求城市或区域，未进入工具路径 |
| U04 | 便宜点、出门路好走 | 追问最高单价，未应用可支持的低价软偏好 |
| U05 | 含“中山去 / 一万五一内 / 步行指树”错别字 | 追问找房子还是道路 |
| U06 | 高步行指数道路附近、百来米 | 返回 5 个 resultSet 与 5 个 overlay，浏览器绘制 20 住宅、30 道路、5 缓冲区；文本仍称证据不足 |
| U07 | 高步行指数道路 1 万米内 | 进入澄清，没有工具调用 |
| U08 | “步行指数是啥意思，我看不懂” | RAG 成功，返回 5 条引用来源并给出可读解释 |
| U09 | 老两口、1.3 万、便利度高、路舒服 | 追问“便利度”含义；Spring 契约已固定便利度映射为 `归一化总分` |
| U10 | 总预算 200 万养老 | 识别口径不足并澄清 |
| U11 | “好的、住着省心” | 识别高度含糊并澄清 |
| U12 | “便宜，好走路” | 追问要找房子还是道路，未应用受支持偏好 |

U01 对应的 Spring Tool A01 已独立验证价格硬过滤正确。因此 U01 的越界结果归属于 LangGraph 的规划或参数构造阶段，不是 `searchHousingCandidates` 执行错误。

### 6.2 间歇性终端失败

浏览器重复执行 U06 时观测到两次“模型服务暂时无法完成请求”。其中一次请求 traceId 为 `0f1cbbd6-d4e7-4b4f-8a32-c32c452aca22`。当终端失败出现时：

- Spring 对浏览器的连接已成功建立并保持为 HTTP 200 SSE。
- `map.result` 已到达浏览器，地图已绘制 20 个住宅、30 条道路和 5 个缓冲区。
- 随后才出现 LangGraph 模型终端失败提示。

这些现象证明 Spring 传输和地图渲染在该次请求中已经完成，失败点位于 Spring 下游的 LangGraph/模型阶段。本报告按要求仅保留证据，不给出 LangGraph 修复方法。

## 7. 浏览器验收

- 使用真实 UI 完成注册、登录、用户地图进入、Assistant 请求和注销。
- 桌面地图、聊天面板、结果图层可以同时呈现，无明显遮挡或布局断裂。
- `390 x 844` 移动视口完成结果展示检查，没有横向溢出或文字越界。
- 重复注册页面稳定复现通用兜底提示，证明 S-03 是用户可见问题。
- 登录页控制台只有 favicon 404；用户页已通过 data favicon 避免同类请求。

测试账号：`codex_spring_20260802_1602`。该账号仅用于本轮验收，生产或共享环境应在验收后删除或禁用。

## 8. 证据索引

| 证据 | 路径 |
| --- | --- |
| 用户场景汇总 | `outputs/spring-comprehensive-2026-08-02/user-scenarios/summary.json` |
| 每场景请求与原始 SSE | `outputs/spring-comprehensive-2026-08-02/user-scenarios/` |
| 注册、注销与会话边界 | `outputs/spring-comprehensive-2026-08-02/user-scenarios/boundary-extra.json` |
| Housing A01-A11 汇总 | `outputs/spring-comprehensive-2026-08-02/housing-acceptance/summary.json` |
| Housing 每案请求/响应 | `outputs/spring-comprehensive-2026-08-02/housing-acceptance/` |
| 地图加载截图 | `output/playwright/spring-comprehensive-2026-08-02/01-user-map-loaded.png` |
| LangGraph 终端失败截图 | `output/playwright/spring-comprehensive-2026-08-02/02-langgraph-failure-after-map.png` |
| 移动端结果截图 | `output/playwright/spring-comprehensive-2026-08-02/03-mobile-agent-result.png` |
| 重复注册提示截图 | `output/playwright/spring-comprehensive-2026-08-02/04-duplicate-registration-message.png` |
| 可复用场景脚本 | `scripts/spring-user-scenarios-acceptance.ps1` |

## 9. 修复后的最小回归门禁

1. `mvnw.cmd -v` 与 `mvnw.cmd test` 在干净 Windows/CI 成功，结果不得低于 49 tests、0 failures、0 errors。
2. 错误密码和未知用户名均返回 401，不创建 Session；空/缺失登录字段返回 400，不出现 500。
3. 重复用户名和重复邮箱在真实页面显示准确提示；并发重复注册由唯一索引可靠拒绝。
4. 新密码使用 BCrypt/Argon2，旧 MD5 账号具备受测的渐进迁移路径。
5. A01-A11 全部重放通过，P75/P90 Tool P95 继续小于 3 秒。
6. 六层 GeoScene 完整探针仍为 24/24，通过量仍与真实 count 一致。
7. 桌面与移动浏览器控制台无未解释错误、资源 404、横向溢出或关键内容遮挡。

完成以上门禁后，Spring 可重新评估为发布候选；LangGraph 的自然语言行为与间歇性模型失败应作为独立验收项判定，不能用 Spring Tool 的通过结果替代。
