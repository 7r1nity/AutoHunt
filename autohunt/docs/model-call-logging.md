# 模型调用与规划修复日志

## 内容与关联

有运行上下文的 `LLMManager.generate` 和原生工具调用记录：

- `model_request`：call_id、provider、请求型号、operation、实际送入provider的请求、长度和指纹。请求是外部模型投影后的副本，不含认证头/配置密钥，不改变原内部状态。
- `model_response`：同一call_id、请求型号与response_model（服务端返回型号）、response_id、未经裁剪的文本与原生工具JSON、状态、耗时和服务端提供的usage。流式usage未提供时保存空对象，不猜测费用。内部推理内容不纳入诊断记录。
- `plan_parse_error`、`plan_repair`：attempt、错误类型/契约问题、call_id与response_id。首次和修复后输出都保留。
- `plan_draft`：解析后的完整模型草稿，包括revision、步骤参数、问题与目标；它不是应用合并/归一化后的执行计划。最终计划另见已有plan_state。
- `plan_candidate_graph`、`plan_validation_result`：关联同次响应的图和失败判断。每次新的模型请求用新call_id；HTTP传输重试仍由既有model_retry记录，不把一次generate伪称为一次物理请求。

这些记录只用于诊断，不生成执行授权，不自动重放失败请求。未绑定RunContext的后台调用不能伪造归属到某个会话；本轮主要验证前台规划与工具循环的记录。

## 保存与隐私

真实运行内容保存在 `agent/data/runtime.db` 的 `agent_run_events`（以配置的数据目录为准），完整事件使用现有AUTOHUNT_DATA_KEY加密。内部读取events/all_events保持原文，授权摘要不走新增脱敏。

普通 `/api/v1/runs/{id}/steps` 和事件流只给摘要：不返回完整请求、响应文本或draft；它们仍可显示调用/响应ID、模型、长度、usage、耗时和错误类型。需要核查原文时，在受控本地环境读取并解密相应run的事件，不把数据库或解密内容上传GitHub。没有运行加密时，仅保存摘要，并明确记录 `details_omitted=runtime_encryption_not_configured`。

## 单次真实合成复测（2026-10-14）

仅发送虚构请求“移除第三方或者外包”、虚构旧错误计划、虚构岗位快照与工具结构。执行模型物理请求1次，无传输/修复追加请求，没有调用任何招聘工具。

- 请求型号：`deepseek-v4-flash`；服务端返回型号：`deepseek-flash`。
- call_id：`108840aef2994e7a8ecb612896776ed5`。
- response_id：`362c9983-52d7-4be4-a0f0-f9270e5574dd`。
- 模型首次返回有效JSON；取消old-refilter/old-delivery，新增job_remove_selection，剩余投递依赖移除。校验和修订通过，没有保留活动refilter。
- 延迟约3.875秒；输入20,283、输出294 tokens，服务端总用量20,577。未换算费用。
- 验证范围仅规划和记录：没有执行描述目标匹配、移除、沟通或投递，不能当作完整端到端成功率。

本地原始合成事件：`evaluation/results/model-trace-live/20261013T171759.963597Z/events.jsonl`。同目录summary.json记录断言和归一化后的计划，physical-calls.jsonl记录实际外部请求/响应（仅合成数据，无认证头）。这些输出被Git忽略。启动器首次因测试构造缺参在发送请求之前失败，修正后才产生上述唯一一次物理请求。

## 测试

从 `autohunt/agent` 运行：

```powershell
# 离线：10项日志、隐私、格式修复、语义修复、取消与假传输验证。
.venv/Scripts/python.exe -m evaluation.model_trace_contract

# 仅有新的明确授权时使用；固定最多1次物理请求，不执行生成的工具。
.venv/Scripts/python.exe -m evaluation.model_trace_contract --models-consented
```

本轮离线10项、286项规划/校验授权回归、18项搜索来源回归通过。脚本模型与假传输覆盖全部修复/失败记录；唯一真实调用没有失败，因此不能声称实际供应商每种异常都已测过。
