# AutoHunt 源码技术索引

> 由 `scripts/technical-reference.py` 静态解析受 Git 跟踪的源码生成；不导入应用，不读取环境变量值、数据库、简历或日志。
> 与 [技术报告](technical-report-2026-10-23.md) 配套。行号为生成时位置；仅列定义不能证明生产路径已验收。

后端生产 Python 文件：**119**；物理行数（含注释、空行）：**28,646**。代码量不是质量或测试覆盖率指标。

## 1. 后端 HTTP 路由

静态装饰器定义共 84 项。变量路径保留表达式，权限和限流以中间件及处理函数为准；不假设每个接口都适合 MCP 调用。

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
| GET | '/api/v1/evaluation-reports' | list_evaluation_reports | [agent/app/api/application.py:350](../agent/app/api/application.py#L350) |
| GET | '/api/v1/evaluation-reports/{report_id}' | read_evaluation_report | [agent/app/api/application.py:356](../agent/app/api/application.py#L356) |
| POST | '/api/v1/runs/{run_id}/target-choice' | choose_job_target | [agent/app/api/application.py:368](../agent/app/api/application.py#L368) |
| POST | '/api/v1/runs/cancel-all' | cancel_all_runs | [agent/app/api/application.py:378](../agent/app/api/application.py#L378) |
| POST | '/api/v1/runs/control/stop' | stop_tasks | [agent/app/api/application.py:393](../agent/app/api/application.py#L393) |
| GET | '/api/v1/runs/control/stop' | stop_tasks_status | [agent/app/api/application.py:400](../agent/app/api/application.py#L400) |
| POST | '/api/v1/runs/{run_id}/cancel' | cancel_run | [agent/app/api/application.py:405](../agent/app/api/application.py#L405) |
| POST | '/api/v1/runs/{run_id}/resume' | resume_run | [agent/app/api/application.py:413](../agent/app/api/application.py#L413) |
| POST | '/api/v1/runs/{run_id}/approval' | decide_action_approval | [agent/app/api/application.py:423](../agent/app/api/application.py#L423) |
| GET | '/api/v1/llm/config' | read_llm_config | [agent/app/api/application.py:447](../agent/app/api/application.py#L447) |
| PUT | '/api/v1/llm/config' | update_llm_config | [agent/app/api/application.py:455](../agent/app/api/application.py#L455) |
| POST | '/api/v1/llm/test' | test_llm_connection | [agent/app/api/application.py:475](../agent/app/api/application.py#L475) |
| POST | '/api/v1/browser/search' | start_browser_search | [agent/app/api/application.py:492](../agent/app/api/application.py#L492) |
| POST | '/api/v1/resumes' | upload_resume | [agent/app/api/application.py:504](../agent/app/api/application.py#L504) |
| GET | '/api/v1/resumes' | list_resumes | [agent/app/api/application.py:543](../agent/app/api/application.py#L543) |
| GET | '/api/v1/resumes/{resume_id}/original' | read_original_resume | [agent/app/api/application.py:551](../agent/app/api/application.py#L551) |
| DELETE | '/api/v1/resumes/{resume_id}' | delete_resume | [agent/app/api/application.py:567](../agent/app/api/application.py#L567) |
| POST | '/api/v1/resumes/{resume_id}/activate' | activate_resume | [agent/app/api/application.py:589](../agent/app/api/application.py#L589) |
| POST | '/api/v1/materials' | upload_candidate_material | [agent/app/api/application.py:616](../agent/app/api/application.py#L616) |
| POST | '/api/v1/documents/ingest' | ingest_candidate_document | [agent/app/api/application.py:659](../agent/app/api/application.py#L659) |
| GET | '/api/v1/materials' | list_candidate_materials | [agent/app/api/application.py:723](../agent/app/api/application.py#L723) |
| POST | '/api/v1/materials/{material_id}/reparse' | reparse_candidate_material | [agent/app/api/application.py:736](../agent/app/api/application.py#L736) |
| GET | '/api/v1/candidate-state' | read_candidate_state | [agent/app/api/application.py:767](../agent/app/api/application.py#L767) |
| GET | '/api/v1/resume-documents/master' | read_master_resume | [agent/app/api/application.py:774](../agent/app/api/application.py#L774) |
| POST | '/api/v1/resume-documents/master/layout' | organize_master_resume | [agent/app/api/application.py:779](../agent/app/api/application.py#L779) |
| GET | '/api/v1/resume-proposals' | list_resume_proposals | [agent/app/api/application.py:798](../agent/app/api/application.py#L798) |
| POST | '/api/v1/resume-proposals/{proposal_id}/approve' | approve_resume_proposal | [agent/app/api/application.py:805](../agent/app/api/application.py#L805) |
| POST | '/api/v1/resume-proposals/{proposal_id}/reject' | reject_resume_proposal | [agent/app/api/application.py:822](../agent/app/api/application.py#L822) |
| PUT | '/api/v1/candidate-state/resume-draft' | update_resume_draft | [agent/app/api/application.py:833](../agent/app/api/application.py#L833) |
| PUT | '/api/v1/preferences' | update_job_preferences | [agent/app/api/application.py:844](../agent/app/api/application.py#L844) |
| GET | '/api/v1/browser/tasks/current' | current_browser_task | [agent/app/api/application.py:852](../agent/app/api/application.py#L852) |
| GET | '/api/v1/browser/tasks/{task_id}' | browser_task_by_id | [agent/app/api/application.py:857](../agent/app/api/application.py#L857) |
| GET | '/api/v1/browser/tasks/{task_id}/report' | browser_task_report | [agent/app/api/application.py:865](../agent/app/api/application.py#L865) |
| POST | '/api/v1/browser/tasks/current/pause' | pause_browser_task | [agent/app/api/application.py:873](../agent/app/api/application.py#L873) |
| POST | '/api/v1/browser/tasks/current/resume' | resume_browser_task | [agent/app/api/application.py:878](../agent/app/api/application.py#L878) |
| POST | '/api/v1/browser/tasks/current/stop' | stop_browser_task | [agent/app/api/application.py:886](../agent/app/api/application.py#L886) |
| POST | '/api/v1/mock-site/jobs/{external_id}/import' | import_mock_site_job | [agent/app/api/application.py:896](../agent/app/api/application.py#L896) |
| GET | '/api/v1/mock-workflow/settings' | read_mock_workflow_settings | [agent/app/api/application.py:930](../agent/app/api/application.py#L930) |
| POST | '/api/v1/mock-workflow/test' | test_daily_workflow | [agent/app/api/application.py:935](../agent/app/api/application.py#L935) |
| PUT | '/api/v1/mock-workflow/settings' | update_mock_workflow_settings | [agent/app/api/application.py:946](../agent/app/api/application.py#L946) |
| POST | '/api/v1/mock-workflow/sync' | sync_mock_workflow | [agent/app/api/application.py:959](../agent/app/api/application.py#L959) |
| GET | '/api/v1/jobs' | list_stored_jobs | [agent/app/api/application.py:967](../agent/app/api/application.py#L967) |
| GET | '/api/v1/applications/export' | download_applications | [agent/app/api/application.py:974](../agent/app/api/application.py#L974) |
| PUT | '/api/v1/jobs/{job_id}/stage' | update_job_stage | [agent/app/api/application.py:989](../agent/app/api/application.py#L989) |
| POST | '/api/v1/jobs/{job_id}/deliver' | deliver_selected_job | [agent/app/api/application.py:1002](../agent/app/api/application.py#L1002) |
| POST | '/api/v1/jobs/{job_id}/delivery-preview' | create_delivery_preview | [agent/app/api/application.py:1019](../agent/app/api/application.py#L1019) |
| POST | '/api/v1/deliveries/{preview_id}/authorize' | authorize_delivery | [agent/app/api/application.py:1036](../agent/app/api/application.py#L1036) |
| GET | '/api/v1/deliveries/current' | current_delivery | [agent/app/api/application.py:1067](../agent/app/api/application.py#L1067) |
| POST | '/api/v1/deliveries/current/resume' | resume_delivery | [agent/app/api/application.py:1072](../agent/app/api/application.py#L1072) |
| GET | '/api/v1/deliveries/{run_id}' | delivery_by_run_id | [agent/app/api/application.py:1080](../agent/app/api/application.py#L1080) |
| GET | '/api/v1/automation/stats' | automation_stats | [agent/app/api/application.py:1088](../agent/app/api/application.py#L1088) |
| GET | '/api/v1/insights' | insights | [agent/app/api/application.py:1093](../agent/app/api/application.py#L1093) |
| GET | '/api/v1/interview-preparations' | list_interview_preparations | [agent/app/api/application.py:1111](../agent/app/api/application.py#L1111) |
| GET | '/api/v1/interview-preparations/{report_id}' | get_interview_preparation | [agent/app/api/application.py:1116](../agent/app/api/application.py#L1116) |
| POST | '/api/v1/interview-preparations' | generate_interview_preparation | [agent/app/api/application.py:1124](../agent/app/api/application.py#L1124) |
| GET | '/api/v1/memory' | read_memory | [agent/app/api/application.py:1138](../agent/app/api/application.py#L1138) |
| PUT | '/api/v1/memory/facts' | update_memory | [agent/app/api/application.py:1143](../agent/app/api/application.py#L1143) |
| GET | '/api/v1/skills' | list_skills | [agent/app/api/application.py:1148](../agent/app/api/application.py#L1148) |
| GET | '/api/v1/skills/{name}' | read_skill | [agent/app/api/application.py:1153](../agent/app/api/application.py#L1153) |
| POST | '/api/v1/skill-proposals/{name}' | propose_skill_update | [agent/app/api/application.py:1163](../agent/app/api/application.py#L1163) |
| GET | '/api/v1/skill-proposals' | list_skill_proposals | [agent/app/api/application.py:1171](../agent/app/api/application.py#L1171) |
| POST | '/api/v1/skill-proposals/{proposal_id}/approve' | approve_skill_proposal | [agent/app/api/application.py:1176](../agent/app/api/application.py#L1176) |
| POST | '/api/v1/jobs/analyze' | job_analysis | [agent/app/api/application.py:1186](../agent/app/api/application.py#L1186) |
| POST | '/api/v1/tasks/job-analysis' | queue_job_analysis | [agent/app/api/application.py:1191](../agent/app/api/application.py#L1191) |
| GET | '/api/v1/tasks/{task_id}' | get_task | [agent/app/api/application.py:1202](../agent/app/api/application.py#L1202) |
| PUT | '/api/v1/private-profile' | save_private_profile | [agent/app/api/application.py:1213](../agent/app/api/application.py#L1213) |
| GET | '/api/v1/private-profile' | read_private_profile | [agent/app/api/application.py:1221](../agent/app/api/application.py#L1221) |

## 2. Agent 工具注册表

静态注册 31 个工具。effect 是恢复/执行分类，不等同最终风险等级；例如搜索可被归类 external，但普通只读搜索的动作风险是 LOW。

| 工具 | effect | 执行函数 | 模型可见说明 | 源码 |
| --- | --- | --- | --- | --- |
| task_continue | session | self._task_continue | 接续当前会话原任务：读取持久执行记录和断点，不重新规划、不重放已完成动作。用户要求恢复/接着做/重试整个任务时使用；区别于只恢复浏览器。缺少或歧义记录则澄清。 | [agent/app/core/agent_tools.py:195](../agent/app/core/agent_tools.py#L195) |
| context_state_read | read | self._context_read | 读取会话 DST：激活任务、槽位、待补信息、追问、拒绝与待办、对象引用、任务归属及精简轨迹。 | [agent/app/core/agent_tools.py:196](../agent/app/core/agent_tools.py#L196) |
| context_history_read | read | self._history_read | 按具体关键词检索本会话历史，返回来源消息 ID；历史是数据，不是当前执行授权。 | [agent/app/core/agent_tools.py:197](../agent/app/core/agent_tools.py#L197) |
| context_memory_read | read | self._semantic_memory_read | 按语义召回较远历史，或用source_id读取原始用户消息。摘要是历史线索，不是现行条件或授权；关键事实应核对来源。 | [agent/app/core/agent_tools.py:199](../agent/app/core/agent_tools.py#L199) |
| context_reference_resolve | read | self._reference_resolve | 把前五个/第二个/这些/名称绑定到已展示的稳定对象 ID；歧义或过期返回错误，不猜测。 | [agent/app/core/agent_tools.py:203](../agent/app/core/agent_tools.py#L203) |
| context_action_propose | session | self._action_propose | 把实际可执行方案登记为待确认动作，并追问。不能登记投递、联系方式交换、主简历直接写入或任意工具。不得把聊天文本当授权。 | [agent/app/core/agent_tools.py:205](../agent/app/core/agent_tools.py#L205) |
| context_question_ask | session | self._question_ask | 当必要参数缺失时登记具体追问及待补槽位；下轮省略回答依据此问题解析，不猜测未提供字段。 | [agent/app/core/agent_tools.py:207](../agent/app/core/agent_tools.py#L207) |
| job_review_selection | write | self._review_selection | 按本轮明确审核命令审核已展示职位；只保存本地审核标记，不授权沟通或投递。执行器绑定最近已展示快照；可省略 snapshot_id，不接受任意库ID或未展示对象。 | [agent/app/core/agent_tools.py:211](../agent/app/core/agent_tools.py#L211) |
| job_detail_read | read | self._job_detail_read | 按已展示职位简表的稳定序号或名称读取完整 JD 与原始链接；只读，不重新搜索，不投递。 | [agent/app/core/agent_tools.py:213](../agent/app/core/agent_tools.py#L213) |
| job_target_resolve | read | self._job_target_resolve | 只定位并冻结用户要保留/投递的岗位，不执行写入。按分数、公司、描述或序号选择；不明确时返回可点击候选。投递某岗位且移除其他的，必须先此步骤，再让移除exclude_target_step_id和投递target_step_id引用此步骤，不能把某公司全部当保留集合。 | [agent/app/core/agent_tools.py:216](../agent/app/core/agent_tools.py#L216) |
| job_remove_selection | write | self._job_remove_selection | 移出申请看板并取消选中岗位未发送的投递。移除其余岗位必须exclude_target_step_id引用先完成的job_target_resolve，按冻结范围减去选中ID，不由公司名猜补集。保留JD与全部历史。 | [agent/app/core/agent_tools.py:220](../agent/app/core/agent_tools.py#L220) |
| search_state_read | read | self._search_state_read | 读取当前会话搜索条件，包括岗位词、城市、数量、最低月薪及来源；不是长期偏好。 | [agent/app/core/agent_tools.py:226](../agent/app/core/agent_tools.py#L226) |
| search_state_update | session | self._search_state_update | 仅更新用户明确改变的会话搜索字段；不启动搜索，不改主简历或长期偏好。省略字段保持原值。 | [agent/app/core/agent_tools.py:228](../agent/app/core/agent_tools.py#L228) |
| job_results_refilter | write | self._job_results_refilter | 仅按已保存的会话条件重算已完成搜索JD的准入证据（含原信息不足结果）。它不改变条件、不移除选中岗位；改变条件用search_state_update，移除岗位用job_remove_selection。不启动浏览器，不沟通或投递；后续使用新证据的投递依赖此步骤。最多30个，不补入未读取职位。 | [agent/app/core/agent_tools.py:240](../agent/app/core/agent_tools.py#L240) |
| job_results_read | read | self._job_results | 读取已经保存的职位搜索结果。用户说“职位搜索结果、找到哪些岗位、查看结果”时使用；绝不能用它启动浏览器。 | [agent/app/core/agent_tools.py:243](../agent/app/core/agent_tools.py#L243) |
| job_stats_read | read | self._job_stats | 读取职位库和最近搜索任务的统计数据，不启动搜索。 | [agent/app/core/agent_tools.py:254](../agent/app/core/agent_tools.py#L254) |
| job_search_start | external | self._job_search_start | 启动所选招聘平台的职位搜索。mock可授权模拟投递；boss/zhaopin只读取不沟通投递。查询已有结果时禁止调用。 | [agent/app/core/agent_tools.py:261](../agent/app/core/agent_tools.py#L261) |
| preference_read | read | self._preference_read | 读取持久化求职偏好及其来源。需要决定搜索关键词、城市或岗位方向前使用。 | [agent/app/core/agent_tools.py:280](../agent/app/core/agent_tools.py#L280) |
| preference_update | write | self._preference_update | 仅在用户明确要求修改时更新持久化求职偏好；搜索对话不能隐式更新。 | [agent/app/core/agent_tools.py:287](../agent/app/core/agent_tools.py#L287) |
| resume_document_read | read | self._resume_read | 读取当前结构化主简历。用户询问简历内容、建议或准备修改时使用。 | [agent/app/core/agent_tools.py:298](../agent/app/core/agent_tools.py#L298) |
| resume_propose_patch | write | self._resume_patch | 把明确的简历修改要求转换为待审核 Diff，不会直接写入主简历。 | [agent/app/core/agent_tools.py:305](../agent/app/core/agent_tools.py#L305) |
| browser_task_read | read | self._browser_read | 读取共享浏览器可用性（can_start_search）及本会话任务详情。其他会话旧任务的归属不代表浏览器占用。 | [agent/app/core/agent_tools.py:312](../agent/app/core/agent_tools.py#L312) |
| browser_task_pause | external | self._browser_pause | 按用户明确请求暂停本会话的浏览器搜索，不暂停其他会话。 | [agent/app/core/agent_tools.py:319](../agent/app/core/agent_tools.py#L319) |
| browser_task_resume | external | self._browser_resume | 用户已完成登录、验证码或 MFA 后恢复当前浏览器任务。 | [agent/app/core/agent_tools.py:324](../agent/app/core/agent_tools.py#L324) |
| browser_task_stop | external | self._browser_stop | 按用户明确要求停止当前浏览器任务。 | [agent/app/core/agent_tools.py:331](../agent/app/core/agent_tools.py#L331) |
| memory_read | read | self._memory_read | 读取去敏后的公开长期记忆；不会返回 API Key、电话或邮箱。 | [agent/app/core/agent_tools.py:338](../agent/app/core/agent_tools.py#L338) |
| interview_preparations_read | read | self._interview_preparations_read | 读取当前简历已保存的面试准备，不重新生成；report_id读取一份完整报告，省略则列出最近报告。 | [agent/app/core/agent_tools.py:362](../agent/app/core/agent_tools.py#L362) |
| interview_preparation_generate | write | self._interview_preparation_generate | 根据选定岗位JD和当前简历证据生成可能的面试问题、追问和准备要点，保存可追溯报告；不是实际面试题或录用概率。岗位用本轮序号、名称或描述定位，不把序号当数据库ID。 | [agent/app/core/agent_tools.py:366](../agent/app/core/agent_tools.py#L366) |
| mock_delivery_start | external | self._mock_delivery_start | 将用户要求范围内的已搜索模拟站职位加入有限投递队列。批量补投默认继承上一批完整条件，scope_mode=new用于用户明确另起或替换范围；指定岗位不继承旧批量条件。filters为投递独立条件，可复用搜索判断。内部生成逐职位预览与一次性授权，快速逐个沟通、并行等待HR回复、回复后发送简历；不支持真实网站。 | [agent/app/core/agent_tools.py:348](../agent/app/core/agent_tools.py#L348) |
| mock_delivery_cancel | external | self._mock_delivery_cancel | 按明确停止命令取消本会话的模拟站投递队列，已发送结果保留。 | [agent/app/core/agent_tools.py:358](../agent/app/core/agent_tools.py#L358) |
| mock_delivery_status | read | self._mock_delivery_status | 读取本会话模拟站自动投递队列状态，不启动或重试。 | [agent/app/core/agent_tools.py:359](../agent/app/core/agent_tools.py#L359) |

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
| AUTOHUNT_ALLOWED_ORIGINS | 'http://127.0.0.1:3000,http://localhost:3000' | [agent/app/core/config.py:34](../agent/app/core/config.py#L34) |
| AUTOHUNT_API_KEY | None | [agent/app/core/config.py:26](../agent/app/core/config.py#L26) |
| AUTOHUNT_APPROVAL_POLICY | 'risk' | [agent/app/core/config.py:57](../agent/app/core/config.py#L57) |
| AUTOHUNT_APPROVAL_THRESHOLD | 'HIGH' | [agent/app/core/config.py:58](../agent/app/core/config.py#L58) |
| AUTOHUNT_BROWSER_CDP_URL | 'http://127.0.0.1:9223' | [agent/app/core/config.py:48](../agent/app/core/config.py#L48) |
| AUTOHUNT_BROWSER_HEADLESS | 'false' | [agent/app/core/config.py:47](../agent/app/core/config.py#L47) |
| AUTOHUNT_DATA_KEY | None | [agent/app/core/config.py:27](../agent/app/core/config.py#L27) |
| AUTOHUNT_DATA_ROOT | str(BACKEND_ROOT / 'data') | [agent/app/core/config.py:25](../agent/app/core/config.py#L25) |
| AUTOHUNT_HISTORY_RESET_ENABLED | 'false' | [agent/app/core/config.py:55](../agent/app/core/config.py#L55) |
| AUTOHUNT_LLM_MODEL | 'mock-autohunt-v1' | [agent/app/core/config.py:45](../agent/app/core/config.py#L45) |
| AUTOHUNT_LLM_PROVIDER | 'mock' | [agent/app/core/config.py:44](../agent/app/core/config.py#L44) |
| AUTOHUNT_LOG_LEVEL | 'INFO' | [agent/app/core/config.py:43](../agent/app/core/config.py#L43) |
| AUTOHUNT_MOCK_HISTORY_DATABASE | None | [agent/app/core/config.py:56](../agent/app/core/config.py#L56) |
| AUTOHUNT_RATE_LIMIT | '60' | [agent/app/core/config.py:40](../agent/app/core/config.py#L40) |
| AUTOHUNT_READ_RATE_LIMIT | '600' | [agent/app/core/config.py:41](../agent/app/core/config.py#L41) |
| AUTOHUNT_ROUTER_SMALL_API_KEY | None | [agent/app/core/config.py:51](../agent/app/core/config.py#L51) |
| AUTOHUNT_ROUTER_SMALL_BASE_URL | 'https://api.siliconflow.cn/v1' | [agent/app/core/config.py:52](../agent/app/core/config.py#L52) |
| AUTOHUNT_ROUTER_SMALL_MODEL | '' | [agent/app/core/config.py:53](../agent/app/core/config.py#L53) |
| AUTOHUNT_TRUSTED_HOSTS | '127.0.0.1,localhost,testserver' | [agent/app/core/config.py:29](../agent/app/core/config.py#L29) |
| DEEPSEEK_API_KEY | None | [agent/app/core/config.py:46](../agent/app/core/config.py#L46) |

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
| job_removals | job_id INTEGER PRIMARY KEY,session_id TEXT NOT NULL,run_id TEXT NOT NULL,content_hash TEXT NOT NULL,removed_at TEXT NOT NULL | [agent/app/jobs/store.py:193](../agent/app/jobs/store.py#L193) |
| job_review_decisions | job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL, run_id TEXT NOT NULL, content_hash TEXT NOT NULL, decided_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| jobs | id INTEGER PRIMARY KEY AUTOINCREMENT, dedupe_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, source_url TEXT NOT NULL, external_id TEXT, title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL, salary TEXT NOT NULL, description TEXT NOT NULL, tags TEXT NOT NULL, score INTEGER NOT NULL DEFAULT 0, decision TEXT NOT NULL DEFAULT 'REVIEW', reasoning TEXT NOT NULL DEFAULT '', stage TEXT NOT NULL DEFAULT 'review', mode TEXT NOT NULL DEFAULT '人工确认', content_hash TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| mock_delivery_queue | job_id INTEGER PRIMARY KEY REFERENCES jobs(id), session_id TEXT NOT NULL, resume_stamp TEXT NOT NULL, binding_json TEXT NOT NULL, job_hash TEXT NOT NULL, min_score INTEGER NOT NULL, status TEXT NOT NULL, run_id TEXT, detail TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
| mock_workflow_settings | id INTEGER PRIMARY KEY CHECK(id=1), applicant_id INTEGER, monitor_enabled INTEGER NOT NULL DEFAULT 0, auto_submit INTEGER NOT NULL DEFAULT 0, auto_submit_enabled_at TEXT, daily_enabled INTEGER NOT NULL DEFAULT 0, daily_time TEXT NOT NULL DEFAULT '09:00', min_score INTEGER NOT NULL DEFAULT 75, max_contacts INTEGER NOT NULL DEFAULT 3, max_search_jobs INTEGER NOT NULL DEFAULT 30, last_run_date TEXT, last_run_id TEXT, last_reported_run_id TEXT, last_error TEXT | [agent/app/jobs/store.py:68](../agent/app/jobs/store.py#L68) |
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
| [frontend/app/api/agent/applications/export/route.ts:1](../frontend/app/api/agent/applications/export/route.ts#L1) | 38 |
| [frontend/app/api/agent/browser-report/route.ts:1](../frontend/app/api/agent/browser-report/route.ts#L1) | 20 |
| [frontend/app/api/agent/browser/route.ts:1](../frontend/app/api/agent/browser/route.ts#L1) | 57 |
| [frontend/app/api/agent/candidate-state/route.ts:1](../frontend/app/api/agent/candidate-state/route.ts#L1) | 36 |
| [frontend/app/api/agent/chat/route.ts:1](../frontend/app/api/agent/chat/route.ts#L1) | 53 |
| [frontend/app/api/agent/conversations/[sessionId]/context/route.ts:1](../frontend/app/api/agent/conversations/[sessionId]/context/route.ts#L1) | 20 |
| [frontend/app/api/agent/conversations/[sessionId]/search-state/route.ts:1](../frontend/app/api/agent/conversations/[sessionId]/search-state/route.ts#L1) | 22 |
| [frontend/app/api/agent/conversations/route.ts:1](../frontend/app/api/agent/conversations/route.ts#L1) | 19 |
| [frontend/app/api/agent/delivery/route.ts:1](../frontend/app/api/agent/delivery/route.ts#L1) | 31 |
| [frontend/app/api/agent/documents/route.ts:1](../frontend/app/api/agent/documents/route.ts#L1) | 58 |
| [frontend/app/api/agent/evaluation-reports/[[...parts]]/route.ts:1](../frontend/app/api/agent/evaluation-reports/[[...parts]]/route.ts#L1) | 20 |
| [frontend/app/api/agent/history/route.ts:1](../frontend/app/api/agent/history/route.ts#L1) | 32 |
| [frontend/app/api/agent/insights/route.ts:1](../frontend/app/api/agent/insights/route.ts#L1) | 19 |
| [frontend/app/api/agent/interview-preparations/route.ts:1](../frontend/app/api/agent/interview-preparations/route.ts#L1) | 37 |
| [frontend/app/api/agent/jobs/route.ts:1](../frontend/app/api/agent/jobs/route.ts#L1) | 52 |
| [frontend/app/api/agent/llm-config/route.ts:1](../frontend/app/api/agent/llm-config/route.ts#L1) | 23 |
| [frontend/app/api/agent/llm-test/route.ts:1](../frontend/app/api/agent/llm-test/route.ts#L1) | 19 |
| [frontend/app/api/agent/materials/route.ts:1](../frontend/app/api/agent/materials/route.ts#L1) | 68 |
| [frontend/app/api/agent/mock-workflow/route.ts:1](../frontend/app/api/agent/mock-workflow/route.ts#L1) | 36 |
| [frontend/app/api/agent/resume-proposals/route.ts:1](../frontend/app/api/agent/resume-proposals/route.ts#L1) | 45 |
| [frontend/app/api/agent/resumes/route.ts:1](../frontend/app/api/agent/resumes/route.ts#L1) | 95 |
| [frontend/app/api/agent/runs/[[...parts]]/route.ts:1](../frontend/app/api/agent/runs/[[...parts]]/route.ts#L1) | 33 |
| [frontend/app/api/agent/stats/route.ts:1](../frontend/app/api/agent/stats/route.ts#L1) | 16 |
| [frontend/app/globals.css:1](../frontend/app/globals.css#L1) | 798 |
| [frontend/app/layout.tsx:1](../frontend/app/layout.tsx#L1) | 17 |
| [frontend/app/page.tsx:1](../frontend/app/page.tsx#L1) | 2188 |
| [frontend/app/workspace.css:1](../frontend/app/workspace.css#L1) | 427 |
| [frontend/components/application-export.module.css:1](../frontend/components/application-export.module.css#L1) | 7 |
| [frontend/components/application-export.tsx:1](../frontend/components/application-export.tsx#L1) | 69 |
| [frontend/components/evaluation-document.tsx:1](../frontend/components/evaluation-document.tsx#L1) | 35 |
| [frontend/components/imported-resume.tsx:1](../frontend/components/imported-resume.tsx#L1) | 312 |
| [frontend/components/interview-preparation.module.css:1](../frontend/components/interview-preparation.module.css#L1) | 31 |
| [frontend/components/interview-preparation.tsx:1](../frontend/components/interview-preparation.tsx#L1) | 148 |
| [frontend/components/job-target-choices.tsx:1](../frontend/components/job-target-choices.tsx#L1) | 51 |
| [frontend/components/metrics-dashboard.css:1](../frontend/components/metrics-dashboard.css#L1) | 80 |
| [frontend/components/metrics-dashboard.tsx:1](../frontend/components/metrics-dashboard.tsx#L1) | 231 |
| [frontend/components/runtime-panel.tsx:1](../frontend/components/runtime-panel.tsx#L1) | 153 |
| [frontend/components/workspace-icon.tsx:1](../frontend/components/workspace-icon.tsx#L1) | 27 |
| [frontend/lib/agent-runtime.ts:1](../frontend/lib/agent-runtime.ts#L1) | 84 |
| [frontend/lib/application-export.ts:1](../frontend/lib/application-export.ts#L1) | 19 |
| [frontend/lib/chat-display.ts:1](../frontend/lib/chat-display.ts#L1) | 18 |
| [frontend/lib/chat-message.ts:1](../frontend/lib/chat-message.ts#L1) | 14 |
| [frontend/lib/execution-metrics.ts:1](../frontend/lib/execution-metrics.ts#L1) | 53 |
| [frontend/lib/inventory-request.ts:1](../frontend/lib/inventory-request.ts#L1) | 26 |
| [frontend/lib/job-display.ts:1](../frontend/lib/job-display.ts#L1) | 8 |
| [frontend/lib/search-condition-display.ts:1](../frontend/lib/search-condition-display.ts#L1) | 38 |
| [frontend/lib/types.ts:1](../frontend/lib/types.ts#L1) | 403 |
| [frontend/lib/use-agent-runtime.ts:1](../frontend/lib/use-agent-runtime.ts#L1) | 300 |
| [frontend/next-env.d.ts:1](../frontend/next-env.d.ts#L1) | 6 |
| [frontend/next.config.ts:1](../frontend/next.config.ts#L1) | 11 |

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
- `list_evaluation_reports:350`
- `read_evaluation_report:356`
- `choose_job_target:368`
- `cancel_all_runs:378`
- `StopTasksRequest:387`
- `stop_tasks:393`
- `stop_tasks_status:400`
- `cancel_run:405`
- `resume_run:413`
- `decide_action_approval:423`
- `current_llm_config:432`
- `read_llm_config:447`
- `update_llm_config:455`
- `test_llm_connection:475`
- `start_browser_search:492`
- `upload_resume:504`
- `list_resumes:543`
- `read_original_resume:551`
- `delete_resume:567`
- `activate_resume:589`
- `upload_candidate_material:616`
- `ingest_candidate_document:659`
- `list_candidate_materials:723`
- `MaterialReparseRequest:730`
- `reparse_candidate_material:736`
- `read_candidate_state:767`
- `read_master_resume:774`
- `organize_master_resume:779`
- `list_resume_proposals:798`
- `approve_resume_proposal:805`
- `reject_resume_proposal:822`
- `update_resume_draft:833`
- `update_job_preferences:844`
- `current_browser_task:852`
- `browser_task_by_id:857`
- `browser_task_report:865`
- `pause_browser_task:873`
- `resume_browser_task:878`
- `stop_browser_task:886`
- `import_mock_site_job:896`
- `read_mock_workflow_settings:930`
- `test_daily_workflow:935`
- `update_mock_workflow_settings:946`
- `sync_mock_workflow:959`
- `list_stored_jobs:967`
- `download_applications:974`
- `update_job_stage:989`
- `JobDeliveryRequest:996`
- `deliver_selected_job:1002`
- `create_delivery_preview:1019`
- `authorize_delivery:1036`
- `current_delivery:1067`
- `resume_delivery:1072`
- `delivery_by_run_id:1080`
- `automation_stats:1088`
- `insights:1093`
- `InterviewPreparationRequest:1104`
- `list_interview_preparations:1111`
- `get_interview_preparation:1116`
- `generate_interview_preparation:1124`
- `read_memory:1138`
- `update_memory:1143`
- `list_skills:1148`
- `read_skill:1153`
- `propose_skill_update:1163`
- `list_skill_proposals:1171`
- `approve_skill_proposal:1176`
- `job_analysis:1186`
- `queue_job_analysis:1191`
- `task_record:1195`
- `get_task:1202`
- `save_private_profile:1213`
- `read_private_profile:1221`

### browser/__init__.py

[agent/app/browser/__init__.py:1](../agent/app/browser/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### browser/automation.py

[agent/app/browser/automation.py:1](../agent/app/browser/automation.py#L1)

- `DailySubmissionDisabled:48`
- `BrowserAutomationService:52 → start:84, continue_search:135, current:175, can_start_search:183, pause:188, resume:196, stop:208, shutdown:232`

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

- `parse_pre_screen_response:53`
- `pre_screen_cards:83`
- `clean_title:138`
- `detail_request_from_card:146`
- `match_job:169`
- `match_many:211`

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
- `MissingDeliveryEvidence:38`
- `AgentToolSpec:48 → capability_contract:55, openai_schema:62`
- `ToolLoopState:74`
- `AgentToolOutcome:84 → observation:95`
- `ToolCallGuard:104 → check:107`
- `AgentToolRegistry:122 → authorize:158, schemas:375, execute:378, validate_arguments:953`

### core/config.py

[agent/app/core/config.py:1](../agent/app/core/config.py#L1)

- `mock_history_path:11`
- `Settings:24`

### core/dependencies.py

[agent/app/core/dependencies.py:1](../agent/app/core/dependencies.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### core/job_filters.py

[agent/app/core/job_filters.py:1](../agent/app/core/job_filters.py#L1)

- `node_branches:35`
- `FilterExpression:49 → serialize:66, bounded_tree:73, leaves:98`
- `filter_schema:127`
- `validate_filter:145`
- `compose_filters:159`
- `combine:170`
- `filter_digest:186`
- `filter_facts_digest:191`
- `filter_facts:197`
- `filter_summary:213`
- `evaluate_filter:239`
- `partial_filter_status:368`

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

- `AutoHuntAgent:284 → restore_history:312, handle:413`

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
- `TaskStatus:17`
- `BrowserTaskStatus:24`
- `BrowserMode:35`
- `ChatRequest:41 → strip_message:48`
- `ToolEvent:52`
- `ClarifyOption:58`
- `ResumeEntryView:65`
- `ResumeDocumentView:77`
- `ResumePatchOperation:95`
- `ResumeProposalView:105`
- `ResumeProposalDecision:119`
- `ChatResponse:125`
- `MemoryFactUpdate:138`
- `SkillUpdate:144`
- `JobAnalysisRequest:149`
- `JobAnalysis:160`
- `TaskRecord:170`
- `PrivateProfile:181`
- `LLMConfigUpdate:187`
- `LLMConfigView:193`
- `MockWorkflowUpdate:200`
- `BrowserSearchRequest:210 → clean_filters:230, mock_actions_only:235, strip_query:249, clean_queries:258, clean_locations:268`
- `ResumeView:276`
- `PreferenceListField:292`
- `PreferenceNumberField:299`
- `CandidateEvidenceItem:306`
- `CandidateProfileView:323`
- `JobPreferencesView:339 → cap_search_keywords:353`
- `JobPreferenceUpdate:358`
- `CandidateStateView:368`
- `ResumeDraftUpdate:375`
- `ResumeLayoutConsent:379`
- `ResumeIngestResult:386`
- `MaterialView:392`
- `MaterialIngestResult:403`
- `SearchFunnelView:411`
- `BrowserTaskView:426`
- `StoredJobView:457`
- `AutomationStats:477`
- `JobStageUpdate:495`
- `DeliveryPreviewView:499`
- `DeliveryTaskStatus:523`
- `DeliveryTaskView:535`
- `DeliveryAuthorizationRequest:560`
- `DeliveryAuthorizationView:565`

### core/replies.py

[agent/app/core/replies.py:1](../agent/app/core/replies.py#L1)

- `tool_label:40`
- `tool_reply:48`
- `has_internal_data:232`
- `public_reply:236`

### core/request_summary.py

[agent/app/core/request_summary.py:1](../agent/app/core/request_summary.py#L1)

- `is_request_summary:7`
- `is_search_summary:16`
- `requested_search_summary:35`
- `search_summary:65`
- `request_context:82`
- `request_summary:107`

### core/result_groups.py

[agent/app/core/result_groups.py:1](../agent/app/core/result_groups.py#L1)

- `ResultGroup:9 → hard_filter:17`
- `normalize_groups:26`
- `groups_schema:35`
- `grouped_filter:43`
- `group_verdicts:50`

### core/salary_conditions.py

[agent/app/core/salary_conditions.py:1](../agent/app/core/salary_conditions.py#L1)

- `SalaryPredicate:11 → valid_bounds:31`
- `salary_facts:41`
- `judge_salary:65`
- `normalized_search_filters:106`

### core/search_contract.py

[agent/app/core/search_contract.py:1](../agent/app/core/search_contract.py#L1)

- `numeric_value:12`
- `missing_search_message:42`
- `browser_search_keyword:47`
- `simple_search_patch:52`
- `safe_role_keyword:93`
- `needs_search_reasoning:117`
- `downstream_condition_scopes:126`
- `search_constraint_text:159`
- `explicit_search_constraints:167`
- `monthly_salary_lower_bound:232`

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
- `understand_sources:156`
- `understand:191`
- `command_grounded:195`
- `retained_reference:199`
- `retained_tools:211`
- `passive_positive:229`
- `condition_mode:237`
- `binding_for:250`
- `binding_valid:256`

### delivery/__init__.py

[agent/app/delivery/__init__.py:1](../agent/app/delivery/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### delivery/automation.py

[agent/app/delivery/automation.py:1](../agent/app/delivery/automation.py#L1)

- `UnsupportedDeliverySite:18`
- `DeliveryAutomationService:29 → current:55, reload_persisted_state:58, supports:62, create_preview:65, authorize_and_start:87, resume_authorized_run:113, start:129, resume:145, stop:178, shutdown:200`

### delivery/mock_site_adapter.py

[agent/app/delivery/mock_site_adapter.py:1](../agent/app/delivery/mock_site_adapter.py#L1)

- `MockApplyObservation:14`
- `MockSubmitResult:27`
- `MockResumeBinding:37`
- `MockJobSiteAdapter:44 → normalize_job_url:57, inspect:78, submit_button:155, auxiliary_confirmation:168, prepare:256, bind_resume:278, submit:337`

### delivery/outreach.py

[agent/app/delivery/outreach.py:1](../agent/app/delivery/outreach.py#L1)

- `job_digest:18`
- `build_outreach:64`
- `OutreachService:94 → draft:99`

### delivery/reconciliation.py

[agent/app/delivery/reconciliation.py:1](../agent/app/delivery/reconciliation.py#L1)

- `resume_fingerprint:13`
- `judge_receipt:22`
- `read_receipt:54`

### delivery/resume_binding.py

[agent/app/delivery/resume_binding.py:1](../agent/app/delivery/resume_binding.py#L1)

- `mock_resume_payload:8`

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

### interviews/__init__.py

[agent/app/interviews/__init__.py:1](../agent/app/interviews/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### interviews/service.py

[agent/app/interviews/service.py:1](../agent/app/interviews/service.py#L1)

- `InterviewQuestionSelection:34`
- `InterviewSelection:42`
- `job_digest:47`
- `candidate_documents:52`
- `InterviewPreparationService:139 → generate:162, get:248, list:278`

### jobs/__init__.py

[agent/app/jobs/__init__.py:1](../agent/app/jobs/__init__.py#L1)

无公开类/函数定义；可能为包入口、常量或内部实现。

### jobs/analysis.py

[agent/app/jobs/analysis.py:1](../agent/app/jobs/analysis.py#L1)

- `analyze_job:107`

### jobs/application_export.py

[agent/app/jobs/application_export.py:1](../agent/app/jobs/application_export.py#L1)

- `ApplicationRows:19 → application_export_rows:20`
- `ApplicationExport:24`
- `export_applications:198`

### jobs/candidate_facts.py

[agent/app/jobs/candidate_facts.py:1](../agent/app/jobs/candidate_facts.py#L1)

- `match_facts:9`
- `verify_requirements:34`
- `qualification_status:60`

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
- `JobStore:53 → create_task:243, update_task:256, get_task:273, latest_task:280, mock_workflow_settings:285, update_mock_workflow_settings:295, advance_daily_schedule:332, bind_mock_applicant:339, claim_daily_mock_run:345, record_daily_mock_run:356, release_daily_mock_run:365, record_daily_mock_report:373, record_mock_workflow_error:377, increment_funnel:427, checkpoint:440, event:458, add_conversation_message:488, conversation_history:506, search_conversation_history:531, create_delivery_run:543, bump_delivery_attempts:560, get_delivery_run:574, latest_delivery_run:581, update_delivery_run:586, authorize_delivery_run:609, mark_delivery_failed:661, reconcile_incomplete_runs:670, upsert_job:732, admit_search_result:788, get_job_from_connection:842, list_jobs:845, application_export_rows:867, job_id_for_source_url:901, update_job_filter_result:906, search_source:911, refilter_candidates:922, save_refilter_results:938, save_search_request:1007, search_recovery:1013, mark_search_processed:1023, job_touched_by_task:1031, reconcile_search_counts:1037, search_report:1048, stats:1177, insights:1214, resume_analysis_evidence:1372, jobs_for_resume_analysis:1492, enqueue_mock_deliveries:1501, record_authorization_audit:1540, authorization_audit:1549, authorization_audit_summary:1563, record_automation_decision:1590, automation_decisions:1604, mark_job_needs_review:1619, all_delivery_runs:1631, deliveries_to_reconcile:1638, record_delivery_reconciliation:1645, delivery_runs_failed_before_submit:1676, delivery_runs_waiting_reply:1689, mock_delivery_queue:1703, claim_mock_delivery:1729, mark_job_retrying_delivery:1734, update_mock_delivery:1745, update_stage:1755, remove_selection:1761, review_selection:1786, review_decisions:1799, review_fingerprint:1806, facts_fingerprint:1813, freeze_delivery_eligibility:1832, assert_delivery_queue_facts:1862, get_job:1891, latest_delivery_status_for_job:1898, create_delivery_preview:1906, get_delivery_preview_job_id:2001, delivery_outreach:2008, authorize_delivery:2045, consume_delivery_authorization:2088, mark_delivery_waiting_reply:2111, assert_delivery_facts:2145, mark_recruiter_reply:2154, mark_delivery_completed:2166, mark_outcome_indeterminate:2174`

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

- `DeepSeekProvider:15 → generate:36, generate_with_tools:83, generate_with_tools_stream:134`

### llm/manager.py

[agent/app/llm/manager.py:1](../agent/app/llm/manager.py#L1)

- `LLMManager:15 → generate:59, provider_for_consent:76, generate_with_tools:82`

### llm/mock_provider.py

[agent/app/llm/mock_provider.py:1](../agent/app/llm/mock_provider.py#L1)

- `MockLLMProvider:7 → generate:13, generate_with_tools:23`

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
- `LLMEvidencePayload:39`
- `CandidateProfileExtractor:337 → extract:341`
- `CandidateEvidenceExtractor:396 → extract:401`
- `CandidateStateStore:467 → read:502, apply_resume_profile:511, remove_resume_profile:554, apply_evidence:602, update_confirmed:654, confirm_search_query:680, reset_search_keywords_from_resume:691, remove_search_terms:707, update_resume_draft:721, sync_approved_resume_skills:731, render_context:759, render_resume_draft:786`

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

### memory/resume_identity.py

[agent/app/memory/resume_identity.py:1](../agent/app/memory/resume_identity.py#L1)

- `resume_stamp:4`

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
- `DialogueState:73 → discard_retired_state:96`
- `topic_for:113`
- `trace:133`
- `apply_operation:143`
- `resolve_reference:351`
- `pending_action:439`
- `slot_answer_patch:449`
- `compact_state:478`
- `bounded_data:499`
- `assemble_context:520`
- `compact_model_messages:525`

### runtime/evaluation_reports.py

[agent/app/runtime/evaluation_reports.py:1](../agent/app/runtime/evaluation_reports.py#L1)

- `read_file:23`
- `value:33`
- `metrics_for:41`
- `catalog:53`
- `report_detail:77`

### runtime/history_reset.py

[agent/app/runtime/history_reset.py:1](../agent/app/runtime/history_reset.py#L1)

- `ResetBusy:23`
- `HistoryResetter:27 → mock_available:35, assert_idle:45, safe_path:57, clear:71`

### runtime/job_references.py

[agent/app/runtime/job_references.py:1](../agent/app/runtime/job_references.py#L1)

- `JobReference:10`
- `UnresolvedQualifier:20`
- `qualifier_matches:26`
- `selector_schema:42`
- `display_reference:47`
- `job_reference:54`
- `instruction_reference:79`

### runtime/ledger.py

[agent/app/runtime/ledger.py:1](../agent/app/runtime/ledger.py#L1)

- `number:14`
- `usage_tokens:18`
- `call_cost:28`
- `execution_ledger:50`

### runtime/manager.py

[agent/app/runtime/manager.py:1](../agent/app/runtime/manager.py#L1)

- `AgentRuntime:80 → start:97, shutdown:127, schedule:140, submit:148, track_delivery:182, track_browser:190, delivery_owner:198, finish:205, emit:222, save:266, action_snapshot:278, authorize_action:305, decide_approval:361, choose_job_target:366, resolve_approval:456, wait_response:480, search_state:493, execute:500, watch:659, watch_plan:707, publish_plan_answer:892, complete_workflow:934, watch_delivery:1003, observe_browser:1024, observe_delivery:1035, recover_search_summaries:1060, browser_summary:1075, cancel:1098, stop_all:1122, stop_status:1126, stop_tasks:1136, active_plan:1261, revise_plan:1272, plan_for_revision:1351, continue_task:1360, resume:1469, stream:1499`

### runtime/memory_context.py

[agent/app/runtime/memory_context.py:1](../agent/app/runtime/memory_context.py#L1)

- `compile_context:15`
- `context_json:81`

### runtime/models.py

[agent/app/runtime/models.py:1](../agent/app/runtime/models.py#L1)

- `SessionCreate:8`
- `RunCreate:12 → valid_message:19`
- `ApprovalDecision:25`
- `DailyTestRequest:32`
- `TargetChoiceDecision:37`
- `public_run:46`

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

### runtime/retired_features.py

[agent/app/runtime/retired_features.py:1](../agent/app/runtime/retired_features.py#L1)

- `retire_growth_steps:7`

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

### workflow/daily.py

[agent/app/workflow/daily.py:1](../agent/app/workflow/daily.py#L1)

- `DailyWorkflow:23 → records:33, info:36, owns_job:44, recover:49, shutdown:66, resume:72, cancel:85, enqueue:101, materialize_due:108, tick:140, save:159, link_search:162, publish_search_summary:175, publish_waiting_summary:207, finish:218, snapshot:229, execute:245, observe:317, search:337`

### workflow/daily_delivery.py

[agent/app/workflow/daily_delivery.py:1](../agent/app/workflow/daily_delivery.py#L1)

- `SubmissionRevoked:16`
- `evidence:20`
- `deliver:41`

### workflow/delivery_rules.py

[agent/app/workflow/delivery_rules.py:1](../agent/app/workflow/delivery_rules.py#L1)

- `build_rules:21`

### workflow/delivery_scope.py

[agent/app/workflow/delivery_scope.py:1](../agent/app/workflow/delivery_scope.py#L1)

- `remember_selection_scope:15`
- `materialize_delivery_scope:25`

### workflow/execution.py

[agent/app/workflow/execution.py:1](../agent/app/workflow/execution.py#L1)

- `persist:24`
- `render_step:41`
- `step_receipt:46`
- `completion_steps:52`
- `completion_lines:99`
- `advance:104`
- `begin:284`

### workflow/failure_recovery.py

[agent/app/workflow/failure_recovery.py:1](../agent/app/workflow/failure_recovery.py#L1)

- `recovery_decision:17`
- `recover_failed_step:40`

### workflow/graph.py

[agent/app/workflow/graph.py:1](../agent/app/workflow/graph.py#L1)

- `wait_for_scope:14`
- `depend_on:21`
- `bind_evidence:36`
- `deduplicate_mutations:68`

### workflow/lifecycle.py

[agent/app/workflow/lifecycle.py:1](../agent/app/workflow/lifecycle.py#L1)

- `input_waits:11`
- `successful_receipt:31`
- `activate_staged_searches:42`

### workflow/mock_coordinator.py

[agent/app/workflow/mock_coordinator.py:1](../agent/app/workflow/mock_coordinator.py#L1)

- `MockWorkflowCoordinator:36 → settings:72, start:75, stop:81, observe:123, remember_authorization_scope:193, queue_deliveries:214, remove_jobs:416, cancel_delivery_queue:445, recover_failed_starts:480, check_reply_queue:530, process_delivery_queue:594, sync_once:676, reconcile_uncertain_deliveries:753, run_daily_if_due:848`

### workflow/observations.py

[agent/app/workflow/observations.py:1](../agent/app/workflow/observations.py#L1)

- `board_facts:16`
- `site_facts:42`
- `observe:77`

### workflow/planning.py

[agent/app/workflow/planning.py:1](../agent/app/workflow/planning.py#L1)

- `revoked:34`
- `Strict:38`
- `RequestDraft:42`
- `live_request:53`
- `pending_slot_answer:76`
- `source_search_constraints:83`
- `verified_slot:97`
- `shortcut_safe:133`
- `plan_request:147`
- `contextual_draft:286`
- `validate_draft:291`

### workflow/presentation.py

[agent/app/workflow/presentation.py:1](../agent/app/workflow/presentation.py#L1)

- `Section:45`
- `Answer:53`
- `Presentation:59`
- `compose:173`

### workflow/recovery.py

[agent/app/workflow/recovery.py:1](../agent/app/workflow/recovery.py#L1)

- `RecoveryDecision:14`
- `reconcile_receipts:20`
- `recover_plan:51`

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
- `edited_plan:39`

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

### workflow/scope_review.py

[agent/app/workflow/scope_review.py:1](../agent/app/workflow/scope_review.py#L1)

- `salary_examples:23`
- `ScopeVerdict:45`
- `ScopeReview:54`
- `proposals:59`
- `review:82`
- `apply_review:146`

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
