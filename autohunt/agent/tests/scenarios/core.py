"""Minimal test-case model and offline harness for AutoHunt.

Design constraints this file encodes:

* **No pytest.** Automated test sources were removed from this repository by request
  and this suite is restored under a new request, so it must not reintroduce a runner
  dependency. Standard library only.
* **Layers are explicit.** A case declares whether it runs offline, needs the model, or
  needs the mock site. Only the offline layer is expected to run unattended; the others
  cost money or require a browser and are opt-in.
* **Cases assert observable behaviour**, not implementation detail: a parsed command, a
  routing decision, a state transition, a stored row. That keeps them meaningful after
  refactors.
"""
from __future__ import annotations

import asyncio
import inspect
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

#: ``offline`` never touches the network, a model or a database beyond temp files.
#: ``model`` calls the configured LLM provider. ``site`` needs the mock job site and a
#: browser. ``state`` needs a temporary SQLite store.
LAYERS = ("offline", "state", "model", "site")


@dataclass
class Case:
    id: str
    group: str
    layer: str
    description: str
    check: Callable[[], Any]
    tags: tuple[str, ...] = ()
    #: A case that documents a known, unfixed defect. ``expect`` inside it states the
    #: behaviour the code *should* have; ``xfail`` keeps the suite green while the gap
    #: stays visible and counted. Remove the flag when the defect is fixed.
    xfail: bool = False
    #: Set by the runner when the case fails, for the report.
    error: str = ""

    def run(self) -> Any:
        result = self.check()
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        return result


@dataclass
class Result:
    case: Case
    ok: bool
    detail: str = ""
    traceback_text: str = ""

    @property
    def xfailed(self) -> bool:
        """A documented defect that still fails, as expected."""
        return bool(self.case.xfail and not self.ok)

    @property
    def xpassed(self) -> bool:
        """A documented defect that now passes: the flag is stale and must go."""
        return bool(self.case.xfail and self.ok)


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for item in self.results if item.ok)

    @property
    def failed(self) -> int:
        """Real failures only; documented xfails are counted separately."""
        return sum(1 for item in self.results if not item.ok and not item.xfailed)

    @property
    def expected_failures(self) -> list[Result]:
        return [item for item in self.results if item.xfailed]

    @property
    def stale_xfails(self) -> list[Result]:
        return [item for item in self.results if item.xpassed]

    def by_group(self) -> dict[str, tuple[int, int]]:
        groups: dict[str, list[int]] = {}
        for item in self.results:
            counts = groups.setdefault(item.case.group, [0, 0])
            counts[0 if item.ok else 1] += 1
        return {name: (ok, bad) for name, (ok, bad) in sorted(groups.items())}

    def ok(self) -> bool:
        return self.failed == 0


class Registry:
    """Collects cases from scenario modules."""

    def __init__(self) -> None:
        self.cases: list[Case] = []

    def add(self, **kwargs: Any) -> None:
        self.cases.append(Case(**kwargs))

    def extend(self, cases: list[Case]) -> None:
        self.cases.extend(cases)


def expect(condition: Any, message: str = "", detail: Any = None) -> None:
    """Raise with the reason a case failed; the runner records it verbatim.

    ``detail`` is appended when present, so a case can report the value it saw without
    building an f-string for the common path.
    """
    if not condition:
        text = message or "expectation failed"
        if detail is not None:
            text = f"{text}: {detail!r}"
        raise AssertionError(text)


def expect_equal(actual: Any, wanted: Any, message: str = "") -> None:
    if actual != wanted:
        raise AssertionError(f"{message or 'value mismatch'}: 实际 {actual!r}，期望 {wanted!r}")


def expect_in(needle: Any, haystack: Any, message: str = "") -> None:
    """Membership check; ``message`` may be passed positionally or by keyword."""
    if needle not in haystack:
        raise AssertionError(f"{message or 'missing'}: {needle!r} 不在 {haystack!r} 中")


def expect_not_in(needle: Any, haystack: Any, message: str = "") -> None:
    if needle in haystack:
        raise AssertionError(f"{message or 'unexpected'}: {needle!r} 不应出现在 {haystack!r} 中")


def expect_raises(exception: type[BaseException], call: Callable[[], Any], message: str = "") -> BaseException:
    try:
        call()
    except exception as exc:  # noqa: PERF203 - the raise order is the point
        return exc
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(
            f"{message or 'wrong exception'}: 期望 {exception.__name__}，实际 {type(exc).__name__}: {exc}") from exc
    raise AssertionError(f"{message or 'no exception'}: 期望 {exception.__name__}，但没有抛出")


def run(cases: list[Case], *, layers: tuple[str, ...] = ("offline",)) -> Report:
    report = Report()
    for case in cases:
        if case.layer not in layers:
            continue
        try:
            case.run()
            report.results.append(Result(case, True))
        except Exception as exc:  # noqa: BLE001 - a failing case must not stop the suite
            report.results.append(Result(case, False, f"{type(exc).__name__}: {exc}",
                                         traceback.format_exc(limit=6)))
    return report


def render(report: Report, *, verbose: bool = False) -> str:
    lines: list[str] = []
    for name, (ok, bad) in report.by_group().items():
        lines.append(f"  {name:<26} 通过 {ok:>3}  失败 {bad:>3}")
    lines.append(f"  {'合计':<24} 通过 {report.passed:>3}  失败 {report.failed:>3}"
                 f"  已知缺陷 {len(report.expected_failures):>2}")
    if report.failed:
        lines.append("")
        lines.append("  失败用例：")
        for item in report.results:
            if item.ok or item.xfailed:
                continue
            lines.append(f"    [{item.case.id}] {item.case.description}")
            lines.append(f"        {item.detail}")
            if verbose and item.traceback_text:
                for row in item.traceback_text.strip().splitlines()[-4:]:
                    lines.append(f"        {row.strip()}")
    if report.expected_failures:
        lines.append("")
        lines.append("  已知缺陷（xfail，尚未修复）：")
        for item in report.expected_failures:
            lines.append(f"    [{item.case.id}] {item.case.description}")
            lines.append(f"        实际：{item.detail}")
    if report.stale_xfails:
        lines.append("")
        lines.append("  ⚠ 以下 xfail 用例已通过，请移除其 xfail 标记：")
        for item in report.stale_xfails:
            lines.append(f"    [{item.case.id}] {item.case.description}")
    return "\n".join(lines)
