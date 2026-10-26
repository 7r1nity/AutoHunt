# Jev标注与AutoHunt测评：当前断点

本轮数据保持 `jev-ranking-v1` 不变，SHA-256 为 `9e8a8b5cd9aae64072e49f877026d6b2c287f81c21e4bec3272ccdfa2e12b413`。共15份虚构候选人、300个同职业配对；原始标签仍未填充，不以作者设计分替代独立标注。

## 当前状态

用户选择“先保留断点”。所有外部调用已停止，未购买额度、未执行招聘操作。

| 阶段 | 实际结果 | 预算 |
| --- | --- | --- |
| Jev独立标注 | 0/300有效标签，当前停在402额度不足 | 2/330次，剩328次 |
| AutoHunt独立预测 | 300/300完成，300次观察到真实模型响应 | DeepSeek300/300次 |
| Precision/Recall/NDCG | 待独立参考标签，未计算 | 后续离线评分不需要模型请求 |

DeepSeek预测平均每对2.161秒，p95为2.999秒。输入225,347 tokens、输出90,138 tokens、合计315,485 tokens；这些是处理与调用统计，不是岗位匹配准确率。预测过程没有读取Jev标签，运行期间应用源码未变化。

机器进度见[jev-evaluation-progress-20261026.json](../agent/evaluation/reports/jev-evaluation-progress-20261026.json)。原始输入/响应、密钥和数据库不随此汇总提交。

## 接入差异与失败记录

1. 用户提供的Key属于 `https://jevmodel.org/v1/systemone` 接入服务，原先向 `https://api.typesafe.ai/v1/systemone` 的第一次请求返回401，立即停止；计入1次物理预算。
2. 用户明确提供新Endpoint后，保留旧manifest、失败日志和已消耗次数，迁移至用户地址。没有清零预算或删除失败。
3. 该服务文档规定每次最多8个问题。官方地址版本有10项，因此接入版本省略项目/职责对应两个辅助标签，保留整体相关性、硬条件总体、学历、相关经验、必需技能、必需项目和两条出处；每对仍一次请求。原始候选人/JD和三级判定标准不变。
4. 新地址第二次物理请求返回402，服务含义为本次请求额度不足，未取得有效模型响应。程序立即停止，没有对300对逐条重复报错。

该接入服务自述不是TypeSafe运营，报告会记录实际Endpoint和响应模型，不将它冒称为官方直连。协议依据[该服务文档](https://jevmodel.org/docs/)，对照[TypeSafe官方协议](https://docs.typesafe.ai/api/system-one)。尚未观察到这个接入服务成功的Jev标注响应，不能声称上游模型质量或协议已经通过真实标注验证。

Key已从可提交模板移入本地被忽略的 `agent/.env`，模板JEV_API_KEY恢复为空；不会在日志、前端或提交中暴露Key。

## 恢复步骤

本地配置保留：

```dotenv
JEV_API_KEY=
JEV_MODEL=jev-1.14.0
JEV_TIMEOUT_SECONDS=45
JEV_ENDPOINT=https://jevmodel.org/v1/systemone
```

上面Key空值仅为文档示例；实际本地.env中已有用户提供的Key，不要用此示例覆盖它。

用户补好可用额度并要求继续后，在 `autohunt/agent` 运行：

```powershell
.venv/Scripts/python.exe -m evaluation.jev_labels.runner annotate --output evaluation/results/jev-labels/session-20261026-v1 --models-consented --call-limit 330
```

继续同一目录，从2/330的账本接续；不再需要Endpoint迁移参数，也不重跑DeepSeek。若更换Key，只改本地.env。不要通过新目录重置本轮预算。

标注齐全后离线评分：

```powershell
.venv/Scripts/python.exe -m evaluation.jev_labels.evaluate score --annotations evaluation/results/jev-labels/session-20261026-v1 --predictions evaluation/results/jev-labels/autohunt-evaluation-20261026-v1 --output evaluation/results/jev-labels/evaluation-20261026-v1
```

评分要求完整300对，并回查原始类型化Jev响应、输入/响应摘要、实际Endpoint及预测来源。低置信/矛盾参考不会被悄悄删除；会报告复核覆盖率及未审核银标的局限。主评估使用候选人隔离的7个测试查询，另报开发/全量。同分采用期望排序，不利用作者顺序提高NDCG。

## 工程验证

20项Jev/Endpoint/断点/预测输入契约与17项既有指标契约通过，HTTP使用MockTransport。新增检查包括8问题上限、Endpoint显式迁移不重置预算、成功标签禁止跨服务混用、402立即停止及缺少真实参考标签时拒绝出分。它们不等于真实Jev准确率或完整用户任务E2E。
