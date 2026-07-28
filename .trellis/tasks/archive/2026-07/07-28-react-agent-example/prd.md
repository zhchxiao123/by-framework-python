# ReAct Agent Example

## Goal

提供一个可直接运行的原生 ReAct（Reason + Act）天气助手示例，让开发者看懂如何定义 Agent、注册 Python 工具、让模型发起工具调用、把工具结果返回模型并得到最终答案。

## Confirmed Facts

- 原生 `Agent` 会编译成包含 model/tool loop 的 `ExecutionPlan`（`src/by_framework/agent/definition.py`）。
- `Runner.run_streamed()` 会持续产生文本、工具、usage 与终态事件，并自动执行低风险 `FunctionTool`（`src/by_framework/agent/execution.py`）。
- `ScriptedModel` 可离线脚本化多个模型回合并记录请求，适合无需 API Key 的确定性示例（`src/by_framework/agent/model.py`）。
- `FunctionTool` 从 Python 类型标注生成并验证输入/输出 schema（`src/by_framework/agent/tools.py`）。

## Requirements

- 在 `examples/native_agent/` 增加一个天气 ReAct 示例。
- 示例默认完全离线运行，不需要 Redis、网络或 API Key。
- 第一轮模型输出 `get_weather` tool call，Runner 执行工具；第二轮模型读取 `ToolMessage` 并输出最终答案。
- 示例打印关键流式事件和最终结果，代码中解释 ReAct 循环的对应位置。
- 增加自动化测试，验证工具调用、第二轮模型输入、最终答案与流式事件。
- README 说明运行命令，并给出替换 OpenAI-compatible provider 的位置，不复制 provider 实现。

## Out of Scope

- 前端 React 示例。
- 真实天气 API。
- Redis 分布式运行、AgentServer 或多 Agent Team。
- 真实 OpenAI-compatible 网络请求和密钥配置。

## Acceptance Criteria

- [x] `uv run python examples/native_agent/react_weather_agent.py` 成功退出并展示工具调用及最终回答。
- [x] 示例包含至少两个模型回合，第二轮请求中包含对应 `ToolMessage`。
- [x] 示例测试无需网络、Redis 或环境变量即可通过。
- [x] 示例只使用公开 `by_framework.agent` API。
- [x] 现有 Agent 测试保持通过。

## Risks and Deferred Items

- `ScriptedModel` 用于教学确定性，不代表真实模型会每次选择相同工具。
- OpenAI-compatible provider 的真实 transport 仍由应用注入；本示例只标明替换点。
