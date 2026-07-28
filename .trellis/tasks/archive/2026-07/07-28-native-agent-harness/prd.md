# Native Agent Framework

## Goal

把 `by-framework` 扩展为一个完整的原生 Agent Framework：在 Agent 定义、模型与工具执行、状态图、多 Agent 编排、持久化恢复和开发者 API 层面与 LangGraph、ADK、AutoGen 等框架处于同一产品层级，同时保留 Redis Streams 分布式调度、Worker 隔离和外部 Agent 集成优势。

用户既可以继续运行现有 `GatewayWorker`、LangGraph 或 ADK Agent，也可以使用框架原生的 `Agent`、`Team`、`StateGraph` 和 durable execution engine；两条路径是可选且平级的。

## Background and Repository Evidence

- `GatewayWorker` 只要求声明 agent type 并实现 `process_command(command, context)`，因此它是传输与生命周期容器，不是 Agent Harness（`src/by_framework/worker/worker.py:119-153`）。
- Worker 已统一创建 `AgentContext`、绑定配置快照、保存用户消息、准备 workspace/sandbox 并规范化结果；原生框架应复用而不是复制该生命周期（`src/by_framework/worker/worker.py:509-697`）。
- `AgentContext` 已提供流式输出、历史落盘、取消、人工询问、token usage、单 Agent 调用、scatter-gather 与结果收集（`src/by_framework/worker/context.py:294-520`, `src/by_framework/worker/context.py:524-976`）。
- 当前历史接口可存储 `user`、`assistant`、`system`、`tool`，但自动落盘主要围绕用户输入与最终文本，不能表达完整、可恢复的 tool/graph transcript（`src/by_framework/core/runtime/history/base.py:17-48`, `src/by_framework/worker/worker.py:607-616`, `src/by_framework/worker/context.py:391-406`）。
- `AgentConfig` 已声明 prompts、tools、skills、callbacks、knowledge bases 与 sub-agents，但主要使用开放字典，核心没有执行这些声明的模型/工具循环（`src/by_framework/core/extensions/agent_config.py:16-64`）。
- `by-framework-langgraph` 与 `by-framework-adk` 已证明外部 Harness 能映射到 Worker 生命周期；它们继续作为平级可选集成（`libs/by-framework-langgraph/src/by_framework_langgraph/worker.py:26-182`, `libs/by-framework-adk/src/by_framework_adk/worker.py:26-114`）。
- 核心当前保持轻依赖，不包含模型 SDK；具体 provider 与外部框架应继续位于可选包（`pyproject.toml:7-12`, `pyproject.toml:46-56`）。

## Requirements

### R1. Product and API Model

- 采用 “Agent-first, Graph-capable” 主范式。
- 普通开发者以 `Agent`、`Tool`、`Team`、`Handoff` 和 `Runner` 为主要 API。
- 高级开发者可直接使用 typed state、node、edge、conditional routing 和 subgraph。
- Agent API、Team API 与 Graph API 必须编译到同一套可版本化 `ExecutionPlan`，共享运行、恢复、观测和错误语义。
- 底层提供可组合的 headless runtime；`NativeAgentWorker` 等高层入口只做组装。

### R2. Agent Specification and Compatibility

- 开发者使用可包含动态 Python 对象的 `Agent`。
- 运行前编译为冻结、可验证、可序列化、带版本指纹的 `AgentSpec` 与 `ExecutionPlan`。
- 现有 `AgentConfig` 保留为插件兼容入口，通过显式转换器进入统一 catalog。
- 现有 `GatewayWorker`、`LangGraphWorker`、`AdkWorker`、协议和插件加载方式不得改变执行路径。
- 原生 Harness 仅在用户显式选择 `NativeAgentWorker` 或 `AgentServer` 时启用。

### R3. Model Layer

- 核心定义供应商中立的 model request/response/stream/tool-call/usage/error 协议。
- 核心提供确定性的 `FakeModel` / `ScriptedModel`。
- MVP 官方提供完整的 OpenAI-compatible provider；具体 SDK 不进入核心类型系统。
- 流式文本、reasoning、结构化输出、并行 tool calls、usage 与 provider metadata 使用统一事件模型表达。

### R4. Tool and Human Approval

- MVP 支持 Python `FunctionTool`、本地/远程 `AgentTool`、`Handoff` 和可选 `MCPToolset`。
- 工具定义与 `ToolExecutor` 分离，支持进程内、sandbox 与远程执行策略。
- 工具调用具有稳定 `tool_call_id`、幂等键、timeout、retry、并发策略、风险与副作用声明。
- 高风险工具可触发 durable approval interrupt，并在批准、修改或拒绝后恢复。
- 工具输入、结果、错误与审批决策必须写入 durable run 记录。
- 不得把进程内 Python callable 描述为天然 sandboxed。

### R5. Graph and Multi-Agent MVP

