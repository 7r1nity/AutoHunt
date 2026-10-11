"""Append synthetic, evidence-varied employer/client jobs to the local simulator.

Dry summary by default; writes only with an explicit existing --database path.
All branding is marked simulated; no real listing text or candidate data is used.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from mock_inventory import append_inventory, generate_jobs, inventory_summary

BATCH = "autohunt-employer-client-20261010-v1"
SOURCES = (
    "https://www.zhaopin.com/jobdetail/CCL1480117890J40820923405.htm",
    "https://www.zhaopin.com/jobdetail/CCL1480117890J40861354105.htm",
    "https://m.zhipin.com/zhaopin/be0931f7dc0e88163nJ83N20/",
)
PROVIDERS = (
    "外企德科数字技术（模拟）", "外企德科人力资源服务（模拟）",
    "启航人力资源（模拟）", "远桥人力资源服务（模拟）",
    "博信人才服务（模拟）", "汇川人力资源（模拟）",
    "嘉诚数字技术（模拟）", "正禾人力资源管理（模拟）",
    "新程人力资源（模拟）", "智汇人才发展（模拟）",
    "海辰企业服务（模拟）", "宏远人才科技（模拟）",
)
TECH_INDUSTRIES = {
    "人工智能与算法", "软件与互联网", "数据与商业分析", "网络与信息安全",
    "半导体与电子", "通信与物联网", "汽车与交通装备",
}
SCENARIOS = (
    "client_contract", "client_location_only", "dual_management", "recruitment_only",
    "own_team", "brand_skill_only", "relationship_unspecified", "fixed_project",
)
CONVERSION = (
    ("possible", "实习结束后可参加留任评估，是否录用以考核结果和当期岗位名额为准。"),
    ("none", "本次仅招募暑期项目成员，项目结束即结束实习，不安排后续留任评估。"),
    ("unknown", "实习至少持续三个月，每周到岗四天；后续安排在面试阶段沟通。"),
)


def generate_relationship_jobs() -> list[dict]:
    jobs = []
    published = datetime(2026, 10, 10, 9, 0)
    # One of the previous two variants: all 40 industries, 240 roles, 24 cities.
    for base in generate_jobs():
        industry_index, role_index, city_index, variant = map(int, base['key'].split(':'))
        if variant:
            continue
        serial = len(jobs)
        scenario = SCENARIOS[(industry_index + role_index + city_index) % len(SCENARIOS)]
        provider = PROVIDERS[(industry_index + role_index * 3 + city_index) % len(PROVIDERS)]
        client = ("华为技术（模拟客户）" if base['category'] in TECH_INDUSTRIES else
                  f"星海{base['category']}集团（模拟客户）")
        own_company = f"星海{base['category']}（模拟）"
        role = base['title'].split('（', 1)[0]
        company = provider
        employer_subject = provider
        relationship = "unknown"
        evidence = ""
        if scenario == 'client_contract':
            relationship = "client_assignment"
            evidence = f"劳动合同签署主体为{provider}，日常工作在{client}园区开展，由客户项目负责人安排交付事项。"
            arrangement = f"客户公司：{client}。{evidence}薪酬和社保手续由合同签署主体办理。"
        elif scenario == 'client_location_only':
            arrangement = f"工作地点：{client}所在园区。招聘团队负责安排面试，合同签署与组织归属在录用阶段说明。"
        elif scenario == 'dual_management':
            relationship = "client_assignment"
            evidence = f"入职后与{provider}签署劳动合同，项目日常事项向{client}业务负责人汇报，薪酬与员工手续由{provider}办理。"
            arrangement = f"{evidence}项目排班以客户团队约定为准。"
        elif scenario == 'recruitment_only':
            relationship = "direct"
            employer_subject = client
            evidence = f"招聘团队仅协助面试预约；录用后劳动合同直接与{client}签订，薪资社保由{client}办理。"
            arrangement = f"招聘协作单位：{provider}。{evidence}"
        elif scenario == 'own_team':
            relationship = "direct"
            # Include HR-company own staff: company name alone must not exclude.
            company = provider if serial % 3 == 0 else own_company
            employer_subject = company
            evidence = f"岗位属于{company}自有团队，劳动合同、薪资社保及日常汇报均由本公司负责，不安排到客户项目工作。"
            arrangement = evidence
        elif scenario == 'brand_skill_only':
            relationship = "direct"
            company = own_company
            employer_subject = company
            evidence = f"岗位属于{company}自有团队，劳动合同及薪资社保由本公司负责。"
            brand = ("了解华为云、鸿蒙或网络设备的应用场景者优先；本岗位负责本公司的产品与交付。"
                     if base['category'] in TECH_INDUSTRIES else
                     "具有大型企业业务协作经验者优先；知名企业客户经历仅作为经验参考。")
            arrangement = evidence + brand
        elif scenario == 'relationship_unspecified':
            arrangement = f"参与{client}相关业务场景的项目建设；面试包含业务负责人沟通，薪资及入职安排以录用通知为准。"
        else:
            relationship = "client_assignment"
            evidence = f"与{provider}签署劳动合同，服务于{client}本期项目；项目验收后由{provider}讨论下一项目安排。"
            arrangement = f"{evidence}本期计划六至十二个月，办公与项目团队保持一致。"

        description = base['description']
        if scenario in {'client_contract', 'dual_management', 'fixed_project'}:
            description = description.replace("【岗位背景】", f"【项目背景】客户业务：{client}。")
        description += f"\n【团队与入职安排】{arrangement}"
        conversion_status = "not_applicable"
        conversion_evidence = ""
        if base['experience_required'] == '在校/实习':
            conversion_status, conversion_evidence = CONVERSION[(industry_index + role_index + city_index // 3) % 3]
            description += f"\n【实习安排】{conversion_evidence}"
        requirement = (
            f"请准备一个与{role}相关的实践案例，说明任务目标、本人贡献、使用方法和结果；不要求披露原单位保密材料。"
        )
        job = {**base, 'company_name': company, 'description': description,
               'requirements': [*base['requirements'], requirement],
               'created_at': (published - timedelta(seconds=serial)).isoformat(' '),
               'metadata': dict(
                   synthetic=True, scenario=scenario,
                   contract_subject=employer_subject if relationship != 'unknown' else None,
                   client_reference=client if scenario != 'brand_skill_only' else None,
                   visible_relationship=relationship, relationship_evidence=evidence,
                   conversion_status=conversion_status, conversion_evidence=conversion_evidence,
                   reference_patterns=list(SOURCES),
               )}
        # Client branding in selected titles, not a blanket relationship label.
        if base['category'] in TECH_INDUSTRIES and scenario in {'client_contract', 'client_location_only'}:
            job['title'] = f"{role}（华为项目场景·{base['city']}）"
        jobs.append(job)
    validate_inventory(jobs)
    return jobs


def validate_inventory(jobs: list[dict]) -> None:
    for job in jobs:
        visible = json.dumps({key: value for key, value in job.items()
                              if key not in {'metadata', 'key'}}, ensure_ascii=False)
        if any(term in visible for term in ('外包', '第三方', '\ufffd')):
            raise ValueError(f"岗位 {job['key']} 存在直白关系标签或损坏文本")
        if '（模拟）' not in job['company_name'] or '不是真实招聘' not in job['description']:
            raise ValueError("每个公司和 JD 都必须保留模拟标识")
        evidence = job['metadata']['relationship_evidence']
        if evidence and evidence not in job['description']:
            raise ValueError("隐式关系依据必须能在可见 JD 核对")
        if job['salary_min'] > job['salary_max']:
            raise ValueError("薪资区间无效")


def summary(jobs: list[dict]) -> dict:
    return {**inventory_summary(jobs),
            'companies': len({job['company_name'] for job in jobs}),
            'scenarios': dict(Counter(job['metadata']['scenario'] for job in jobs)),
            'visible_relationship': dict(Counter(job['metadata']['visible_relationship'] for job in jobs)),
            'intern_conversion': dict(Counter(job['metadata']['conversion_status'] for job in jobs))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path)
    parser.add_argument('--backup-dir', type=Path, default=Path(__file__).resolve().parents[1] / '.runlogs' / 'mock-inventory-backups')
    args = parser.parse_args()
    jobs = generate_relationship_jobs()
    report = summary(jobs)
    if args.database:
        report = {**append_inventory(args.database, args.backup_dir, jobs=jobs, batch=BATCH), **report}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
