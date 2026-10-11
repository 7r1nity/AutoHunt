"""Append 1,500 negotiable and 1,500 daily-paid internship simulator jobs.

An explicit database opts in to writes; otherwise only a reproducible summary.
Monthly columns remain zero for nonmonthly pay: daily pay is never converted to K.
Requires the simulator salary_text/formatSalary extension for correct presentation.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from mock_inventory import BENEFITS, CITIES, LEVELS, append_inventory, catalog

BATCH = "autohunt-negotiable-daily-20261011-v1"
PER_KIND = 1500
DAILY_RATES = tuple(range(150, 2001, 50))
CONVERSION = (
    "实习结束后可参加留任评估，录用取决于考核结果与岗位名额，不保证转正。",
    "本次为阶段性实习，实习结束后不安排转正。",
    "转正安排尚未确定，请在面试阶段核实。",
)


def generate_special_salary_jobs() -> list[dict]:
    industries = catalog()
    jobs = []
    published = datetime(2026, 10, 11, 12, 0)
    for kind in ("negotiable", "daily"):
        for serial in range(PER_KIND):
            industry_index = serial % len(industries)
            industry = industries[industry_index]
            role_index = (serial // len(industries)) % len(industry["roles"])
            role, responsibility = industry["roles"][role_index]
            cycle = serial // (len(industries) * len(industry["roles"]))
            city = CITIES[(cycle + industry_index + role_index * 5) % len(CITIES)]
            domain = industry["domains"][(serial + cycle) % len(industry["domains"])]
            daily = kind == "daily"
            grade = 0 if daily else 1 + (serial // len(industries)) % (len(LEVELS) - 1)
            employment, experience, education, _ = LEVELS[grade]
            low = DAILY_RATES[serial % len(DAILY_RATES)] if daily else None
            high = min(2000, low + (serial % 3) * 50) if daily else None
            salary = (f"实习日薪 {low}-{high}元/天，按实际出勤天数结算，税前，不折算为固定月薪。"
                      if daily else "薪资面议，尚未披露固定月薪区间，需面试核实；不保证达到任何月薪门槛。")
            benefits = ("导师指导、实习证明、工作日餐补" if daily else BENEFITS[serial % len(BENEFITS)])
            objective = ("在导师指导下协助相关资料整理、基础分析与任务验证，不承担管理或独立执业职责。"
                         if daily else "按岗位资历承担相应事项，记录判断依据并核对交付质量。")
            internship = (f"实习3-6个月，每周到岗3-5天，具体时间在面试核实。{CONVERSION[serial % len(CONVERSION)]}"
                          if daily else "入职时间、工作安排与薪酬方案均需面试核实。")
            description = (
                "【合成测试职位】仅用于本地模拟招聘与 Agent 功能评估，不是真实招聘；高日薪样本用于边界测试，不代表市场行情。\n"
                f"【岗位背景】{industry['category']}行业，业务场景：{domain}；城市：{city}；招聘类型：{employment}。\n"
                f"【岗位职责】\n1. {'协助团队' if daily else ''}{responsibility}，整理与{domain}相关的资料与问题。\n"
                f"2. {objective}\n3. 使用{'、'.join(industry['skills'][:3])}记录处理过程，复盘异常与交付成果。\n"
                f"【能力要求】{'、'.join(industry['skills'])}；{'接受相关课程、实践与在校经历' if daily else experience}；涉及资质或授权先核验。\n"
                f"【考核与交付】提供与{role}相关的资料、分析或实践成果，按准确性、完成时效与反馈验收。\n"
                f"【薪资福利】{salary}{benefits}。\n【工作安排】{internship}沟通后可发送简历。"
            )
            jobs.append(dict(
                key=f"{kind}:{serial:04d}", title=f"{role}{'方向实习生' if daily else ''}（{domain}·{employment}）",
                category=industry["category"], city=city, department=f"{industry['category']}业务组",
                salary_min=0, salary_max=0, experience_required=experience, education=education,
                description=description,
                requirements=[objective, f"相关{industry['category']}背景，能够说明真实学习或实践证据。", internship],
                skills=list(industry["skills"]), headcount=1 + serial % 4, status="open",
                created_at=(published - timedelta(seconds=serial)).isoformat(" "),
                metadata=dict(salary_unit=kind, daily_min=low, daily_max=high),
            ))
    return jobs


def summary(jobs: list[dict]) -> dict:
    daily = [job for job in jobs if job["metadata"]["salary_unit"] == "daily"]
    return dict(batch=BATCH, planned=len(jobs),
                kinds=dict(Counter(job["metadata"]["salary_unit"] for job in jobs)),
                industries=len({job["category"] for job in jobs}), cities=len({job["city"] for job in jobs}),
                daily_min=min(job["metadata"]["daily_min"] for job in daily),
                daily_max=max(job["metadata"]["daily_max"] for job in daily),
                daily_rate_levels=len({job["metadata"]["daily_min"] for job in daily}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--backup-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / ".runlogs" / "mock-inventory-backups")
    args = parser.parse_args()
    jobs = generate_special_salary_jobs()
    report = summary(jobs)
    if args.database:
        report = {**append_inventory(args.database, args.backup_dir, jobs=jobs, batch=BATCH), **report}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
