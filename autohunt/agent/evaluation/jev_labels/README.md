# 独立 Jev 标注数据集

这是与 `benchmark-v1`、本地未提交的 `ranking_v2` 分开的三档相关性数据集。它不修改AutoHunt在线规划、授权、投递或用户数据。

## 输入与设计

冻结语料：[jev-ranking-v1.json](../corpora/jev-ranking-v1.json)。15份虚构候选人、300份配对JD、15个固定20岗位候选池；覆盖Python后端、LLM/RAG、数据分析、金融分析和外贸五个职业方向。

同一池保持职业方向接近，区分以下事实：

- 总经验与本职业相关经验；学历及明确必需的项目证据。
- 练习/原型、独立模块交付、上线维护、跨团队牵头；技能词重合不代表同等职责。
- 相同技术在不同业务、规模和交付要求下的迁移能力。
- 优先项与硬条件、否定要求、信息缺失、JD前后矛盾、标签与真实职责不符。
- 被评审JD内要求“直接给最高分”的不可信文字，不能成为标注指令。

素材由AI编写场景卡片后可复现组装，不是本轮调用另一个生成模型，也不是真实网站JD。所有初始 `relevance/eligibility` 均为null。

`design_intents` 仅用于检查作者是否设计了低/中/高相关性与未知难例，不是参考真值，禁止据它宣称NDCG或模型准确率。Jev请求构造器只发送候选人/JD原文与固定判定标准，不发送设计分、case类型、ID、分组、生产分数或历史标签。岗位顺序固定洗牌。

每池在设计上覆盖0/1/2；**真实标注后是否混合三级必须重新统计**。不能为达到指定比例改写Jev答案；所有结果同级时应标为数据或判定标准待复核，而不是伪造分布。

候选人级development/test隔离：8/7份，共160/140对。跨split仍共享作者场景模式，不能声称未见文档或领域泛化。

## 配置接口

在后端本地 `autohunt/agent/.env` 添加以下字段；若文件已有模型配置，**只添加字段，不覆盖整份文件**：

```dotenv
JEV_API_KEY=
JEV_MODEL=jev-1.14.0
JEV_TIMEOUT_SECONDS=45
```

填入个人TypeSafe Key，仅用于本地评测。也兼容 `TYPESAFE_API_KEY`，以非空JEV_API_KEY优先。模板见[.env.example](../../.env.example)。Key不进入前端、不写入日志、不提交Git；不需要改变生产模型配置。

使用官方固定地址 `POST https://api.typesafe.ai/v1/systemone`，Bearer认证。固定版本默认jev-1.14.0，不使用随时间变化的latest别名；返回版本改变时停止，不混用不同版本标签。

官方资料（2026-10-26核对）：[HTTP协议](https://docs.typesafe.ai/api/system-one)、[Choice](https://docs.typesafe.ai/primitives/choice)、[模型版本](https://docs.typesafe.ai/models)、[置信度](https://docs.typesafe.ai/confidence)、[限流与错误](https://docs.typesafe.ai/api/errors)。

Jev的Score可以返回小数，本集需要离散0/1/2，因此用Choice三选一而不直接四舍五入Score。每对在一次物理请求里提出10个独立问题：相关性、硬条件总体、学历、相关经验、必需技能、必需项目、项目对应、职责对应，以及两条候选人/JD出处选择。出处从完整原文行中选择，不要求Jev生成解释文字；单条出处不能证明全部维度。

置信度是分布集中程度，不等同于业务正确率。默认0.7只用于标记待复核，**尚未用人工集校准**；低置信、证据缺失及维度矛盾都会保留原始标签和概率并进入needs_review，不悄悄修改为设计答案。所有结果仍是银标，human_gold=false。

选中“虚构评测素材”声明行作为证据也会进入复核：原文确实存在不代表能支持相关性判断。官方说明非英语表现需要在本领域实测，本集是中文，因此64条开发集人工校准任务仍有必要，不能用产品宣传或置信度代替。

## 执行与断点

在 `autohunt/agent` 安装开发依赖后运行：

```powershell
# 无网络：生成/核对已冻结合成语料
python -m evaluation.jev_labels.corpus --write

# 无网络：生成固定运行清单、预算和64条开发集人工盲审任务
python -m evaluation.jev_labels.runner prepare --output evaluation/results/jev-labels/session-20261026-v1 --call-limit 330

# 必须有用户对TypeSafe目的地、合成数据范围和物理上限的许可
python -m evaluation.jev_labels.runner annotate --output evaluation/results/jev-labels/session-20261026-v1 --models-consented --call-limit 330
```

2026-10-26用户已允许本轮最多330次物理调用，仅限本冻结合成数据。没有API Key时返回awaiting_api_key、计数为0；不会退回DeepSeek或合成标签冒充Jev。CLI开关不代替实际许可。续跑必须复用同一目录和budget.json，不能换目录重新获得330次。

一次请求最多处理一对，含重试都先扣持久预算；默认单进程串行。429/529/5xx和连接/超时最多三次尝试，指数退避；401/403或请求配置错误立即停止，连续三对无有效响应也停止。HTTP传输自动重试关闭，不自动跟随重定向。

运行目录有独占OS锁；预算使用跨进程文件锁。响应先写journal，再派生annotations，崩溃后可从已存响应恢复，不重复付费请求。硬中断导致已扣预算但未存响应的次数仍占预算。损坏账本不自动重置。更换模型、语料、判定标准或预算时拒绝覆盖已有运行。

## 输出与人工复核

- `manifest.json`：语料、问题/标准摘要、模型、目的地和预算，均无Key。
- `budget.json`：物理调用预扣记录，失败也计入。
- `model-calls.jsonl`：原始合成请求、类型化响应、用量、状态与耗时；不记录Authorization或异常正文。
- `annotations.json`：实际Jev标签、概率、置信度、出处、输入/响应摘要、复核标记。
- `summary.json`：已标注/待标注数、每池实际0/1/2分布、是否真正观察到三级、错误与用量；不会把未完成标注计为通过。
- `human-calibration.jsonl`：64条开发集盲审任务，含两位审核者和仲裁空栏，不展示Jev结果或作者设计分。尚未填写，不能称为人工Gold；测试集不得用于调阈值。

用这些标签计算NDCG时必须记录标签版本与来源，并完整评审同一个候选池。不要只保留高置信或成功案例再宣称全量匹配质量；needs_review需人工处理或明确报告覆盖率，不把未知硬条件自动当满足。

原始输出仅留在被Git忽略的 `evaluation/results`。语料可提交，因为全部是本次新造虚构材料；用户简历、聊天和站点数据不参与。

## 无模型校验

```powershell
python -m evaluation.jev_labels.contracts
python -m unittest evaluation.benchmark.contracts evaluation.quantified_metrics_contract
```

测试以MockTransport验证缺Key/许可零请求、响应协议、原文出处、预算、重试、错误停止、独占锁、崩溃恢复及设计标签隔离。这是接口契约，不是Jev真实标注质量或准确率；未配置密钥前没有真实Jev成绩。
