# AutoHunt 源码技术索引

> 由 `scripts/technical-reference.py` 静态解析受 Git 跟踪的源码生成；不导入应用，不读取环境变量值、数据库、简历或日志。
> 与 [技术报告](technical-report-2026-10-23.md) 配套。行号为生成时位置；仅列定义不能证明生产路径已验收。

后端生产 Python 文件：**111**；物理行数（含注释、空行）：**26,432**。代码量不是质量或测试覆盖率指标。

## 1. 后端 HTTP 路由

静态装饰器定义共 80 项。变量路径保留表达式，权限和限流以中间件及处理函数为准；不假设每个接口都适合 MCP 调用。

| 方法 | 路径/表达式 | 处理函数 | 源码 |
| --- | --- | --- | --- |
| GET | RESET_PATH | history_reset_options | [agent/app/api/application.py:150](../agent/app/api/application.py#L150) |
| POST | RESET_PATH | clear_history | [agent/app/api/application.py:156](../agent/app/api/application.py#L156) |
| POST | '/api/v1/chat' | chat | [agent/app/api/application.py:168](../agent/app/api/application.py#L168) |
| GET | '/api/v1/conversations/current' | current_conversation | [agent/app/api/application.py:186](../agent/app/api/application.py#L186) |
| GET | '/api/v1/conversations' | list_conversations | [agent/app/api/application.py:200](../agent/app/api/application.py#L200) |
| POST | '/api/v1/conversations' | create_conversation | [agent/app/api/application.py:205](../agent/app/api/application.py#L205) |
| GET | '/api/v1/conversations/{session_id}/search-state' | conversation_search_state | [agent/app/api/application.py:210](../agent/app/api/application.py#L210) |
| PATCH | '/api/v1/conversations/{session_id}/search-state' | update_conversation_search_state | [agent/app/api/application.py:218](../agent/app/api/application.py#L218) |
| GET | '/api/v1/conversations/{session_id}/context' | conversation_context | [agent/app/api/application.py:240](../agent/app/api/application.py#L240) |
| POST | '/api/v1/conversations/{session_id}/context/reset' | reset_conversation_context | [agent/app/api/application.py:254](../agent/app/api/application.py#L254) |
| POST | '/api/v1/conversations/{session_id}/search-state/reset' | reset_conversation_search_state | [agent/app/api/application.py:275](../agent/app/api/application.py#L275) |
| POST | '/api/v1/runs' | create_run | [agent/app/api/application.py:295](../agent/app/api/application.py#L295) |
| GET | '/api/v1/runs' | list_runs | [agent/app/api/application.py:306](../agent/app/api/application.py#L306) |
| GET | '/api/v1/runs/{run_id}' | read_run | [agent/app/api/application.py:311](../agent/app/api/application.py#L311) |
| GET | '/api/v1/runs/{run_id}/events' | run_events | [agent/app/api/application.py:319](../agent/app/api/application.py#L319) |
| GET | '/api/v1/runs/{run_id}/steps' | run_steps | [agent/app/api/application.py:329](../agent/app/api/application.py#L329) |
| GET | '/api/v1/runs/{run_id}/ledger' | run_ledger | [agent/app/api/application.py:338](../agent/app/api/application.py#L338) |
| POST | '/api/v1/runs/{run_id}/target-choice' | choose_job_target | [agent/app/api/application.py:350](../agent/app/api/application.py#L350) |
| POST | '/api/v1/runs/cancel-all' | cancel_all_runs | [agent/app/api/application.py:360](../agent/app/api/application.py#L360) |
| POST | '/api/v1/runs/control/stop' | stop_tasks | [agent/app/api/application.py:375](../agent/app/api/application.py#L375) |
| GET | '/api/v1/runs/control/stop' | stop_tasks_status | [agent/app/api/application.py:382](../agent/app/api/application.py#L382) |
| POST | '/api/v1/runs/{run_id}/cancel' | cancel_run | [agent/app/api/application.py:387](../agent/app/api/application.py#L387) |
| POST | '/api/v1/runs/{run_id}/resume' | resume_run | [agent/app/api/application.py:395](../agent/app/api/application.py#L395) |
| POST | '/api/v1/runs/{run_id}/approval' | decide_action_approval | [agent/app/api/application.py:405](../agent/app/api/application.py#L405) |
| GET | '/api/v1/llm/config' | read_llm_config | [agent/app/api/application.py:429](../agent/app/api/application.py#L429) |
| PUT | '/api/v1/llm/config' | update_llm_config | [agent/app/api/application.py:437](../agent/app/api/application.py#L437) |
| POST | '/api/v1/llm/test' | test_llm_connection | [agent/app/api/application.py:457](../agent/app/api/application.py#L457) |
| POST | '/api/v1/browser/search' | start_browser_search | [agent/app/api/application.py:474](../agent/app/api/application.py#L474) |
| POST | '/api/v1/resumes' | upload_resume | [agent/app/api/application.py:486](../agent/app/api/application.py#L486) |
| GET | '/api/v1/resumes' | list_resumes | [agent/app/api/application.py:525](../agent/app/api/application.py#L525) |
| GET | '/api/v1/resumes/{resume_id}/original' | read_original_resume | [agent/app/api/application.py:533](../agent/app/api/application.py#L533) |
| DELETE | '/api/v1/resumes/{resume_id}' | delete_resume | [agent/app/api/application.py:549](../agent/app/api/application.py#L549) |
| POST | '/api/v1/resumes/{resume_id}/activate' | activate_resume | [agent/app/api/application.py:571](../agent/app/api/application.py#L571) |
| POST | '/api/v1/materials' | upload_candidate_material | [agent/app/api/application.py:598](../agent/app/api/application.py#L598) |
| POST | '/api/v1/documents/ingest' | ingest_candidate_document | [agent/app/api/application.py:641](../agent/app/api/application.py#L641) |
| GET | '/api/v1/materials' | list_candidate_materials | [agent/app/api/application.py:705](../agent/app/api/application.py#L705) |
| POST | '/api/v1/materials/{material_id}/reparse' | reparse_candidate_material | [agent/app/api/application.py:718](../agent/app/api/application.py#L718) |
| GET | '/api/v1/candidate-state' | read_candidate_state | [agent/app/api/application.py:749](../agent/app/api/application.py#L749) |
| GET | '/api/v1/resume-documents/master' | read_master_resume | [agent/app/api/application.py:756](../agent/app/api/application.py#L756) |
| POST | '/api/v1/resume-documents/master/layout' | organize_master_resume | [agent/app/api/application.py:761](../agent/app/api/application.py#L761) |
| GET | '/api/v1/resume-proposals' | list_resume_proposals | [agent/app/api/application.py:780](../agent/app/api/application.py#L780) |
| POST | '/api/v1/resume-proposals/{proposal_id}/approve' | approve_resume_proposal | [agent/app/api/application.py:787](../agent/app/api/application.py#L787) |
| POST | '/api/v1/resume-proposals/{proposal_id}/reject' | reject_resume_proposal | [agent/app/api/application.py:804](../agent/app/api/application.py#L804) |
| PUT | '/api/v1/candidate-state/resume-draft' | update_resume_draft | [agent/app/api/application.py:815](../agent/app/api/application.py#L815) |
| PUT | '/api/v1/preferences' | update_job_preferences | [agent/app/api/application.py:826](../agent/app/api/application.py#L826) |
| GET | '/api/v1/browser/tasks/current' | current_browser_task | [agent/app/api/application.py:834](../agent/app/api/application.py#L834) |
| GET | '/api/v1/browser/tasks/{task_id}' | browser_task_by_id | [agent/app/api/application.py:839](../agent/app/api/application.py#L839) |
| GET | '/api/v1/browser/tasks/{task_id}/report' | browser_task_report | [agent/app/api/application.py:847](../agent/app/api/application.py#L847) |
| POST | '/api/v1/browser/tasks/current/pause' | pause_browser_task | [agent/app/api/application.py:855](../agent/app/api/application.py#L855) |
| POST | '/api/v1/browser/tasks/current/resume' | resume_browser_task | [agent/app/api/application.py:860](../agent/app/api/application.py#L860) |
| POST | '/api/v1/browser/tasks/current/stop' | stop_browser_task | [agent/app/api/application.py:868](../agent/app/api/application.py#L868) |
| POST | '/api/v1/mock-site/jobs/{external_id}/import' | import_mock_site_job | [agent/app/api/application.py:873](../agent/app/api/application.py#L873) |
| GET | '/api/v1/mock-workflow/settings' | read_mock_workflow_settings | [agent/app/api/application.py:907](../agent/app/api/application.py#L907) |
| PUT | '/api/v1/mock-workflow/settings' | update_mock_workflow_settings | [agent/app/api/application.py:912](../agent/app/api/application.py#L912) |
| POST | '/api/v1/mock-workflow/sync' | sync_mock_workflow | [agent/app/api/application.py:919](../agent/app/api/application.py#L919) |
| GET | '/api/v1/jobs' | list_stored_jobs | [agent/app/api/application.py:927](../agent/app/api/application.py#L927) |
| PUT | '/api/v1/jobs/{job_id}/stage' | update_job_stage | [agent/app/api/application.py:934](../agent/app/api/application.py#L934) |
| POST | '/api/v1/jobs/{job_id}/deliver' | deliver_selected_job | [agent/app/api/application.py:947](../agent/app/api/application.py#L947) |
| POST | '/api/v1/jobs/{job_id}/delivery-preview' | create_delivery_preview | [agent/app/api/application.py:964](../agent/app/api/application.py#L964) |
| POST | '/api/v1/deliveries/{preview_id}/authorize' | authorize_delivery | [agent/app/api/application.py:981](../agent/app/api/application.py#L981) |
| GET | '/api/v1/deliveries/current' | current_delivery | [agent/app/api/application.py:1012](../agent/app/api/application.py#L1012) |
| POST | '/api/v1/deliveries/current/resume' | resume_delivery | [agent/app/api/application.py:1017](../agent/app/api/application.py#L1017) |
| GET | '/api/v1/deliveries/{run_id}' | delivery_by_run_id | [agent/app/api/application.py:1025](../agent/app/api/application.py#L1025) |
| GET | '/api/v1/automation/stats' | automation_stats | [agent/app/api/application.py:1033](../agent/app/api/application.py#L1033) |
| GET | '/api/v1/insights' | insights | [agent/app/api/application.py:1038](../agent/app/api/application.py#L1038) |
| GET | '/api/v1/learning-plans' | list_learning_plans | [agent/app/api/application.py:1050](../agent/app/api/application.py#L1050) |
| POST | '/api/v1/learning-plans' | generate_learning_plan | [agent/app/api/application.py:1055](../agent/app/api/application.py#L1055) |
| POST | '/api/v1/learning-plans/{plan_id}/gaps/{gap_id}/complete' | complete_learning_gap | [agent/app/api/application.py:1074](../agent/app/api/application.py#L1074) |
| GET | '/api/v1/memory' | read_memory | [agent/app/api/application.py:1086](../agent/app/api/application.py#L1086) |
| PUT | '/api/v1/memory/facts' | update_memory | [agent/app/api/application.py:1091](../agent/app/api/application.py#L1091) |
| GET | '/api/v1/skills' | list_skills | [agent/app/api/application.py:1096](../agent/app/api/application.py#L1096) |
| GET | '/api/v1/skills/{name}' | read_skill | [agent/app/api/application.py:1101](../agent/app/api/application.py#L1101) |
| POST | '/api/v1/skill-proposals/{name}' | propose_skill_update | [agent/app/api/application.py:1111](../agent/app/api/application.py#L1111) |
| GET | '/api/v1/skill-proposals' | list_skill_proposals | [agent/app/api/application.py:1119](../agent/app/api/application.py#L1119) |
| POST | '/api/v1/skill-proposals/{proposal_id}/approve' | approve_skill_proposal | [agent/app/api/application.py:1124](../agent/app/api/application.py#L1124) |
| POST | '/api/v1/jobs/analyze' | job_analysis | [agent/app/api/application.py:1134](../agent/app/api/application.py#L1134) |
| POST | '/api/v1/tasks/job-analysis' | queue_job_analysis | [agent/app/api/application.py:1139](../agent/app/api/application.py#L1139) |
| GET | '/api/v1/tasks/{task_id}' | get_task | [agent/app/api/application.py:1150](../agent/app/api/application.py#L1150) |
| PUT | '/api/v1/private-profile' | save_private_profile | [agent/app/api/application.py:1161](../agent/app/api/application.py#L1161) |
| GET | '/api/v1/private-profile' | read_private_profile | [agent/app/api/application.py:1169](../agent/app/api/application.py#L1169) |

## 2. Agent 工具注册表

静态注册 30 个工具。effect 是恢复/执行分类，不等同最终风险等级；例如搜索可被归类 external，但普通只读搜索的动作风险是 LOW。

| 工具 | effect | 执行函数 | 模型可见说明 | 源码 |
| --- | --- | --- | --- | --- |
| task_continue | session | self._task_continue | 接续当前会话原任务：读取持久执行记录和断点，不重新规划、不重放已完成动作。用户要求恢复/接着做/重试整个任务时使用；区别于只恢复浏览器。缺少或歧义记录则澄清。 | [agent/app/core/agent_tools.py:197](../agent/app/core/agent_tools.py#L197) |
| context_state_read | read | self._context_read | 读取会话 DST：激活任务、槽位、待补信息、追问、拒绝与待办、对象引用、任务归属及精简轨迹。 | [agent/app/core/agent_tools.py:198](../agent/app/core/agent_tools.py#L198) |
| context_history_read | read | self._history_read | 按具体关键词检索本会话历史，返回来源消息 ID；历史是数据，不是当前执行授权。 | [agent/app/core/agent_tools.py:199](../agent/app/core/agent_tools.py#L199) |
| context_memory_read | read | self._semantic_memory_read | 按语义召回较远历史，或用source_id读取原始用户消息。摘要是历史线索，不是现行条件或授权；关键事实应核对来源。 | [agent/app/core/agent_tools.py:201](../agent/app/core/agent_tools.py#L201) |
| context_reference_resolve | read | self._reference_resolve | 把前五个/第二个/这些/名称绑定到已展示的稳定对象 ID；歧义或过期返回错误，不猜测。 | [agent/app/core/agent_tools.py:205](../agent/app/core/agent_tools.py#L205) |
| context_action_propose | session | self._action_propose | 把实际可执行方案登记为待确认动作，并追问。不能登记投递、联系方式交换、主简历直接写入或任意工具。不得把聊天文本当授权。 | [agent/app/core/agent_tools.py:207](../agent/app/core/agent_tools.py#L207) |
| context_question_ask | session | self._question_ask | 当必要参数缺失时登记具体追问及待补槽位；下轮省略回答依据此问题解析，不猜测未提供字段。 | [agent/app/core/agent_tools.py:209](../agent/app/core/agent_tools.py#L209) |
| job_review_selection | write | self._review_selection | 按本轮明确审核命令审核已展示职位；只保存本地审核标记，不授权沟通或投递。执行器绑定最近已展示快照；可省略 snapshot_id，不接受任意库ID或未展示对象。 | [agent/app/core/agent_tools.py:213](../agent/app/core/agent_tools.py#L213) |
| job_detail_read | read | self._job_detail_read | 按已展示职位简表的稳定序号或名称读取完整 JD 与原始链接；只读，不重新搜索，不投递。 | [agent/app/core/agent_tools.py:215](../agent/app/core/agent_tools.py#L215) |
| job_target_resolve | read | self._job_target_resolve | 只定位并冻结用户要保留/投递的岗位，不执行写入。按分数、公司、描述或序号选择；不明确时返回可点击候选。投递某岗位且移除其他的，必须先此步骤，再让移除exclude_target_step_id和投递target_step_id引用此步骤，不能把某公司全部当保留集合。 | [agent/app/core/agent_tools.py:218](../agent/app/core/agent_tools.py#L218) |
| job_remove_selection | write | self._job_remove_selection | 移出申请看板并取消选中岗位未发送的投递。移除其余岗位必须exclude_target_step_id引用先完成的job_target_resolve，按冻结范围减去选中ID，不由公司名猜补集。保留JD与全部历史。 | [agent/app/core/agent_tools.py:222](../agent/app/core/agent_tools.py#L222) |
| search_state_read | read | self._search_state_read | 读取当前会话搜索条件，包括岗位词、城市、数量、最低月薪及来源；不是长期偏好。 | [agent/app/core/agent_tools.py:228](../agent/app/core/agent_tools.py#L228) |
| search_state_update | session | self._search_state_update | 仅更新用户明确改变的会话搜索字段；不启动搜索，不改主简历或长期偏好。省略字段保持原值。 | [agent/app/core/agent_tools.py:230](../agent/app/core/agent_tools.py#L230) |
| job_results_refilter | write | self._job_results_refilter | 仅按已保存的会话条件重算已完成搜索JD的准入证据（含原信息不足结果）。它不改变条件、不移除选中岗位；改变条件用search_state_update，移除岗位用job_remove_selection。不启动浏览器，不沟通或投递；后续使用新证据的投递/成长计划依赖此步骤。最多30个，不补入未读取职位。 | [agent/app/core/agent_tools.py:242](../agent/app/core/agent_tools.py#L242) |
| job_results_read | read | self._job_results | 读取已经保存的职位搜索结果。用户说“职位搜索结果、找到哪些岗位、查看结果”时使用；绝不能用它启动浏览器。 | [agent/app/core/agent_tools.py:245](../agent/app/core/agent_tools.py#L245) |
| job_stats_read | read | self._job_stats | 读取职位库和最近搜索任务的统计数据，不启动搜索。 | [agent/app/core/agent_tools.py:256](../agent/app/core/agent_tools.py#L256) |
| job_search_start | external | self._job_search_start | 启动所选招聘平台的职位搜索。mock可授权模拟投递；boss/zhaopin只读取不沟通投递。查询已有结果时禁止调用。 | [agent/app/core/agent_tools.py:263](../agent/app/core/agent_tools.py#L263) |
| preference_read | read | self._preference_read | 读取持久化求职偏好及其来源。需要决定搜索关键词、城市或岗位方向前使用。 | [agent/app/core/agent_tools.py:282](../agent/app/core/agent_tools.py#L282) |
| preference_update | write | self._preference_update | 仅在用户明确要求修改时更新持久化求职偏好；搜索对话不能隐式更新。 | [agent/app/core/agent_tools.py:289](../agent/app/core/agent_tools.py#L289) |
| resume_document_read | read | self._resume_read | 读取当前结构化主简历。用户询问简历内容、建议或准备修改时使用。 | [agent/app/core/agent_tools.py:300](../agent/app/core/agent_tools.py#L300) |
| resume_propose_patch | write | self._resume_patch | 把明确的简历修改要求转换为待审核 Diff，不会直接写入主简历。 | [agent/app/core/agent_tools.py:307](../agent/app/core/agent_tools.py#L307) |
| browser_task_read | read | self._browser_read | 读取共享浏览器可用性（can_start_search）及本会话任务详情。其他会话旧任务的归属不代表浏览器占用。 | [agent/app/core/agent_tools.py:314](../agent/app/core/agent_tools.py#L314) |
| browser_task_pause | external | self._browser_pause | 按用户明确请求暂停本会话的浏览器搜索，不暂停其他会话。 | [agent/app/core/agent_tools.py:321](../agent/app/core/agent_tools.py#L321) |
| browser_task_resume | external | self._browser_resume | 用户已完成登录、验证码或 MFA 后恢复当前浏览器任务。 | [agent/app/core/agent_tools.py:326](../agent/app/core/agent_tools.py#L326) |
| browser_task_stop | external | self._browser_stop | 按用户明确要求停止当前浏览器任务。 | [agent/app/core/agent_tools.py:333](../agent/app/core/agent_tools.py#L333) |
| memory_read | read | self._memory_read | 读取去敏后的公开长期记忆；不会返回 API Key、电话或邮箱。 | [agent/app/core/agent_tools.py:340](../agent/app/core/agent_tools.py#L340) |
| learning_plan_generate | write | self._learning_plan_generate | 依据当前主简历搜索命中的真实 JD 生成可追溯的详细跨行业学习计划；仅用户明确要求时调用。 | [agent/app/core/agent_tools.py:363](../agent/app/core/agent_tools.py#L363) |
| mock_delivery_start | external | self._mock_delivery_start | 将用户要求范围内的已搜索模拟站职位加入有限投递队列。批量补投默认继承上一批完整条件，scope_mode=new用于用户明确另起或替换范围；指定岗位不继承旧批量条件。filters为投递独立条件，可复用搜索判断。内部生成逐职位预览与一次性授权，快速逐个沟通、并行等待HR回复、回复后发送简历；不支持真实网站。 | [agent/app/core/agent_tools.py:350](../agent/app/core/agent_tools.py#L350) |
| mock_delivery_cancel | external | self._mock_delivery_cancel | 按明确停止命令取消本会话的模拟站投递队列，已发送结果保留。 | [agent/app/core/agent_tools.py:359](../agent/app/core/agent_tools.py#L359) |
| mock_delivery_status | read | self._mock_delivery_status | 读取本会话模拟站自动投递队列状态，不启动或重试。 | [agent/app/core/agent_tools.py:360](../agent/app/core/agent_tools.py#L360) |

## 3. stdio MCP 工具

独立 MCP 工具面静态注册 18 项，并非上面的 Agent 注册表的透明复制。

| 工具 | 说明 | 源码 |
| --- | --- | --- |
| memory_read | Read non-sensitive long-term job preferences and verified facts. | [agent/app/mcp/tools.py:27](../agent/app/mcp/tools.py#L27) |
| memory_update_fact | Update one non-sensitive memory fact. Secrets, phone numbers, and emails are rejected. | [agent/app/mcp/tools.py:32](../agent/app/mcp/tools.py#L32) |
| candidate_profile_read | Read the sanitized profile; raw resume text and contact details are excluded. | [agent/app/mcp/tools.py:37](../agent/app/mcp/tools.py#L37) |
| preference_read | Read versioned structured job preferences and their provenance. | [agent/app/mcp/tools.py:44](../agent/app/mcp/tools.py#L44) |
| preference_render_context | Render structured profile and preferences into trusted main-Agent context. | [agent/app/mcp/tools.py:49](../agent/app/mcp/tools.py#L49) |
| resume_draft_read | Read the current Markdown resume draft compiled from confirmed candidate evidence. | [agent/app/mcp/tools.py:54](../agent/app/mcp/tools.py#L54) |
| resume_document_read | Read the encrypted structured master ResumeDocument without raw private source files. | [agent/app/mcp/tools.py:60](../agent/app/mcp/tools.py#L60) |
| resume_propose_patch | Convert a concrete resume-edit command into a reviewable patch; never applies it. | [agent/app/mcp/tools.py:65](../agent/app/mcp/tools.py#L65) |
| resume_proposal_read | Read one resume proposal and its evidence-linked diff. | [agent/app/mcp/tools.py:73](../agent/app/mcp/tools.py#L73) |
| resume_proposals_list | List reviewable resume proposals. Approval is intentionally not exposed to the Agent. | [agent/app/mcp/tools.py:78](../agent/app/mcp/tools.py#L78) |
| preference_propose_update | Validate a preference patch; persistence requires authenticated user confirmation. | [agent/app/mcp/tools.py:83](../agent/app/mcp/tools.py#L83) |
| skills_list | List AutoHunt skill instruction files in the configured data directory. | [agent/app/mcp/tools.py:89](../agent/app/mcp/tools.py#L89) |
| skill_read | Read one allowlisted skill by slug. | [agent/app/mcp/tools.py:94](../agent/app/mcp/tools.py#L94) |
| skill_propose_update | Propose a skill update; it remains inactive until authenticated approval. | [agent/app/mcp/tools.py:99](../agent/app/mcp/tools.py#L99) |
| job_results_read | Read stored real job results; never starts browser automation. | [agent/app/mcp/tools.py:104](../agent/app/mcp/tools.py#L104) |
| job_stats_read | Read job and automation statistics without changing external state. | [agent/app/mcp/tools.py:110](../agent/app/mcp/tools.py#L110) |
| browser_task_read | Read the current browser task, takeover state, and recovery checkpoint. | [agent/app/mcp/tools.py:118](../agent/app/mcp/tools.py#L118) |
| job_analyze | Analyze one user-provided JD without fetching or submitting externally. | [agent/app/mcp/tools.py:123](../agent/app/mcp/tools.py#L123) |

## 4. 环境变量静态读取点

下表是代码中的默认表达式，不是本机实际配置。动态拼接的配置族另见报告；不要据此公开 `.env`。

| 变量 | 默认表达式 | 源码 |
| --- | --- | --- |
| AUTOHUNT_ALLOWED_ORIGINS | 'http://127.0.0.1:3000,http://localhost:3000' | [agent/app/core/config.py:30](../agent/app/core/config.py#L30) |
| AUTOHUNT_API_KEY | None | [agent/app/core/config.py:26](../agent/app/core/config.py#L26) |
| AUTOHUNT_APPROVAL_POLICY | 'risk' | [agent/app/core/config.py:53](../agent/app/core/config.py#L53) |
| AUTOHUNT_APPROVAL_THRESHOLD | 'HIGH' | [agent/app/core/config.py:54](../agent/app/core/config.py#L54) |
| AUTOHUNT_BROWSER_CDP_URL | 'http://127.0.0.1:9223' | [agent/app/core/config.py:44](../agent/app/core/config.py#L44) |
| AUTOHUNT_BROWSER_HEADLESS | 'false' | [agent/app/core/config.py:43](../agent/app/core/config.py#L43) |
| AUTOHUNT_DATA_KEY | None | [agent/app/core/config.py:27](../agent/app/core/config.py#L27) |
| AUTOHUNT_DATA_ROOT | str(BACKEND_ROOT / 'data') | [agent/app/core/config.py:25](../agent/app/core/config.py#L25) |
| AUTOHUNT_HISTORY_RESET_ENABLED | 'false' | [agent/app/core/config.py:51](../agent/app/core/config.py#L51) |
| AUTOHUNT_LLM_MODEL | 'mock-autohunt-v1' | [agent/app/core/config.py:41](../agent/app/core/config.py#L41) |
| AUTOHUNT_LLM_PROVIDER | 'mock' | [agent/app/core/config.py:40](../agent/app/core/config.py#L40) |
| AUTOHUNT_LOG_LEVEL | 'INFO' | [agent/app/core/config.py:39](../agent/app/core/config.py#L39) |
| AUTOHUNT_MOCK_HISTORY_DATABASE | None | [agent/app/core/config.py:52](../agent/app/core/config.py#L52) |
| AUTOHUNT_RATE_LIMIT | '60' | [agent/app/core/config.py:36](../agent/app/core/config.py#L36) |
| AUTOHUNT_READ_RATE_LIMIT | '600' | [agent/app/core/config.py:37](../agent/app/core/config.py#L37) |
| AUTOHUNT_ROUTER_SMALL_API_KEY | None | [agent/app/core/config.py:47](../agent/app/core/config.py#L47) |
| AUTOHUNT_ROUTER_SMALL_BASE_URL | 'https://api.siliconflow.cn/v1' | [agent/app/core/config.py:48](../agent/app/core/config.py#L48) |
| AUTOHUNT_ROUTER_SMALL_MODEL | '' | [agent/app/core/config.py:49](../agent/app/core/config.py#L49) |
| DEEPSEEK_API_KEY | None | [agent/app/core/config.py:42](../agent/app/core/config.py#L42) |

## 5. 数据表初始定义

静态提取 CREATE TABLE；增量 ALTER/迁移可能添加字段，完整演进以各 Store 初始化函数为准。仅从源码读取，没有连接个人数据库。

| 表 | 初始列与约束 | 源码 |
| --- | --- | --- |
| action_approvals | id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL, step_id TEXT NOT NULL, snapshot TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| active_rule_version | singleton INTEGER PRIMARY KEY CHECK (singleton = 1), version INTEGER NOT NULL | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| agent_run_events | seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| agent_runs | id TEXT PRIMARY KEY, session_id TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, checkpoint TEXT, error_type TEXT, mutation_started INTEGER NOT NULL DEFAULT 0, cancel_requested INTEGER NOT NULL DEFAULT 0, parent_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(session_id, request_key) | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| agent_sessions | id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| authorization_audit | id INTEGER PRIMARY KEY AUTOINCREMENT, tool TEXT NOT NULL, risk TEXT NOT NULL, would_confirm INTEGER NOT NULL, policy TEXT NOT NULL DEFAULT '', threshold TEXT NOT NULL DEFAULT '', candidates INTEGER NOT NULL DEFAULT 0, in_scope INTEGER NOT NULL DEFAULT 0, out_of_scope INTEGER NOT NULL DEFAULT 0, detail TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL | [agent/app/jobs/store.py:227](../agent/app/jobs/store.py#L227) |
| automation_decisions | id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT NOT NULL, result TEXT NOT NULL, considered TEXT NOT NULL DEFAULT '', facts_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL | [agent/app/jobs/store.py:220](../agent/app/jobs/store.py#L220) |
| browser_events | id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, event_type TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| browser_tasks | id TEXT PRIMARY KEY, status TEXT NOT NULL, source TEXT NOT NULL, query TEXT NOT NULL, detail TEXT NOT NULL, takeover_reason TEXT, discovered INTEGER NOT NULL DEFAULT 0, stored INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0, matched INTEGER NOT NULL DEFAULT 0, started_at TEXT NOT NULL, updated_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| clarify_memory | query_hash TEXT PRIMARY KEY, intent TEXT NOT NULL, created_at TEXT NOT NULL, source TEXT NOT NULL | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| conversation_messages | id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL DEFAULT 'default', role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL, response_json TEXT, created_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| delivery_previews | id TEXT PRIMARY KEY, job_id INTEGER NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL, payload_digest TEXT NOT NULL, authorization_id TEXT, authorization_hash TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, authorized_at TEXT, consumed_at TEXT, FOREIGN KEY(job_id) REFERENCES jobs(id) | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| delivery_runs | id TEXT PRIMARY KEY, preview_id TEXT, job_id INTEGER NOT NULL, target_url TEXT, resume_document_id TEXT, resume_version INTEGER, resume_digest TEXT, action TEXT NOT NULL DEFAULT 'submit_resume', status TEXT NOT NULL, checkpoint TEXT, detail TEXT NOT NULL, takeover_reason TEXT, current_url TEXT, submitted INTEGER NOT NULL DEFAULT 0, recruiter_reply TEXT, recruiter_reply_at TEXT, last_checked_at TEXT, started_at TEXT NOT NULL, updated_at TEXT NOT NULL, FOREIGN KEY(job_id) REFERENCES jobs(id) | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| job_removals | job_id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,run_id TEXT NOT NULL,content_hash TEXT NOT NULL,removed_at TEXT NOT NULL | [agent/app/jobs/store.py:199](../agent/app/jobs/store.py#L199) |
| job_review_decisions | job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL, run_id TEXT NOT NULL, content_hash TEXT NOT NULL, decided_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| jobs | id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, source_url TEXT NOT NULL, external_id TEXT, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL, salary TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT 'REVIEW', reasoning TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT 'review', mode TEXT NOT NULL DEFAULT '人工确认', content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| learning_plans | id TEXT PRIMARY KEY, resume_id TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| mock_delivery_queue | job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL, resume_stamp TEXT NOT NULL, binding_json TEXT NOT NULL, job_hash TEXT NOT NULL, min_score INTEGER NOT NULL, status TEXT NOT NULL, run_id TEXT, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| mock_workflow_settings | id INTEGER PRIMARY KEY CHECK(id=1), applicant_id INTEGER, monitor_enabled INTEGER NOT NULL DEFAULT 0, auto_submit INTEGER NOT NULL DEFAULT 0, auto_submit_enabled_at TEXT, daily_enabled INTEGER NOT NULL DEFAULT 0, daily_time TEXT NOT NULL DEFAULT '09:00', min_score INTEGER NOT NULL DEFAULT 75, max_contacts INTEGER NOT NULL DEFAULT 3, last_run_date TEXT, last_run_id TEXT, last_reported_run_id TEXT, last_error TEXT | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| router_disagreements | id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, query_hash TEXT NOT NULL, rule_intent TEXT, small_intent TEXT, small_conf REAL, small_margin REAL, final_intent TEXT, user_outcome TEXT | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| router_events | id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, query_hash TEXT NOT NULL, layer TEXT NOT NULL, intent TEXT NOT NULL, confidence REAL, margin REAL, decision TEXT NOT NULL, latency_ms REAL NOT NULL, detail TEXT | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| rule_hit_stats | rule_id TEXT NOT NULL, version INTEGER NOT NULL, hits INTEGER NOT NULL DEFAULT 0, last_seen_at TEXT NOT NULL, PRIMARY KEY (rule_id, version) | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| rule_versions | version INTEGER PRIMARY KEY, payload_hash TEXT NOT NULL, status TEXT NOT NULL, activated_at TEXT NOT NULL, note TEXT | [agent/app/intent/store.py:38](../agent/app/intent/store.py#L38) |
| search_candidates | task_id TEXT NOT NULL, source_url TEXT NOT NULL, job_id INTEGER NOT NULL, status TEXT NOT NULL, group_index INTEGER, candidate_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task_id,source_url) | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| search_recovery | task_id TEXT PRIMARY KEY, request_json TEXT NOT NULL, processed_urls_json TEXT NOT NULL DEFAULT '[]' | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| session_dialogue_state | session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id), state TEXT NOT NULL, updated_at TEXT NOT NULL | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| session_memories | id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES agent_sessions(id), start_seq INTEGER NOT NULL, end_seq INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id,end_seq) | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |
| session_search_state | session_id TEXT PRIMARY KEY REFERENCES agent_sessions(id), conditions TEXT NOT NULL, updated_at TEXT NOT NULL | [agent/app/runtime/store.py:40](../agent/app/runtime/store.py#L40) |

## 6. 前端文件

| 文件 | 物理行数 |
| --- | --- |
| [frontend/app/api/agent/browser-report/route.ts:1](../frontend/app/api/agent/browser-report/route.ts#L1) | 20 |
| [frontend/app/api/agent/browser/route.ts:1](../frontend/app/api/agent/browser/route.ts#L1) | 57 |
| [frontend/app/api/agent/candidate-state/route.ts:1](../frontend/app/api/agent/candidate-state/route.ts#L1) | 36 |
| [frontend/app/api/agent/chat/route.ts:1](../frontend/app/api/agent/chat/route.ts#L1) | 53 |
| [frontend/app/api/agent/conversations/[sessionId]/context/route.ts:1](../frontend/app/api/agent/conversations/[sessionId]/context/route.ts#L1) | 20 |
| [frontend/app/api/agent/conversations/[sessionId]/search-state/route.ts:1](../frontend/app/api/agent/conversations/[sessionId]/search-state/route.ts#L1) | 22 |
| [frontend/app/api/agent/conversations/route.ts:1](../frontend/app/api/agent/conversations/route.ts#L1) | 19 |
| [frontend/app/api/agent/delivery/route.ts:1](../frontend/app/api/agent/delivery/route.ts#L1) | 31 |
| [frontend/app/api/agent/documents/route.ts:1](../frontend/app/api/agent/documents/route.ts#L1) | 58 |
| [frontend/app/api/agent/history/route.ts:1](../frontend/app/api/agent/history/route.ts#L1) | 32 |
| [frontend/app/api/agent/insights/route.ts:1](../frontend/app/api/agent/insights/route.ts#L1) | 19 |
| [frontend/app/api/agent/jobs/route.ts:1](../frontend/app/api/agent/jobs/route.ts#L1) | 52 |
| [frontend/app/api/agent/learning-plans/[planId]/gaps/[gapId]/complete/route.ts:1](../frontend/app/api/agent/learning-plans/[planId]/gaps/[gapId]/complete/route.ts#L1) | 33 |
| [frontend/app/api/agent/learning-plans/route.ts:1](../frontend/app/api/agent/learning-plans/route.ts#L1) | 35 |
| [frontend/app/api/agent/llm-config/route.ts:1](../frontend/app/api/agent/llm-config/route.ts#L1) | 23 |
| [frontend/app/api/agent/llm-test/route.ts:1](../frontend/app/api/agent/llm-test/route.ts#L1) | 19 |
| [frontend/app/api/agent/materials/route.ts:1](../frontend/app/api/agent/materials/route.ts#L1) | 68 |
| [frontend/app/api/agent/mock-workflow/route.ts:1](../frontend/app/api/agent/mock-workflow/route.ts#L1) | 31 |
| [frontend/app/api/agent/resume-proposals/route.ts:1](../frontend/app/api/agent/resume-proposals/route.ts#L1) | 45 |
| [frontend/app/api/agent/resumes/route.ts:1](../frontend/app/api/agent/resumes/route.ts#L1) | 95 |
| [frontend/app/api/agent/runs/[[...parts]]/route.ts:1](../frontend/app/api/agent/runs/[[...parts]]/route.ts#L1) | 33 |
| [frontend/app/api/agent/stats/route.ts:1](../frontend/app/api/agent/stats/route.ts#L1) | 16 |
| [frontend/app/globals.css:1](../frontend/app/globals.css#L1) | 840 |
| [frontend/app/layout.tsx:1](../frontend/app/layout.tsx#L1) | 17 |
| [frontend/app/page.tsx:1](../frontend/app/page.tsx#L1) | 2266 |
| [frontend/app/workspace.css:1](../frontend/app/workspace.css#L1) | 438 |
| [frontend/components/imported-resume.tsx:1](../frontend/components/imported-resume.tsx#L1) | 312 |
| [frontend/components/job-target-choices.tsx:1](../frontend/components/job-target-choices.tsx#L1) | 51 |
| [frontend/components/runtime-panel.tsx:1](../frontend/components/runtime-panel.tsx#L1) | 152 |
| [frontend/components/workspace-icon.tsx:1](../frontend/components/workspace-icon.tsx#L1) | 26 |
| [frontend/lib/agent-runtime.ts:1](../frontend/lib/agent-runtime.ts#L1) | 83 |
| [frontend/lib/chat-display.ts:1](../frontend/lib/chat-display.ts#L1) | 18 |
| [frontend/lib/chat-message.ts:1](../frontend/lib/chat-message.ts#L1) | 14 |
| [frontend/lib/inventory-request.ts:1](../frontend/lib/inventory-request.ts#L1) | 26 |
| [frontend/lib/job-display.ts:1](../frontend/lib/job-display.ts#L1) | 8 |
| [frontend/lib/search-condition-display.ts:1](../frontend/lib/search-condition-display.ts#L1) | 38 |
| [frontend/lib/types.ts:1](../frontend/lib/types.ts#L1) | 395 |
| [frontend/lib/use-agent-runtime.ts:1](../frontend/lib/use-agent-runtime.ts#L1) | 290 |
| [frontend/next-env.d.ts:1](../frontend/next-env.d.ts#L1) | 6 |
| [frontend/next.config.ts:1](../frontend/next.config.ts#L1) | 8 |

## 7. 后端完整模块与公共入口

公共类/函数及类方法为静态导航，不意味着所有历史兼容函数仍被主链路调用；以报告的运行链路为准。下划线开头的内部函数不逐项展开，但保留其所在文件。

### __init__.py

[agent/app/__init__.py:1](../agent/app/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### api/__init__.py

[agent/app/api/__init__.py:1](../agent/app/api/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### api/application.py

[agent/app/api/application.py:1](../agent/app/api/application.py#L1)

- `lifespan:102`
- `HistoryResetRequest:143`
- `history_reset_options:150`
- `clear_history:156`
- `chat:168`
- `current_conversation:186`
- `list_conversations:200`
- `create_conversation:205`
- `conversation_search_state:210`
- `update_conversation_search_state:218`
- `conversation_context:240`
- `reset_conversation_context:254`
- `reset_conversation_search_state:275`
- `create_run:295`
- `list_runs:306`
- `read_run:311`
- `run_events:319`
- `run_steps:329`
- `run_ledger:338`
- `choose_job_target:350`
- `cancel_all_runs:360`
- `StopTasksRequest:369`
- `stop_tasks:375`
- `stop_tasks_status:382`
- `cancel_run:387`
- `resume_run:395`
- `decide_action_approval:405`
- `current_llm_config:414`
- `read_llm_config:429`
- `update_llm_config:437`
- `test_llm_connection:457`
- `start_browser_search:474`
- `upload_resume:486`
- `list_resumes:525`
- `read_original_resume:533`
- `delete_resume:549`
- `activate_resume:571`
- `upload_candidate_material:598`
- `ingest_candidate_document:641`
- `list_candidate_materials:705`
- `MaterialReparseRequest:712`
- `reparse_candidate_material:718`
- `read_candidate_state:749`
- `read_master_resume:756`
- `organize_master_resume:761`
- `list_resume_proposals:780`
- `approve_resume_proposal:787`
- `reject_resume_proposal:804`
- `update_resume_draft:815`
- `update_job_preferences:826`
- `current_browser_task:834`
- `browser_task_by_id:839`
- `browser_task_report:847`
- `pause_browser_task:855`
- `resume_browser_task:860`
- `stop_browser_task:868`
- `import_mock_site_job:873`
- `read_mock_workflow_settings:907`
- `update_mock_workflow_settings:912`
- `sync_mock_workflow:919`
- `list_stored_jobs:927`
- `update_job_stage:934`
- `JobDeliveryRequest:941`
- `deliver_selected_job:947`
- `create_delivery_preview:964`
- `authorize_delivery:981`
- `current_delivery:1012`
- `resume_delivery:1017`
- `delivery_by_run_id:1025`
- `automation_stats:1033`
- `insights:1038`
- `list_learning_plans:1050`
- `generate_learning_plan:1055`
- `complete_learning_gap:1074`
- `read_memory:1086`
- `update_memory:1091`
- `list_skills:1096`
- `read_skill:1101`
- `propose_skill_update:1111`
- `list_skill_proposals:1119`
- `approve_skill_proposal:1124`
- `job_analysis:1134`
- `queue_job_analysis:1139`
- `task_record:1143`
- `get_task:1150`
- `save_private_profile:1161`
- `read_private_profile:1169`

### browser/__init__.py

[agent/app/browser/__init__.py:1](../agent/app/browser/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### browser/automation.py

[agent/app/browser/automation.py:1](../agent/app/browser/automation.py#L1)

- `BrowserAutomationService:46 → start:78, continue_search:118, current:158, can_start_search:166, pause:171, resume:179, stop:191, shutdown:215`

### browser/boss_adapter.py

[agent/app/browser/boss_adapter.py:1](../agent/app/browser/boss_adapter.py#L1)

- `BossPageEvidence:18 → as_dict:29, intervention_reason:33`
- `BossAdapter:41 → search_url:47, safe_job_url:52, inspect:83, select_city:151, submit_search:236, wait_for_search_cards:292, wait_for_job_detail:312, extract_search_cards:323, extract_job_detail:350`

### browser/lifecycle.py

[agent/app/browser/lifecycle.py:1](../agent/app/browser/lifecycle.py#L1)

- `BrowserLifecycle:12 → record:20, watch_page:39, watch_context:52`

### browser/lock.py

[agent/app/browser/lock.py:1](../agent/app/browser/lock.py#L1)

- `BrowserLockBusy:8`
- `BrowserExecutionLock:12 → acquire:26, release:51`

### browser/mock_site_adapter.py

[agent/app/browser/mock_site_adapter.py:1](../agent/app/browser/mock_site_adapter.py#L1)

- `MockJobSiteSearchAdapter:15 → safe_job_url:21, inspect:31, open_login_prompt:75, search:126, extract_search_cards:204, wait_for_job_detail:229, extract_job_detail:232`

### browser/screening.py

[agent/app/browser/screening.py:1](../agent/app/browser/screening.py#L1)

- `parse_pre_screen_response:49`
- `pre_screen_cards:79`
- `clean_title:134`
- `detail_request_from_card:142`
- `match_job:163`
- `match_many:202`

### browser/session.py

[agent/app/browser/session.py:1](../agent/app/browser/session.py#L1)

- `BrowserSession:21 → open:38, attach_existing_cdp:93, ensure_cdp_browser:115, reuse:119, ensure_cdp_window:148, lifecycle_snapshot:305, pages:324, page_for_url:329, boss_page:340, recover_page:352, navigate:409, close:442`

### browser/zhaopin_adapter.py

[agent/app/browser/zhaopin_adapter.py:1](../agent/app/browser/zhaopin_adapter.py#L1)

- `ZhaopinAdapter:15 → search_url:21, safe_job_url:25, inspect:39, select_city:52, submit_search:57, wait_for_search_cards:67, wait_for_job_detail:74, extract_search_cards:81, extract_job_detail:97`

### core/__init__.py

[agent/app/core/__init__.py:1](../agent/app/core/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### core/action_language.py

[agent/app/core/action_language.py:1](../agent/app/core/action_language.py#L1)

- `action_clauses:13`
- `positive_action:17`
- `score_scope:21`
- `score_scope_in_source:58`
- `explicit_mock_delivery:88`
- `delivery_shortcut:97`
- `plan_scope_answer:120`

### core/agent_tools.py

[agent/app/core/agent_tools.py:1](../agent/app/core/agent_tools.py#L1)

- `MissingSearchInput:32`
- `MissingPlanScope:36`
- `MissingDeliveryEvidence:40`
- `AgentToolSpec:50 → capability_contract:57, openai_schema:64`
- `ToolLoopState:76`
- `AgentToolOutcome:86 → observation:97`
- `ToolCallGuard:106 → check:109`
- `AgentToolRegistry:124 → authorize:160, schemas:375, execute:378, validate_arguments:962`

### core/config.py

[agent/app/core/config.py:1](../agent/app/core/config.py#L1)

- `mock_history_path:11`
- `Settings:24`

### core/dependencies.py

[agent/app/core/dependencies.py:1](../agent/app/core/dependencies.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### core/job_filters.py

[agent/app/core/job_filters.py:1](../agent/app/core/job_filters.py#L1)

- `FilterExpression:29 → bounded_tree:39, leaves:56`
- `filter_schema:85`
- `validate_filter:107`
- `compose_filters:121`
- `combine:132`
- `filter_digest:148`
- `filter_facts_digest:153`
- `filter_facts:159`
- `filter_summary:175`
- `evaluate_filter:201`
- `partial_filter_status:330`

### core/job_followups.py

[agent/app/core/job_followups.py:1](../agent/app/core/job_followups.py#L1)

- `job_followup_request:7`

### core/job_targets.py

[agent/app/core/job_targets.py:1](../agent/app/core/job_targets.py#L1)

- `TargetClarification:18`
- `is_remaining_selector:28`
- `resolve_job_targets:33`

### core/main_agent.py

[agent/app/core/main_agent.py:1](../agent/app/core/main_agent.py#L1)

- `AutoHuntAgent:284 → restore_history:318, handle:419`

### core/middleware.py

[agent/app/core/middleware.py:1](../agent/app/core/middleware.py#L1)

- `HistoryMaintenanceMiddleware:22 → dispatch:29`
- `RequestIdMiddleware:50 → dispatch:51`
- `SecurityHeadersMiddleware:59 → dispatch:60`
- `BodyLimitMiddleware:70 → dispatch:75`
- `ApiKeyMiddleware:86 → dispatch:91`
- `RateLimitMiddleware:101 → dispatch:110`
- `AuditMiddleware:132 → dispatch:138`

### core/models.py

[agent/app/core/models.py:1](../agent/app/core/models.py#L1)

- `AgentLane:11`
- `TaskStatus:18`
- `BrowserTaskStatus:25`
- `BrowserMode:36`
- `ChatRequest:42 → strip_message:49`
- `ToolEvent:53`
- `ClarifyOption:59`
- `ResumeEntryView:66`
- `ResumeDocumentView:78`
- `ResumePatchOperation:96`
- `ResumeProposalView:106`
- `ResumeProposalDecision:120`
- `ChatResponse:126`
- `MemoryFactUpdate:139`
- `SkillUpdate:145`
- `JobAnalysisRequest:150`
- `JobAnalysis:160`
- `TaskRecord:168`
- `PrivateProfile:179`
- `LLMConfigUpdate:185`
- `LLMConfigView:191`
- `MockWorkflowUpdate:198`
- `BrowserSearchRequest:207 → clean_filters:227, mock_actions_only:232, strip_query:246, clean_queries:255, clean_locations:265`
- `ResumeView:273`
- `PreferenceListField:289`
- `PreferenceNumberField:296`
- `CandidateEvidenceItem:303`
- `CandidateProfileView:320`
- `JobPreferencesView:335 → cap_search_keywords:349`
- `JobPreferenceUpdate:354`
- `CandidateStateView:364`
- `ResumeDraftUpdate:371`
- `ResumeLayoutConsent:375`
- `ResumeIngestResult:382`
- `MaterialView:388`
- `MaterialIngestResult:399`
- `SearchFunnelView:407`
- `BrowserTaskView:422`
- `StoredJobView:453`
- `AutomationStats:473`
- `JobStageUpdate:491`
- `DeliveryPreviewView:495`
- `DeliveryTaskStatus:514`
- `DeliveryTaskView:526`
- `DeliveryAuthorizationRequest:551`
- `DeliveryAuthorizationView:556`

### core/replies.py

[agent/app/core/replies.py:1](../agent/app/core/replies.py#L1)

- `tool_label:39`
- `tool_reply:47`
- `has_internal_data:219`
- `public_reply:223`

### core/request_summary.py

[agent/app/core/request_summary.py:1](../agent/app/core/request_summary.py#L1)

- `is_request_summary:7`
- `is_search_summary:16`
- `requested_search_summary:35`
- `search_summary:71`
- `request_context:88`
- `request_summary:113`

### core/result_groups.py

[agent/app/core/result_groups.py:1](../agent/app/core/result_groups.py#L1)

- `ResultGroup:9 → hard_filter:17`
- `normalize_groups:26`
- `groups_schema:35`
- `grouped_filter:43`
- `group_verdicts:50`

### core/salary_conditions.py

[agent/app/core/salary_conditions.py:1](../agent/app/core/salary_conditions.py#L1)

- `SalaryPredicate:11 → valid_bounds:21`
- `salary_facts:31`
- `judge_salary:55`
- `normalized_search_filters:96`

### core/search_contract.py

[agent/app/core/search_contract.py:1](../agent/app/core/search_contract.py#L1)

- `numeric_value:12`
- `missing_search_message:42`
- `browser_search_keyword:47`
- `simple_search_patch:52`
- `safe_role_keyword:93`
- `needs_search_reasoning:117`
- `downstream_condition_scopes:126`
- `search_constraint_text:160`
- `explicit_search_constraints:168`
- `monthly_salary_lower_bound:233`

### core/search_language.py

[agent/app/core/search_language.py:1](../agent/app/core/search_language.py#L1)

- `SearchLanguage:45 → authorized:53, hint:56`
- `role_fragments:73`
- `parse_search_message:103`

### core/security.py

[agent/app/core/security.py:1](../agent/app/core/security.py#L1)

- `redact_text:49`
- `redact:61`
- `assert_no_secrets:104`
- `assert_public_memory:112`
- `secure_compare:121`

### core/task_semantics.py

[agent/app/core/task_semantics.py:1](../agent/app/core/task_semantics.py#L1)

- `mask_quotes:51`
- `clause_spans:55`
- `ActionMeaning:68`
- `RequestMeaning:80 → commands:84, revoked_tools:90, bind:93, public:104`
- `understand_sources:154`
- `understand:189`
- `command_grounded:193`
- `retained_reference:197`
- `retained_tools:209`
- `passive_positive:228`
- `condition_mode:236`
- `binding_for:249`
- `binding_valid:255`

### delivery/__init__.py

[agent/app/delivery/__init__.py:1](../agent/app/delivery/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### delivery/automation.py

[agent/app/delivery/automation.py:1](../agent/app/delivery/automation.py#L1)

- `UnsupportedDeliverySite:17`
- `DeliveryAutomationService:28 → current:53, reload_persisted_state:56, supports:60, authorize_and_start:70, resume_authorized_run:96, start:112, resume:128, stop:161, shutdown:183`

### delivery/mock_site_adapter.py

[agent/app/delivery/mock_site_adapter.py:1](../agent/app/delivery/mock_site_adapter.py#L1)

- `MockApplyObservation:14`
- `MockSubmitResult:26`
- `MockResumeBinding:34`
- `MockJobSiteAdapter:41 → normalize_job_url:54, inspect:75, prepare:182, bind_resume:203, submit:262`

### delivery/reconciliation.py

[agent/app/delivery/reconciliation.py:1](../agent/app/delivery/reconciliation.py#L1)

- `resume_fingerprint:13`
- `judge_receipt:22`
- `read_receipt:54`

### delivery/resume_binding.py

[agent/app/delivery/resume_binding.py:1](../agent/app/delivery/resume_binding.py#L1)

- `mock_resume_payload:8`

### growth/__init__.py

[agent/app/growth/__init__.py:1](../agent/app/growth/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### growth/plans.py

[agent/app/growth/plans.py:1](../agent/app/growth/plans.py#L1)

- `JDQuote:22`
- `GapPlan:27 → require_actionable_detail:50`
- `GeneratedPlan:59`
- `LearningCompletion:65`
- `ProjectPayload:71`
- `is_explicit_plan_request:93`
- `LearningPlanService:98 → list:111, generate:115, complete:221`

### growth/requests.py

[agent/app/growth/requests.py:1](../agent/app/growth/requests.py#L1)

- `resume_stamp:8`
- `continued_plan_scope:12`

### intent/__init__.py

[agent/app/intent/__init__.py:1](../agent/app/intent/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### intent/catalog.py

[agent/app/intent/catalog.py:1](../agent/app/intent/catalog.py#L1)

- `IntentSpec:17`

### intent/circuit.py

[agent/app/intent/circuit.py:1](../agent/app/intent/circuit.py#L1)

- `CircuitState:16`
- `CircuitBreaker:22 → state:46, allow:49, record_success:66, record_failure:75, reset:86`

### intent/normalize.py

[agent/app/intent/normalize.py:1](../agent/app/intent/normalize.py#L1)

- `normalize_text:19`
- `security_text:26`
- `query_hash:34`

### intent/router.py

[agent/app/intent/router.py:1](../agent/app/intent/router.py#L1)

- `RouteThresholds:43`
- `RouteOutcome:54`
- `IntentRouter:65 → llm_timeout_s:100, llm_available:103, record_llm_success:106, record_llm_failure:109, route:120, resolve_clarify_choice:239, record_final:252`

### intent/rules.py

[agent/app/intent/rules.py:1](../agent/app/intent/rules.py#L1)

- `Rule:47`
- `RuleMatch:57`
- `RuleEngine:157 → version:170, reload:173, activate_version:212, rollback:231, match:240, snapshot:265`

### intent/small_model.py

[agent/app/intent/small_model.py:1](../agent/app/intent/small_model.py#L1)

- `SmallModelError:40`
- `SmallModelDecision:45`
- `parse_logprobs_decision:53`
- `SmallModelClient:87 → available:119, classify:132`

### intent/store.py

[agent/app/intent/store.py:1](../agent/app/intent/store.py#L1)

- `utc_now:19`
- `RouterStore:23 → record_event:92, activate_version:115, rollback:162, current_version:187, hit_rule:195, record_disagreement:207, remember_clarify:232, lookup_clarify:244, summarize:255`

### jobs/__init__.py

[agent/app/jobs/__init__.py:1](../agent/app/jobs/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### jobs/analysis.py

[agent/app/jobs/analysis.py:1](../agent/app/jobs/analysis.py#L1)

- `analyze_job:107`

### jobs/search_reporting.py

[agent/app/jobs/search_reporting.py:1](../agent/app/jobs/search_reporting.py#L1)

- `job_characteristics:20`
- `jd_evidence:43`
- `aggregate_jd:66`
- `salary_overview:79`
- `render_search_report:87`
- `render_job_detail:136`

### jobs/store.py

[agent/app/jobs/store.py:1](../agent/app/jobs/store.py#L1)

- `utc_now:27`
- `normalized_key:31`
- `city_from_location:42`
- `JobStore:53 → create_task:243, update_task:256, get_task:273, latest_task:280, mock_workflow_settings:285, update_mock_workflow_settings:295, bind_mock_applicant:318, claim_daily_mock_run:324, record_daily_mock_run:333, release_daily_mock_run:337, record_daily_mock_report:344, record_mock_workflow_error:348, increment_funnel:398, checkpoint:411, event:429, add_conversation_message:453, conversation_history:471, search_conversation_history:496, create_delivery_run:508, bump_delivery_attempts:525, get_delivery_run:539, latest_delivery_run:546, update_delivery_run:551, authorize_delivery_run:574, mark_delivery_failed:626, reconcile_incomplete_runs:635, upsert_job:697, admit_search_result:747, get_job_from_connection:800, list_jobs:803, job_id_for_source_url:823, update_job_filter_result:828, search_source:833, refilter_candidates:844, save_refilter_results:860, save_search_request:928, search_recovery:934, mark_search_processed:944, job_touched_by_task:952, reconcile_search_counts:958, search_report:969, stats:1084, insights:1121, resume_analysis_evidence:1236, jobs_for_resume_analysis:1357, save_learning_plan:1365, enqueue_mock_deliveries:1375, record_authorization_audit:1405, authorization_audit:1414, authorization_audit_summary:1428, record_automation_decision:1455, automation_decisions:1469, mark_job_needs_review:1484, all_delivery_runs:1496, deliveries_to_reconcile:1503, record_delivery_reconciliation:1510, delivery_runs_failed_before_submit:1541, delivery_runs_waiting_reply:1554, mock_delivery_queue:1568, claim_mock_delivery:1582, mark_job_retrying_delivery:1587, update_mock_delivery:1598, learning_plans:1608, learning_plan:1616, update_stage:1623, remove_selection:1629, review_selection:1654, review_decisions:1667, review_fingerprint:1674, get_job:1678, latest_delivery_status_for_job:1685, create_delivery_preview:1693, get_delivery_preview_job_id:1751, authorize_delivery:1785, consume_delivery_authorization:1828, mark_delivery_waiting_reply:1851, assert_delivery_facts:1869, mark_recruiter_reply:1877, mark_delivery_completed:1889, mark_outcome_indeterminate:1897`

### llm/__init__.py

[agent/app/llm/__init__.py:1](../agent/app/llm/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### llm/base.py

[agent/app/llm/base.py:1](../agent/app/llm/base.py#L1)

- `LLMRequest:9`
- `LLMResult:22`
- `LLMToolCall:32`
- `LLMToolResult:39`
- `LLMProvider:50 → generate:55, generate_with_tools:58`

### llm/deepseek_provider.py

[agent/app/llm/deepseek_provider.py:1](../agent/app/llm/deepseek_provider.py#L1)

- `DeepSeekProvider:15 → generate:36, generate_with_tools:79, generate_with_tools_stream:130`

### llm/manager.py

[agent/app/llm/manager.py:1](../agent/app/llm/manager.py#L1)

- `LLMManager:15 → generate:59, provider_for_consent:76, generate_with_tools:82`

### llm/mock_provider.py

[agent/app/llm/mock_provider.py:1](../agent/app/llm/mock_provider.py#L1)

- `MockLLMProvider:7 → generate:13, generate_with_tools:25`

### llm/tracing.py

[agent/app/llm/tracing.py:1](../agent/app/llm/tracing.py#L1)

- `public_model_data:16`
- `traced_call:20`

### main.py

[agent/app/main.py:1](../agent/app/main.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### mcp/__init__.py

[agent/app/mcp/__init__.py:1](../agent/app/mcp/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### mcp/server.py

[agent/app/mcp/server.py:1](../agent/app/mcp/server.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### mcp/tools.py

[agent/app/mcp/tools.py:1](../agent/app/mcp/tools.py#L1)

- `register_tools:13`

### memory/__init__.py

[agent/app/memory/__init__.py:1](../agent/app/memory/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### memory/candidate_state.py

[agent/app/memory/candidate_state.py:1](../agent/app/memory/candidate_state.py#L1)

- `LLMProfilePayload:29`
- `LLMEvidencePayload:38`
- `CandidateProfileExtractor:336 → extract:340`
- `CandidateEvidenceExtractor:390 → extract:395`
- `CandidateStateStore:461 → read:496, apply_resume_profile:505, remove_resume_profile:548, apply_evidence:595, update_confirmed:647, confirm_search_query:673, reset_search_keywords_from_resume:684, remove_search_terms:700, update_resume_draft:714, sync_approved_resume_skills:724, render_context:752, render_resume_draft:779`

### memory/data_files.py

[agent/app/memory/data_files.py:1](../agent/app/memory/data_files.py#L1)

- `DataFileError:8`
- `DataFiles:12 → resolve:20, read_text:28, atomic_write:36`

### memory/document_router.py

[agent/app/memory/document_router.py:1](../agent/app/memory/document_router.py#L1)

- `DocumentClassification:13`
- `DocumentRouter:21 → classify:76`

### memory/material_store.py

[agent/app/memory/material_store.py:1](../agent/app/memory/material_store.py#L1)

- `MaterialError:26`
- `extract_material_text:68`
- `MaterialStore:91 → save:119, list:141, read_text:144`

### memory/resume_store.py

[agent/app/memory/resume_store.py:1](../agent/app/memory/resume_store.py#L1)

- `ResumeError:54`
- `extract_resume_text:105`
- `analyze_resume_text:128`
- `ResumeStore:135 → save:165, list:190, latest:193, get:197, read_original:209, delete:221, read_text:235`

### memory/resume_workspace.py

[agent/app/memory/resume_workspace.py:1](../agent/app/memory/resume_workspace.py#L1)

- `resume_document_digest:35`
- `LLMResumeEntryPayload:41`
- `LLMResumeDocumentPayload:51`
- `ResumeDocumentExtractor:276 → import_verbatim:282, extract:298`
- `ResumeWorkspaceStore:350 → ensure_document:381, read_document:399, sync_profile:405, propose_replace_from_upload:424, replace_from_upload:446, remove_base_resume:467, update_from_markdown:487, save_layout:602, read_proposal:631, list_proposals:640, propose_add_evidence:668, propose_learning_completion:711, propose_update:740, approve:835, reject:916, render_markdown:1006`

### memory/stores.py

[agent/app/memory/stores.py:1](../agent/app/memory/stores.py#L1)

- `MemoryStore:23 → read:28, update_fact:36`
- `SkillStore:63 → validate_name:68, list:73, read:78, update:82, propose:100, list_proposals:118, approve_proposal:126`
- `PrivateProfileStore:136 → save:147, read:152`
- `LLMSettingsStore:164 → read:176, save:185`
- `LLMCredentialStore:195 → save:219, read:232, has:237`

### resume_layout.py

[agent/app/resume_layout.py:1](../agent/app/resume_layout.py#L1)

- `SourceBlock:35`
- `LayoutNode:42`
- `LayoutPlan:49`
- `ResumeLayoutReport:54`
- `source_hash:68`
- `source_blocks:72`
- `validate_plan:82`
- `ResumeLayoutExtractor:109 → analyze:114`

### runtime/__init__.py

[agent/app/runtime/__init__.py:1](../agent/app/runtime/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### runtime/context.py

[agent/app/runtime/context.py:1](../agent/app/runtime/context.py#L1)

- `RunContext:10`
- `diagnostic_metadata:51`
- `emit_diagnostic:62`
- `active_run:72`
- `emit:78`
- `checkpoint:84`
- `effect_checkpoint:90`
- `save_search_state:111`
- `dialogue_change:131`
- `scoped_browser:143`
- `browser_search_available:154`
- `browser_availability:162`

### runtime/context_builder.py

[agent/app/runtime/context_builder.py:1](../agent/app/runtime/context_builder.py#L1)

- `encode:18`
- `estimate_tokens:22`
- `ContextBudgetExceeded:28`
- `ContextLimits:42 → configured:59`
- `turns:64`
- `complete_protocol:75`
- `ContextBuilder:94 → history:98, session:132, sections:156, model_input:179`

### runtime/control.py

[agent/app/runtime/control.py:1](../agent/app/runtime/control.py#L1)

- `stop_scope:5`

### runtime/dialogue.py

[agent/app/runtime/dialogue.py:1](../agent/app/runtime/dialogue.py#L1)

- `Strict:25`
- `Frame:29`
- `Snapshot:39`
- `Pending:51`
- `JobSelection:65`
- `DialogueState:73`
- `topic_for:96`
- `trace:118`
- `apply_operation:128`
- `resolve_reference:356`
- `pending_action:430`
- `slot_answer_patch:440`
- `compact_state:469`
- `bounded_data:491`
- `assemble_context:512`
- `compact_model_messages:517`

### runtime/history_reset.py

[agent/app/runtime/history_reset.py:1](../agent/app/runtime/history_reset.py#L1)

- `ResetBusy:23`
- `HistoryResetter:27 → mock_available:35, assert_idle:45, safe_path:57, clear:71`

### runtime/job_references.py

[agent/app/runtime/job_references.py:1](../agent/app/runtime/job_references.py#L1)

- `job_reference:9`
- `instruction_reference:34`

### runtime/ledger.py

[agent/app/runtime/ledger.py:1](../agent/app/runtime/ledger.py#L1)

- `number:14`
- `usage_tokens:18`
- `call_cost:28`
- `execution_ledger:50`

### runtime/manager.py

[agent/app/runtime/manager.py:1](../agent/app/runtime/manager.py#L1)

- `AgentRuntime:68 → start:84, shutdown:103, schedule:116, submit:124, track_delivery:158, track_browser:166, delivery_owner:174, finish:181, emit:198, save:242, action_snapshot:254, authorize_action:285, decide_approval:335, choose_job_target:340, resolve_approval:434, wait_response:458, search_state:471, execute:478, watch:642, watch_plan:690, publish_plan_answer:874, complete_workflow:916, watch_delivery:989, observe_browser:1010, observe_delivery:1021, recover_search_summaries:1046, browser_summary:1059, cancel:1082, stop_all:1106, stop_status:1110, stop_tasks:1120, active_plan:1242, revise_plan:1253, plan_for_revision:1337, continue_task:1346, resume:1455, stream:1483`

### runtime/memory_context.py

[agent/app/runtime/memory_context.py:1](../agent/app/runtime/memory_context.py#L1)

- `compile_context:15`
- `context_json:81`

### runtime/models.py

[agent/app/runtime/models.py:1](../agent/app/runtime/models.py#L1)

- `SessionCreate:8`
- `RunCreate:12 → valid_message:20`
- `ApprovalDecision:26`
- `TargetChoiceDecision:33`
- `public_run:42`

### runtime/resilience.py

[agent/app/runtime/resilience.py:1](../agent/app/runtime/resilience.py#L1)

- `PreflightRefused:15`
- `Failure:27 → metadata:33`
- `classify_failure:37`
- `safe_retry:67`
- `WatchLimits:109 → configured:117`
- `WatchdogExpired:122`
- `ProgressWatch:130 → check:140`
- `guarded_operation:158`
- `wait_expired:175`

### runtime/search_state.py

[agent/app/runtime/search_state.py:1](../agent/app/runtime/search_state.py#L1)

- `SearchState:10 → group_total:25, clean_filters:34, clean_list:39`
- `SearchPatch:45 → clean_groups:59`
- `prepare_search_state:64`
- `merge_search_state:96`
- `bounded_history:115`

### runtime/semantic_memory.py

[agent/app/runtime/semantic_memory.py:1](../agent/app/runtime/semantic_memory.py#L1)

- `terms:12`
- `SemanticMemory:18 → compact:24, recall:64`

### runtime/store.py

[agent/app/runtime/store.py:1](../agent/app/runtime/store.py#L1)

- `now:20`
- `safe_json:24`
- `RunStore:33 → connect:98, has_event:103, sessions:107, ensure_default_session:111, create_session:115, search_state:121, save_search_state:128, dialogue_state:141, mutate_dialogue:149, create:163, decode:193, get:201, list:208, request_records:217, memory_source_batch:238, save_memory:254, memories:272, memory_source:284, update:291, save_plan:314, event:331, all_events:339, execution_ledger:346, events:366, public_events:371, approval:380, approvals:387, ensure_approval:394, settle_approval:424, invalidate_approvals:443`

### runtime/understanding.py

[agent/app/runtime/understanding.py:1](../agent/app/runtime/understanding.py#L1)

- `TurnRole:13`
- `turn_role:18`
- `may_answer_pending:52`

### security/__init__.py

[agent/app/security/__init__.py:1](../agent/app/security/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### security/action_grants.py

[agent/app/security/action_grants.py:1](../agent/app/security/action_grants.py#L1)

- `argument_digest:14`
- `action_binding:19`
- `binding_matches:26`

### security/audit.py

[agent/app/security/audit.py:1](../agent/app/security/audit.py#L1)

- `salary_floor:28`
- `CandidateVerdict:51`
- `AuthorizationAudit:61 → as_row:76`
- `audit_delivery:93`

### security/decisions.py

[agent/app/security/decisions.py:1](../agent/app/security/decisions.py#L1)

- `Decision:10`
- `ActionDecision:27`
- `ActionPolicy:35 → evaluate:39`

### security/risk.py

[agent/app/security/risk.py:1](../agent/app/security/risk.py#L1)

- `SecurityRisk:37`
- `RiskNotComparable:56`
- `risk_rank:60`
- `at_least:68`
- `ActionRisk:76`
- `grade_action:85`
- `broad_delivery_scope:110`
- `ConfirmationPolicy:125 → should_confirm:129, describe:131`
- `AlwaysConfirm:136 → should_confirm:137`
- `NeverConfirm:142 → should_confirm:143`
- `ConfirmRisky:148 → should_confirm:158, describe:163`
- `ScopedInstruction:168 → covers:186`

### security/scope_changes.py

[agent/app/security/scope_changes.py:1](../agent/app/security/scope_changes.py#L1)

- `delivery_scope_change:49`

### workflow/__init__.py

[agent/app/workflow/__init__.py:1](../agent/app/workflow/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### workflow/contracts.py

[agent/app/workflow/contracts.py:1](../agent/app/workflow/contracts.py#L1)

- `PlannedStep:11`
- `Goal:28`

### workflow/delivery_rules.py

[agent/app/workflow/delivery_rules.py:1](../agent/app/workflow/delivery_rules.py#L1)

- `build_rules:21`

### workflow/delivery_scope.py

[agent/app/workflow/delivery_scope.py:1](../agent/app/workflow/delivery_scope.py#L1)

- `materialize_delivery_scope:15`

### workflow/execution.py

[agent/app/workflow/execution.py:1](../agent/app/workflow/execution.py#L1)

- `persist:24`
- `render_step:41`
- `step_receipt:46`
- `completion_steps:52`
- `completion_lines:99`
- `advance:104`
- `begin:285`

### workflow/failure_recovery.py

[agent/app/workflow/failure_recovery.py:1](../agent/app/workflow/failure_recovery.py#L1)

- `recovery_decision:17`
- `recover_failed_step:40`

### workflow/graph.py

[agent/app/workflow/graph.py:1](../agent/app/workflow/graph.py#L1)

- `wait_for_scope:14`
- `depend_on:21`
- `bind_evidence:36`
- `deduplicate_mutations:57`

### workflow/lifecycle.py

[agent/app/workflow/lifecycle.py:1](../agent/app/workflow/lifecycle.py#L1)

- `input_waits:11`
- `successful_receipt:31`
- `activate_staged_searches:42`

### workflow/mock_coordinator.py

[agent/app/workflow/mock_coordinator.py:1](../agent/app/workflow/mock_coordinator.py#L1)

- `MockWorkflowCoordinator:36 → settings:71, start:74, stop:78, observe:118, remember_authorization_scope:181, queue_deliveries:202, remove_jobs:379, cancel_delivery_queue:408, recover_failed_starts:440, check_reply_queue:490, process_delivery_queue:554, sync_once:629, reconcile_uncertain_deliveries:706, run_daily_if_due:801`

### workflow/observations.py

[agent/app/workflow/observations.py:1](../agent/app/workflow/observations.py#L1)

- `board_facts:16`
- `site_facts:42`
- `observe:77`

### workflow/planning.py

[agent/app/workflow/planning.py:1](../agent/app/workflow/planning.py#L1)

- `revoked:33`
- `Strict:37`
- `RequestDraft:41`
- `live_request:51`
- `pending_slot_answer:74`
- `source_search_constraints:81`
- `verified_slot:95`
- `shortcut_safe:131`
- `plan_request:145`
- `contextual_draft:245`
- `validate_draft:250`

### workflow/presentation.py

[agent/app/workflow/presentation.py:1](../agent/app/workflow/presentation.py#L1)

- `Section:46`
- `Answer:54`
- `Presentation:60`
- `compose:191`

### workflow/recovery.py

[agent/app/workflow/recovery.py:1](../agent/app/workflow/recovery.py#L1)

- `RecoveryDecision:13`
- `reconcile_receipts:19`
- `recover_plan:50`

### workflow/requests.py

[agent/app/workflow/requests.py:1](../agent/app/workflow/requests.py#L1)

- `TaskRequest:17`
- `is_continuation:51`
- `action_request:60`
- `request_selector:71`
- `capture_request:75`
- `amended_request:89`
- `withdrawn_request:108`
- `proven_idle_resume:120`
- `canonical_request:134`
- `failed_history_request:197`
- `employment_dropped:224`
- `Continuation:233`
- `stale_questions:239`
- `resolve_continuation:282`

### workflow/result_sources.py

[agent/app/workflow/result_sources.py:1](../agent/app/workflow/result_sources.py#L1)

- `ResultSourceClarification:14`
- `result_references:18`
- `resolve_search_source:31`

### workflow/revision.py

[agent/app/workflow/revision.py:1](../agent/app/workflow/revision.py#L1)

- `StepUpdate:15`
- `RevisionDraft:23`
- `edited_plan:36`

### workflow/rules.py

[agent/app/workflow/rules.py:1](../agent/app/workflow/rules.py#L1)

- `Observation:26 → get:32, as_dict:38`
- `Rule:49`
- `Decision:70 → as_dict:77`
- `RuleEngine:82 → register:96, matching:100, record:113, step:117`
- `fact:149`
- `any_fact:154`
- `all_facts:158`
- `run_until:162`

### workflow/target_selection.py

[agent/app/workflow/target_selection.py:1](../agent/app/workflow/target_selection.py#L1)

- `bind_target_dependencies:9`
- `prepared_selection:22`
- `excluded_selection:42`

### workflow/validation.py

[agent/app/workflow/validation.py:1](../agent/app/workflow/validation.py#L1)

- `ValidationIssue:21 → public:37, blocking:47`
- `PlanValidationError:51`
- `citation_found:58`
- `preflight:73`
- `preflight_warnings:142`
- `restore_quote_spacing:147`
- `normalize_encoding:163`
- `encoding_schema:179`
- `dependency_ancestors:194`
- `resolved_slots:207`
