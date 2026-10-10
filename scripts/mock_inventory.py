"""Append-only, reproducible synthetic jobs for the independent local simulator.

No model calls or real recruitment writes. Requires an explicit database path.
The provenance ledger makes reruns idempotent without editing existing jobs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

BATCH = "autohunt-cross-industry-20261007-v1"
CITIES = (
    "深圳", "上海", "杭州", "武汉", "北京", "广州", "成都", "重庆",
    "南京", "苏州", "天津", "西安", "长沙", "郑州", "合肥", "青岛",
    "宁波", "厦门", "福州", "济南", "东莞", "佛山", "无锡", "南昌",
)

# Category | monthly salary band in K | business contexts | skills |
# six role:role-specific responsibility pairs. All rows are synthetic fixtures.
CATALOG = """
国际贸易|6|45|工业设备出口,消费品出口,海外渠道拓展|商务英语,国际贸易术语,信用证,客户开发,出口报关,CRM|外贸业务员:开发海外客户并跟进询盘报价;国际销售:设计区域客户拓展与回款方案;海外销售:建设海外经销渠道并维护客户;外贸销售主管:管理销售预测与团队业绩复盘;国际业务开发:评估目标市场并推进商务合作;外贸跟单员:跟踪订单生产交付与出口单证
跨境电商|6|42|欧美跨境零售,东南亚电商,独立站业务|Amazon,Shopify,广告投放,选品分析,英语,供应链管理|跨境电商运营:优化店铺商品与转化率;亚马逊运营:管理Listing与站内广告;独立站运营:优化独立站渠道与用户留存;国际销售:开展跨境品牌与渠道商务合作;海外销售:拓展海外分销和品牌客户;外贸业务员:处理跨境B2B询盘与订单交付
金融风控|8|60|消费信贷,小微企业融资,支付反欺诈|SQL,统计分析,信用评分,风险策略,贷后监控,监管合规|信用风险分析师:分析信用风险并监控评分表现;高级风险策略分析师:设计风险准入与额度策略;金融分析师:评估财务表现和风险敞口;反欺诈分析师:识别欺诈行为并复盘拦截效果;模型验证分析师:验证模型稳定性与偏差;贷后风险管理:分析逾期迁徙并优化预警规则
银行与保险|6|48|银行零售业务,商业保险,企业金融服务|金融产品,客户需求分析,Excel,合规审查,资产配置,保险核保|银行客户经理:维护客户关系并提供产品服务;保险核保专员:审核承保材料与风险因素;保险理赔专员:核验理赔资料与处理流程;财富管理顾问:梳理客户需求并比较资产配置方案;金融合规专员:核查业务流程与合规记录;银行运营专员:处理运营对账与服务质量问题
财务与审计|5|40|集团财务,制造业成本管理,企业审计|会计准则,Excel,ERP,财务分析,预算管理,内部控制|财务会计:完成核算与月度关账;成本会计:分析产品成本和差异;财务分析师:分析经营指标与预算偏差;审计专员:执行审计取证和问题跟踪;税务专员:整理税务申报与资料核验;财务经理:统筹预算和财务内控
证券与投资研究|8|65|行业研究,基金投资,机构投资服务|财务建模,行业分析,估值方法,Python,数据终端,研究报告|行业研究员:跟踪行业变化并撰写研究报告;投资分析师:进行标的估值和情景分析;量化研究员:设计策略回测和风险归因;基金运营专员:核对基金交易与估值记录;投资者关系专员:维护机构沟通与信息披露资料;交易支持分析师:分析交易执行和异常对账
人工智能与算法|10|80|智能客服,视觉质检,企业智能体|Python,PyTorch,Transformer,RAG,模型评估,LLM|AI Agent工程师:设计任务规划与工具调用流程;AI算法工程师:训练模型并完成误差分析;大模型应用工程师:实现检索增强和模型服务;计算机视觉工程师:优化目标检测与图像分类;NLP算法工程师:处理文本建模与语义评测;机器学习工程师:构建特征管线和模型上线流程
软件与互联网|8|65|企业SaaS,电商平台,数字化协作|Git,SQL,软件工程,接口设计,自动化测试,Docker|Python后端工程师:开发业务接口与数据服务;Java开发工程师:实现服务端业务与性能优化;前端开发工程师:构建交互页面并优化性能;测试开发工程师:搭建自动化测试与回归流程;产品经理:分析用户需求并管理产品迭代;移动端开发工程师:开发移动应用与稳定性治理
数据与商业分析|7|52|经营分析,增长分析,供应链分析|SQL,Excel,Python,PowerBI,统计分析,数据治理|数据分析师:分析业务指标与异常变化;商业分析师:构建商业问题分析与建议;数据工程师:建设数据管道与质量规则;BI工程师:建设经营报表和分析模型;用户研究员:设计用户访谈并提炼需求;数据治理专员:维护数据标准与质量巡检
网络与信息安全|8|60|企业网络安全,云平台安全,数据安全|Linux,网络协议,访问控制,安全审计,漏洞管理,应急响应|安全工程师:跟踪漏洞治理和防护策略;网络工程师:部署网络设备与故障排查;渗透测试工程师:在授权范围执行安全测试;云安全工程师:检查云配置和访问权限;安全运营分析师:研判告警并处理安全事件;数据安全专员:落实数据分类与权限核查
半导体与电子|8|70|集成电路设计,消费电子,功率器件|电路分析,EDA,电子测量,可靠性测试,PCB,质量管理|芯片设计工程师:设计芯片模块并完成验证;数字验证工程师:建设验证环境和覆盖率;模拟IC工程师:分析模拟电路性能与优化;硬件工程师:设计硬件电路和样机调试;PCB工程师:完成板级布局与信号完整性检查;半导体工艺工程师:分析工艺参数与良率问题
通信与物联网|7|55|无线通信,工业物联网,车联网|通信协议,嵌入式C,网络测试,射频测量,设备管理,数据采集|通信工程师:分析链路性能并优化配置;射频工程师:完成射频电路与指标测试;嵌入式工程师:开发设备固件与驱动;物联网工程师:接入设备数据与平台服务;通信测试工程师:制定协议与场景测试方案;技术支持工程师:定位客户现场通信故障
机械与工业制造|6|42|精密设备,工业自动化,智能工厂|机械制图,SolidWorks,工艺设计,设备调试,质量分析,PLC|机械设计工程师:设计机械结构与验证方案;工艺工程师:优化生产工艺与作业标准;自动化工程师:调试自动化控制与设备联动;设备工程师:分析设备故障与预防维护;制造工程师:改善产线节拍和制造流程;工业工程师:分析现场布局与生产效率
汽车与交通装备|7|60|新能源汽车,智能座舱,汽车零部件|车辆工程,CAN,功能测试,系统设计,质量管理,项目协作|汽车研发工程师:设计车辆子系统与验证流程;电池管理工程师:分析电池控制与保护策略;智能驾驶工程师:开发感知与决策模块;汽车质量工程师:处理车辆质量问题与改进;汽车测试工程师:执行台架和道路验证;汽车项目经理:协调研发节点与风险管理
新能源与电力|6|55|光伏电站,储能系统,电网运维|电气设计,能源管理,安全规范,数据分析,储能技术,设备维护|光伏工程师:设计光伏系统与运行评估;储能工程师:开展储能系统设计与测试;电气工程师:完成电气图纸与设备选型;电力运维工程师:巡检设备并处理运行异常;能源分析师:分析能耗结构与节能方案;新能源项目经理:管理新能源建设进度与风险
化工与新材料|6|48|精细化工,功能材料,高分子材料|化学分析,材料测试,实验设计,工艺优化,实验记录,安全管理|化工工艺工程师:优化工艺参数和生产稳定性;材料研发工程师:设计材料配方与性能验证;分析检测工程师:执行检测方法与结果分析;高分子工程师:研究聚合物结构与应用;实验室技术员:管理实验操作和样品记录;化工安全工程师:排查工艺安全与操作风险
生物医药研发|7|60|创新药研发,生物技术,药物分析|实验设计,生物分析,质量控制,数据记录,文献研究,GMP|生物研发工程师:设计生物实验与数据分析;药物分析研究员:建立分析方法与验证流程;临床研究协调员:协调试验资料与进度记录;药品注册专员:整理注册资料与变更记录;医药质量专员:核查质量记录与偏差处理;生物信息分析师:分析生物数据与研究结果
医疗健康服务|5|45|医疗服务运营,健康管理,医疗器械服务|健康管理,服务沟通,医学基础,质量管理,数据记录,隐私保护|健康管理师:制定健康随访与服务方案;医疗运营专员:改善医疗服务流程和指标;医疗器械工程师:维护器械设备与故障记录;康复服务协调员:协调康复计划与服务安排;医学编辑:整理医学资料与内容核验;医疗客户成功:跟踪机构服务与产品使用
建筑与城市规划|6|48|公共建筑,商业空间,城市更新|AutoCAD,BIM,工程规范,方案设计,预算分析,项目管理|建筑设计师:制定建筑方案和图纸协调;结构工程师:分析结构方案与施工配合;BIM工程师:建立模型与碰撞检查;城市规划师:开展空间研究与规划方案;造价工程师:进行工程计量和成本审核;建筑项目经理:协调施工节点与质量风险
房地产与物业|5|35|商业地产,园区运营,物业服务|客户服务,招商管理,合同管理,设施维护,成本控制,服务标准|招商专员:拓展租户并跟踪招商流程;物业运营专员:协调物业服务与指标改进;商业运营经理:统筹商户运营和活动计划;设施管理工程师:维护设施设备与应急方案;房地产市场分析师:分析市场供需与竞品项目;物业品质专员:检查服务标准与投诉闭环
物流与供应链|5|42|跨境物流,仓配履约,制造业采购|供应链管理,Excel,WMS,ERP,库存分析,供应商管理|供应链分析师:优化库存与交付预测;采购专员:评估供应商与采购合同;物流运营专员:跟踪运输时效与异常处理;仓储管理专员:优化库位与盘点流程;关务专员:核验贸易单证与报关资料;物流规划工程师:设计运输网络与仓配布局
零售与快消|4|35|连锁零售,消费品品牌,渠道经营|门店运营,客户洞察,库存管理,陈列管理,销售分析,活动策划|零售运营专员:分析门店表现与改善措施;渠道销售:拓展经销商与渠道覆盖;商品运营:管理商品组合与销售策略;店长:统筹门店人员和服务质量;快消品牌专员:执行品牌活动与效果分析;品类管理专员:优化品类结构与采购计划
营销与品牌|6|48|品牌增长,企业营销,数字化获客|市场调研,品牌策略,内容策划,广告投放,数据分析,项目协作|市场专员:执行市场活动与线索分析;品牌策划:设计品牌定位与传播方案;广告投放优化师:优化广告预算与转化效果;内容营销专员:策划内容并跟踪线索转化;增长运营:设计增长实验与数据复盘;市场研究员:完成调研分析与竞品报告
销售与商务|5|50|企业软件销售,工业品销售,客户经营|客户开发,商务谈判,CRM,销售预测,方案沟通,回款管理|销售代表:拓展客户与跟进销售机会;大客户经理:维护重点客户与项目协作;商务拓展:评估合作机会并推进协议;销售运营:维护销售数据与预测报表;售前顾问:梳理客户需求并演示解决方案;客户成功经理:提升客户使用效果与续约
教育与培训|5|36|职业教育,企业培训,在线学习|课程设计,学习评估,教学组织,教研,沟通辅导,培训运营|教研专员:设计课程与教学内容;课程顾问:分析学习需求并提供课程建议;培训师:组织培训与学习效果评估;教学运营:维护教学排期和服务质量;学习设计师:设计学习活动与评价标准;教育产品经理:梳理教育产品需求与迭代
法律与合规|6|50|企业法务,知识产权,业务合规|合同审查,法律检索,风险评估,证据整理,合规管理,文书写作|法务专员:审查合同与整理风险清单;知识产权专员:管理知识产权材料与流程;合规分析师:分析业务合规与流程缺口;法律助理:支持资料检索与案件整理;合同管理专员:维护合同台账与履约跟踪;数据合规专员:核查数据处理流程与授权记录
人力资源|5|40|企业招聘,组织发展,人事服务|招聘管理,Excel,人才评估,培训设计,绩效管理,员工沟通|招聘专员:维护招聘流程与候选人沟通;HRBP:支持业务组织与员工发展;薪酬绩效专员:分析薪酬和绩效数据;培训发展专员:设计培训计划与效果跟踪;人事运营:维护人事流程与资料记录;组织发展顾问:分析组织问题与改善方案
行政与企业服务|4|30|办公室服务,企业采购,商务支持|文档管理,沟通协调,采购管理,预算执行,活动组织,办公软件|行政专员:协调日常行政与服务采购;商务助理:支持商务资料与会议安排;总经理助理:跟踪重点事项与沟通协调;企业服务专员:管理服务交付与需求反馈;档案管理专员:整理档案与检索目录;会务专员:组织会议流程和现场协调
文化传媒与出版|5|40|新媒体内容,图书出版,文化传播|内容编辑,采访写作,校对,选题策划,版权管理,传播分析|编辑:策划选题并完成稿件编辑;新媒体运营:维护内容渠道与互动分析;记者:采集采访资料与核查事实;出版策划:设计出版计划与市场定位;版权运营:跟踪版权合作与授权资料;影视制片助理:协调拍摄计划与制作资源
设计与创意|6|45|数字产品设计,品牌视觉,空间展示|Figma,Adobe,设计研究,视觉表达,原型设计,用户体验|UI设计师:设计界面与交互规范;UX设计师:开展用户研究与可用性评估;平面设计师:完成品牌视觉和传播物料;工业设计师:设计产品形态与样机验证;插画师:创作主题插画与视觉元素;空间设计师:设计展示空间与动线方案
游戏与文娱|6|60|移动游戏,互动娱乐,数字内容|游戏设计,Unity,用户分析,内容制作,项目管理,社区运营|游戏策划:设计玩法规则与数值方案;游戏客户端工程师:开发客户端交互与性能优化;游戏美术:制作游戏视觉与资源规范;游戏测试:验证玩法与版本质量;游戏运营:管理活动与用户反馈;音频设计师:设计音效与声音体验
旅游与酒店|4|32|城市酒店,商务旅行,目的地旅游|服务沟通,预订管理,旅游产品,成本管理,客户维护,应急协调|酒店运营:改善酒店服务与经营指标;旅游产品经理:设计线路产品和服务标准;旅行顾问:梳理客户行程与预订需求;收益管理专员:分析预订需求与价格组合;前厅主管:协调接待流程与现场服务;文旅策划:策划旅游活动与传播内容
餐饮与食品|4|32|连锁餐饮,食品生产,团餐服务|食品安全,质量检测,运营管理,成本核算,产品研发,供应商管理|餐饮运营:分析门店运营与服务改进;食品研发工程师:设计食品配方与试制评估;食品质量专员:执行质量检验与问题跟踪;餐饮采购:跟踪食材采购和供应商表现;营养产品专员:梳理营养产品资料与需求;餐饮店长:管理人员排班与服务质量
农业与乡村服务|4|32|现代种植,农业科技,农产品流通|农业技术,田间管理,数据记录,供应链管理,病虫害观察,市场分析|农业技术员:记录田间表现与技术试验;农艺师:制定种植方案与生产指导;农产品运营:跟踪农产品渠道与库存;农业项目专员:协调农业项目进度与材料;育种研究助理:支持育种试验与数据整理;农业设备专员:调试农业设备与维护记录
环境与公共服务|5|38|环境检测,资源回收,城市公共服务|环境监测,数据分析,现场采样,报告编制,安全管理,项目协调|环保工程师:设计环境治理与运行方案;环境检测员:执行采样与检测记录;碳管理分析师:整理碳排放数据与核算依据;水处理工程师:分析水处理工艺与设备运行;资源回收运营:协调回收流程与质量记录;公共服务项目专员:跟踪服务需求与项目交付
质量与安全管理|5|42|生产质量,服务质量,工程安全|ISO9001,质量分析,风险识别,现场审核,流程改进,记录管理|质量工程师:分析缺陷与纠正预防措施;体系审核员:检查质量体系与文件记录;EHS工程师:识别现场环境健康安全风险;供应商质量工程师:跟踪供应商质量与改善;质量数据分析师:分析质量趋势与异常分布;安全管理专员:落实检查与隐患整改跟踪
咨询与管理服务|7|55|战略咨询,运营改善,企业数字化|结构化分析,行业研究,访谈,财务分析,报告写作,项目管理|管理咨询顾问:诊断管理问题并设计改善方案;战略分析师:研究市场机会与战略路径;运营咨询顾问:梳理流程与运营指标;数字化咨询顾问:评估数字化需求与实施方案;研究助理:整理访谈与行业资料;项目管理顾问:协调项目计划与风险跟踪
科研与技术服务|7|55|应用研究,检测认证,技术转移|文献检索,实验设计,数据分析,技术写作,项目申报,质量管理|科研助理:支持实验计划与研究资料;研究工程师:开展技术验证与原型设计;检测认证工程师:执行检测方案与结果评审;技术转移专员:评估技术成果与合作需求;科研项目管理:维护项目节点与经费记录;技术文档工程师:编写技术资料与使用说明
电商与客户服务|4|35|国内电商,企业客服,会员服务|客服沟通,工单管理,数据分析,电商运营,用户反馈,服务质量|电商运营:优化商品和活动转化;用户运营:设计用户分层与留存方案;客服专员:处理咨询与异常工单;客服质检:检查沟通质量和服务合规;会员运营:维护会员权益与活动效果;服务培训师:设计客服培训与技能评估
交通与城市运营|5|38|公共交通,智慧停车,城市配送|调度管理,运行分析,设备维护,安全检查,现场协调,客户服务|交通调度专员:安排运力并处理调度异常;城市运营专员:协调城市服务与运行指标;停车运营经理:优化停车服务与资源配置;运输安全专员:跟踪运输检查与隐患整改;交通数据分析师:分析客流和运行效率;配送规划专员:规划配送路线与时效改善
"""

BENEFITS = (
    "五险一金、双休、带薪年假、年度体检", "五险一金、绩效奖金、餐补、交通补贴",
    "六险一金、弹性工作、补充医疗、培训预算", "五险一金、年终奖、带薪年假、住房补贴",
)
LEVELS = (
    ("实习", "在校/实习", "大专", 0.0), ("应届/校招", "应届毕业生", "本科", 0.14),
    ("社招", "1-3年", "大专", 0.28), ("社招", "3-5年", "本科", 0.45),
    ("资深社招", "5-10年", "本科", 0.66), ("专家社招", "10年以上", "本科", 0.84),
)
LEADERSHIP = ("主管", "经理", "高级", "管理", "店长", "HRBP", "架构师", "专家")


def catalog() -> list[dict]:
    records = []
    for line in CATALOG.strip().splitlines():
        category, low, high, domains, skills, roles = line.split("|")
        records.append(dict(category=category, low=int(low), high=int(high),
                            domains=domains.split(","), skills=skills.split(","),
                            roles=[item.split(":", 1) for item in roles.split(";")]))
    return records


def generate_jobs() -> list[dict]:
    records = []
    # Fixed timestamps and structured keys make the batch reproducible.
    published = datetime(2026, 10, 7, 9, 0)
    for industry_index, industry in enumerate(catalog()):
        for role_index, (role, responsibility) in enumerate(industry["roles"]):
            for city_index, city in enumerate(CITIES):
                for variant in range(2):
                    grade = (city_index + role_index + industry_index + variant * 3) % len(LEVELS)
                    if any(term in role for term in LEADERSHIP):
                        grade = max(3, grade)
                    employment, experience, education, factor = LEVELS[grade]
                    domain = industry["domains"][(city_index + role_index + variant) % len(industry["domains"])]
                    skills = industry["skills"]
                    floor = industry["low"] + round((industry["high"] - industry["low"] - 5) * factor)
                    ceiling = industry["high"] if grade == 5 else min(industry["high"], floor + 3 + grade + variant)
                    benefits = BENEFITS[(city_index + role_index + variant) % len(BENEFITS)]
                    objective = ("在导师指导下完成基础任务与资料核对" if grade < 2 else
                                 "独立完成负责事项并跟踪交付质量" if grade < 4 else
                                 "牵头复杂问题分析、方案评审与团队协作")
                    requirements = [
                        f"{education}学历或同等实践能力，{experience}；具有{industry['category']}相关学习或工作背景。",
                        f"能结合{domain}场景开展{responsibility}，{objective}。",
                        f"能够使用{skills[(role_index + variant) % len(skills)]}与{skills[(role_index + variant + 1) % len(skills)]}完成岗位任务，提供可核验的案例或实践记录。",
                        "能够清楚记录处理过程、解释判断依据，主动核对数据与材料的准确性。",
                        "具备跨团队沟通和保密意识；涉及执业资质或专项授权的任务须先核验资格与权限。",
                    ]
                    if industry["category"] in ("国际贸易", "跨境电商"):
                        requirements.append("能够用商务英语开展邮件与会议沟通，理解报价、交期、贸易术语、订单履约和回款风险。")
                    if grade < 2:
                        requirements.append("接受相关课程项目、实习或实践经历；提供入职培训与明确导师，不要求完整全职经历。")
                    jd = (
                        f"【合成测试职位】本岗位仅用于本地模拟招聘与 Agent 功能评估，不是真实招聘。\n"
                        f"【岗位背景】{industry['category']}行业，业务场景为{domain}；工作地点：{city}。招聘类型：{employment}。\n"
                        f"【岗位职责】\n1. {responsibility}，围绕{domain}制定具体工作计划与交付清单。\n"
                        f"2. {objective}，使用{'、'.join(skills[:3])}整理问题、数据与执行记录。\n"
                        f"3. 与上下游团队核对需求、期限和风险，跟踪{responsibility}相关的处理进度与异常。\n"
                        "4. 定期复盘交付质量、业务效果与返工问题，形成可复用的检查表和改善建议。\n"
                        f"【能力要求】{'、'.join(skills)}；具体资历和职责范围以本岗位等级为准。\n"
                        f"【考核与交付】交付与{role}相关的工作记录、分析报告或执行成果，以准确性、完成时效和业务反馈验收。\n"
                        f"【薪资福利】固定税前月薪 {floor}-{ceiling}K，按12个月计薪；{benefits}。"
                        "奖金或佣金如有则另行说明，不计入月薪下限，不承诺额外收益。\n"
                        f"【工作安排】{city}本地办公，工作时间与必要现场支持提前约定；沟通后可发送简历。"
                    )
                    records.append(dict(
                        key=f"{industry_index:02d}:{role_index}:{city_index:02d}:{variant}",
                        title=f"{role}（{domain}·{employment}）", category=industry["category"],
                        city=city, department=f"{industry['category']}业务组", salary_min=floor,
                        salary_max=ceiling, experience_required=experience, education=education,
                        description=jd, requirements=requirements, skills=list(skills),
                        headcount=1 + (role_index + variant) % 4, status="open",
                        created_at=(published - timedelta(minutes=city_index * 2 + variant)).isoformat(" "),
                    ))
    return records


def append_inventory(database: Path, backup_dir: Path, *, jobs: list[dict] | None = None,
                     batch: str = BATCH) -> dict:
    database = database.resolve(strict=True)
    if not database.is_file() or database.suffix != ".db":
        raise ValueError("必须显式指定已存在的模拟网站 SQLite .db 文件")
    jobs = generate_jobs() if jobs is None else jobs
    if not jobs or len({job['key'] for job in jobs}) != len(jobs):
        raise ValueError("岗位批次必须非空且每个键唯一")
    db = sqlite3.connect(database, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {"jobs", "users", "applications", "messages", "resumes"}
        if not required <= tables:
            raise ValueError("目标数据库不是模拟招聘网站数据库")
        employers = db.execute("SELECT id,COALESCE(company_name,name) company FROM users WHERE role='employer' ORDER BY id LIMIT 30").fetchall()
        if len(employers) != 30 and any('company_name' not in job for job in jobs):
            raise ValueError("需存在原有30个招聘方账号；此脚本不创建、替换或删除账号")
        backup_dir = backup_dir.resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"mock-site-before-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S-%f')}.db"
        with sqlite3.connect(backup) as destination:
            db.backup(destination)
        db.execute("BEGIN IMMEDIATE")
        db.execute("""CREATE TABLE IF NOT EXISTS autohunt_seed_ledger (
            batch TEXT NOT NULL, fixture_key TEXT NOT NULL, job_id INTEGER NOT NULL,
            PRIMARY KEY(batch,fixture_key), UNIQUE(job_id))""")
        if any('metadata' in job for job in jobs):
            # Kept outside jobs/JobOut: never exposed as evidence to the Agent.
            db.execute("""CREATE TABLE IF NOT EXISTS autohunt_inventory_metadata (
                job_id INTEGER PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
                batch TEXT NOT NULL, metadata TEXT NOT NULL)""")
        before = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in required}
        protected = {table: db.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0]
                     for table in tables - {'jobs', 'users', 'autohunt_seed_ledger',
                                             'autohunt_inventory_metadata', 'sqlite_sequence'}}
        original_users = db.execute('SELECT * FROM users ORDER BY id').fetchall()
        existing = {row[0] for row in db.execute("SELECT fixture_key FROM autohunt_seed_ledger WHERE batch=?", (batch,))}
        stale = db.execute("""SELECT COUNT(*) FROM autohunt_seed_ledger l
            LEFT JOIN jobs j ON j.id=l.job_id WHERE l.batch=? AND j.id IS NULL""", (batch,)).fetchone()[0]
        if stale:
            raise RuntimeError("批次台账包含已删除职位，需人工核对后再追加")
        company_accounts = {}
        created_users = 0
        inserted = 0
        for index, job in enumerate(jobs):
            if job["key"] in existing:
                continue
            if 'company_name' in job:
                company = job['company_name']
                if company not in company_accounts:
                    email = f"inventory-{hashlib.sha256(company.encode()).hexdigest()[:24]}@example.invalid"
                    account = db.execute("SELECT id,role,company_name FROM users WHERE email=?", (email,)).fetchone()
                    if account and (account['role'] != 'employer' or account['company_name'] != company):
                        raise RuntimeError("模拟招聘方账号冲突，未修改任何已有账号")
                    if account:
                        company_accounts[company] = account['id']
                    else:
                        cursor = db.execute("""INSERT INTO users (role,name,email,company_name,created_at)
                            VALUES ('employer',?,?,?,?)""", (f'{company}招聘团队', email, company, job['created_at']))
                        company_accounts[company] = cursor.lastrowid
                        created_users += 1
                employer = dict(id=company_accounts[company], company=company)
            else:
                employer = employers[index % len(employers)]
            cursor = db.execute("""INSERT INTO jobs
                (employer_id,title,company_name,department,category,city,salary_min,salary_max,
                 experience_required,education,description,requirements,skills,headcount,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                employer["id"], job["title"], employer["company"], job["department"], job["category"],
                job["city"], job["salary_min"], job["salary_max"], job["experience_required"], job["education"],
                job["description"], json.dumps(job["requirements"], ensure_ascii=False),
                json.dumps(job["skills"], ensure_ascii=False), job["headcount"], job["status"], job["created_at"],
            ))
            db.execute("INSERT INTO autohunt_seed_ledger VALUES (?,?,?)", (batch, job["key"], cursor.lastrowid))
            if 'metadata' in job:
                db.execute("INSERT INTO autohunt_inventory_metadata VALUES (?,?,?)",
                           (cursor.lastrowid, batch, json.dumps(job['metadata'], ensure_ascii=False)))
            inserted += 1
        after = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in required}
        for table in required - {"jobs", "users"}:
            if before[table] != after[table]:
                raise RuntimeError(f"非职位数据被修改：{table}")
        if after['users'] - before['users'] != created_users:
            raise RuntimeError("模拟招聘方新增数量不一致")
        for account in original_users:
            if tuple(db.execute('SELECT * FROM users WHERE id=?', (account['id'],)).fetchone()) != tuple(account):
                raise RuntimeError("已有账号被修改")
        for table, count in protected.items():
            if db.execute('SELECT COUNT(*) FROM "' + table.replace('"', '""') + '"').fetchone()[0] != count:
                raise RuntimeError(f"已有业务数据被修改：{table}")
        if after["jobs"] - before["jobs"] != inserted:
            raise RuntimeError("新增职位数未通过核验")
        db.commit()
        return dict(batch=batch, inserted=inserted, created_employers=created_users,
                    before=before, after=after, backup=str(backup),
                    **inventory_summary(jobs))
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def inventory_summary(jobs: list[dict]) -> dict:
    return dict(planned=len(jobs), industries=len({job["category"] for job in jobs}),
                role_directions=sum(len(item["roles"]) for item in catalog()), cities=len(CITIES),
                salary_min_k=min(job["salary_min"] for job in jobs),
                salary_max_k=max(job["salary_max"] for job in jobs),
                employment=dict(Counter(next(level[0] for level in LEVELS if level[1] == job["experience_required"]) for job in jobs)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="显式指定模拟网站已有数据库")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parents[1] / ".runlogs" / "mock-inventory-backups")
    args = parser.parse_args()
    result = append_inventory(args.database, args.backup_dir) if args.database else inventory_summary(generate_jobs())
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
