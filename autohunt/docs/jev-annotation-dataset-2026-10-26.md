# Jev独立标注数据集与配置入口

本轮按用户要求新建三档相关性数据集；保留工作区已有的五档 `ranking_v2` 及其并行改动，不将其重命名、覆盖或混入本轮提交。

## 已完成

- [冻结虚构语料](../agent/evaluation/corpora/jev-ranking-v1.json)：15个候选人、300个配对，五个职业方向，每池20个同职业岗位。样本重点比较相关经验与总经验、项目原型/生产交付、参与/独立/牵头职责、规模迁移、优先条件、信息缺失和矛盾。文本全部为新编虚构材料。
- 作者设计目标涵盖0/1/2及未定难例，初始标签全部null。设计分不是Gold，且不发送给Jev。最终是否每池真的出现0/1/2，要在标注后检查，不强制改答案。
- 候选人级开发/测试隔离为8/7份；开发集64条盲审任务已生成，两位审核者和仲裁栏为空。它们尚未由人标注。
- [Jev标注器](../agent/evaluation/jev_labels/runner.py)：对接官方固定HTTP地址，版本固定为jev-1.14.0；每对一次请求，含相关性、硬条件、技能、项目与出处等10个Choice问题。保存概率、置信度、证据行、输入/响应摘要及用量。
- [配置模板](../agent/.env.example)新增JEV_API_KEY、JEV_MODEL和JEV_TIMEOUT_SECONDS；实际Key填到本地后端.env，不修改生产模型配置，不进入前端或Git。
- 持久物理调用预算、无Key/许可零调用、限流/网络有界重试、认证/配置错误停止、连续失败阈值、独占锁和从已保存响应恢复。批量数据只来自冻结合成语料，不读取用户简历/聊天，不操作招聘工具。

## 实际状态

用户已批准本轮最多330次物理Jev请求，失败和重试计入。检查到本机没有已配置的Jev Key；实际执行标注入口后状态为 **awaiting_api_key**。

**目前真实请求0/330，Jev已标注0/300。没有Jev准确率、标签分布或NDCG成绩。** 不使用模拟响应、作者设计分或其它模型补成“已标注”。Key填好后复用同一个运行目录继续，不重置预算。

本地路径：`autohunt/agent/evaluation/results/jev-labels/session-20261026-v1/`。包括manifest、budget、summary、空annotations和64条human-calibration；全部在Git忽略目录。

## 验证

- 17项新接口契约通过：冻结语料、同职业设计覆盖、候选人隔离、原文出处、设计信息不外发、类型化响应、矛盾待复核、缺Key/许可、重试计数、认证/配置停止、版本变化、独占与断点恢复。HTTP使用MockTransport，无真实模型调用。
- 17项既有评测/指标契约通过，新增330上限仅供Jev runner显式使用；旧DeepSeek runner默认300上限未放宽，也没有重置旧账本。
- 代码编译与语料可复现性检查通过。这些是工程契约，不是Jev标注质量评测。

## 使用

在本地 `autohunt/agent/.env` 仅添加/填写以下字段，不覆盖现有配置：

```dotenv
JEV_API_KEY=
JEV_MODEL=jev-1.14.0
JEV_TIMEOUT_SECONDS=45
```

在 `autohunt/agent` 中继续本轮已授权标注：

```powershell
.venv/Scripts/python.exe -m evaluation.jev_labels.runner annotate --output evaluation/results/jev-labels/session-20261026-v1 --models-consented --call-limit 330
```

API Key不要发到聊天或提交Git。未来另开新数据批次仍需新的目的地、数据范围和预算许可。

完整协议、输出、银标/人工Gold的区别与复核方法见[模块说明](../agent/evaluation/jev_labels/README.md)。协议依据[TypeSafe官方HTTP文档](https://docs.typesafe.ai/api/system-one)、[Choice](https://docs.typesafe.ai/primitives/choice)和[模型版本说明](https://docs.typesafe.ai/models)，均于2026-10-26核对。
