"""Run the AutoHunt scenario suite.

Usage (from the `agent/` directory, with the project venv):

    .venv/Scripts/python.exe tests/run_scenarios.py                 # offline layer only
    .venv/Scripts/python.exe tests/run_scenarios.py --layer state   # add temp-DB cases
    .venv/Scripts/python.exe tests/run_scenarios.py --layer model   # calls the LLM provider
    .venv/Scripts/python.exe tests/run_scenarios.py --layer site    # needs the mock site
    .venv/Scripts/python.exe tests/run_scenarios.py --list          # list cases, do not run
    .venv/Scripts/python.exe tests/run_scenarios.py --group 单轮     # filter by group prefix

Layers are opt-in on purpose: only `offline` is free and unattended. The mock site must
never be driven by a test that writes unless the site layer is requested explicitly.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
AGENT_ROOT = HERE.parent.parent
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

# Tests must never touch the real data root: a case that writes would corrupt the
# user's board, and JobStore runs schema migrations on construction.
os.environ.setdefault("AUTOHUNT_DATA_ROOT", str(HERE.parent / ".tmp-data-root"))

from scenarios import (  # noqa: E402
    automation_monitoring,
    cross_industry,
    delivery_rules,
    jd_scoring,
    multi_turn,
    site_end_to_end,
    single_turn,
)
from scenarios.core import LAYERS, render, run  # noqa: E402

BUILDERS = (
    single_turn.build,
    multi_turn.build,
    cross_industry.build,
    jd_scoring.build,
    delivery_rules.build,
    automation_monitoring.build,
    site_end_to_end.build,
)


def collect() -> list:
    cases = []
    for builder in BUILDERS:
        cases.extend(builder())
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise SystemExit(f"重复的用例 id：{case.id}")
        seen.add(case.id)
    return cases


def main() -> int:
    parser = argparse.ArgumentParser(description="AutoHunt 场景测试集")
    parser.add_argument("--layer", action="append", choices=LAYERS, default=None,
                        help="要运行的层，可重复；默认只跑 offline")
    parser.add_argument("--group", default=None, help="只跑组名包含该子串的用例")
    parser.add_argument("--id", default=None, help="只跑 id 匹配该前缀的用例")
    parser.add_argument("--tag", default=None, help="只跑含该标签的用例")
    parser.add_argument("--list", action="store_true", help="只列出用例，不执行")
    parser.add_argument("--verbose", action="store_true", help="失败时打印堆栈片段")
    args = parser.parse_args()

    cases = collect()
    if args.group:
        cases = [case for case in cases if args.group in case.group]
    if args.id:
        cases = [case for case in cases if case.id.startswith(args.id)]
    if args.tag:
        cases = [case for case in cases if args.tag in case.tags]

    if args.list:
        print(f"共 {len(cases)} 个用例：")
        for case in cases:
            print(f"  [{case.layer:<7}] {case.id:<7} {case.group:<24} {case.description}")
        return 0

    layers = tuple(args.layer) if args.layer else ("offline",)
    runnable = [case for case in cases if case.layer in layers]
    skipped = len(cases) - len(runnable)
    print(f"运行层：{', '.join(layers)}；执行 {len(runnable)} 个用例，跳过 {skipped} 个（未请求的层）")
    report = run(runnable, layers=layers)
    print(render(report, verbose=args.verbose))
    print()
    print("全部通过。" if report.ok() else f"存在 {report.failed} 个失败用例。")
    return 0 if report.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())