- Graph API 支持 typed state/reducer、同步与异步 node、START/END、普通与条件 edge、fan-out/fan-in、parallel superstep、subgraph、interrupt/resume、retry/timeout/fallback、streaming、checkpoint/replay/fork、取消与运行预算。
- 高层提供 `SupervisorTeam`、`HandoffTeam`、`ParallelTeam` 与 `WorkflowTeam`。
- 所有 Team 都是 graph/compiler，不拥有特殊 runtime。
- 运行预算至少支持 step、递归、token、cost 与 wall-time 上限。

### R6. Durable State

- append-only `RunEventStore` 是 run 事实源；`CheckpointStore` 保存恢复快照。
- conversation/history 是运行事件的模型输入投影，不是编排状态事实源。
- artifact/file store 保存大对象；事件与 checkpoint 保存引用和校验信息。
- run 绑定 plan、agent catalog 与 state schema 版本，禁止不兼容定义静默恢复。
- 存储协议可插拔；MVP 提供 `InMemoryRunStore` 与 `RedisRunStore`。

### R7. Distributed Execution

- 每个 run 由一个可迁移、带租约的逻辑 `RunCoordinator` 推进。
- 分布式 `StepWorker` 执行 node，但不能直接推进全局 run。
- step 使用稳定 ID、attempt、lease 与 fencing token；旧租约的迟到结果被拒绝。
- 同一 superstep 的 state writes 通过确定性 reducer 合并。
- 框架保证 exactly-once state transition；外部副作用依赖幂等工具实现，不承诺 exactly-once。
- Coordinator 崩溃后可从事件与 checkpoint 在其他实例恢复。

### R8. Deployment

- `NativeAgentWorker` 提供嵌入式开发与中小规模部署。
- `AgentServer` 提供可独立扩展的 Coordinator 与通用 Step Worker。
- 两种模式共享 `AgentSpec`、`ExecutionPlan`、事件、checkpoint 与执行协议，Agent 代码无需修改即可迁移。
- 本地模式允许内存存储；跨进程或需要恢复的运行必须使用 durable store。
- MVP CLI 覆盖启动、注册、运行、查看状态、取消与恢复。

### R9. Observability and Safety

- model、tool、agent、handoff、node、checkpoint、interrupt 与 coordinator transition 都产生统一 span/event。
- 敏感输入输出的记录必须可配置关闭或脱敏。
- 取消、预算耗尽、重试终止、审批拒绝与恢复失败使用明确的结构化终态。
- 状态序列化默认使用安全、受控格式；不得默认反序列化任意 pickle。

## Out of Scope

- 群聊广播、共识投票、黑板系统和拍卖式任务分配。
- 可视化 graph/team 编辑器和完整管理控制台。
- 首版原生支持 Anthropic、Gemini 等多个 provider；它们通过后续 provider 包增加。
- 对任意外部 API 提供 exactly-once side effect。
- 强制现有外部 Agent 或 Worker 迁移到原生框架。
- 用 LangGraph、ADK 或 LangChain 作为原生 runtime 的内部依赖。

## Acceptance Criteria

- [x] 开发者可用一个 `Agent`、OpenAI-compatible model 和 Python tool 启动可流式运行的原生 Agent。
- [x] 同一 Agent 无代码修改即可在嵌入式 `NativeAgentWorker` 与独立 `AgentServer` 中运行。
- [x] `SupervisorTeam`、`HandoffTeam`、`ParallelTeam` 和 `WorkflowTeam` 均编译为可检查的 `ExecutionPlan`。
- [x] fan-out 节点可跨 Step Worker 并行运行，并以确定性 reducer 汇聚。
- [x] Coordinator 在 step 完成、提交前崩溃后可恢复，且不会重复推进状态。
- [x] tool、remote agent 与 human approval 均可 interrupt、checkpoint、resume。
- [x] checkpoint/replay/fork 可观察，且 run 绑定的 plan/schema 版本可审计。
- [x] FakeModel 能无网络验证模型循环、并行工具、handoff 与多 Agent 编排。
- [x] Redis Streams 重复投递和迟到 step result 不会造成双重状态提交。
- [x] 旧 `GatewayWorker`、LangGraph、ADK 和 `AgentConfig` 集成测试保持通过。
- [x] 核心包不新增具体模型 SDK 或 MCP SDK 的硬依赖。
- [x] 模型、工具、Agent、node 与协调器操作出现在统一 trace/event 层级中。

## Risks and Deferred Items

- 完整 MVP 范围较大，应按可独立验收的纵向里程碑交付，不能一次性大爆炸实现。
- durable replay 要求 node/task 明确区分确定性控制逻辑与非确定性副作用。
- graph/agent schema 演进必须建立兼容策略，否则长期挂起 run 会阻塞升级。
- Redis 事务、Lua/Functions 与 Cluster key slot 约束需在实现前做专项原型。
- MCP session 生命周期、流式返回和取消语义需要 provider 层专项测试。
- 安全 sandbox 是执行策略，不应与工具 schema 或 Agent API 混为一层。
