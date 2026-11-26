"""Harness result delivery: read-only application snapshots as real XLSX/CSV.

This module projects saved observations. Downloading never performs a model
call, changes job/queue state, reconciles a receipt, or starts recruitment work.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..core.security import CONTACT_FIELDS, SECRET_FIELDS, redact_text


class ApplicationRows(Protocol):
    def application_export_rows(self, *, scope: str = "all") -> list[dict]: ...


@dataclass(frozen=True)
class ApplicationExport:
    content_bytes: bytes
    media_type: str
    filename: str


# Stable column order is shared by both formats. Only explicit business fields
# enter an artifact; source dictionaries and execution payloads are never dumped.
COLUMNS = (
    ("job_id", "岗位ID", 10), ("title", "岗位名称", 32), ("company", "公司", 28),
    ("location", "地点", 20), ("salary", "薪资原文", 20), ("score", "匹配分", 10),
    ("board_stage", "看板阶段", 20), ("board_mode", "处理模式", 22),
    ("on_board", "在当前看板", 14), ("removed", "已移除", 12), ("removed_at", "移除时间", 28),
    ("source", "来源", 18), ("source_url", "岗位来源链接", 56),
    ("communication_status", "沟通状态", 24), ("recruiter_reply_at", "HR回复时间", 28),
    ("queue_status", "投递队列状态", 20), ("delivery_status", "最近投递执行状态", 24),
    ("receipt_result", "最近投递回执结果", 36), ("receipt_application_id", "站点回执ID", 18),
    ("receipt_time", "回执核验时间", 28), ("delivery_started_at", "最近投递开始时间", 28),
    ("delivery_updated_at", "最近投递状态更新时间", 28),
    ("delivery_run_count", "投递执行记录数", 18), ("first_seen_at", "首次记录时间", 28),
)

_STAGES = {"review": "待人工审核", "applying": "沟通中／等待回复", "waiting": "已投递／等待后续",
           "interview": "面试阶段", "rejected": "已结束／不再申请", "outcome_indeterminate": "结果待核验"}
_DELIVERY = {"idle": "未启动", "starting": "正在启动", "running": "执行中", "waiting_for_user": "等待人工接管",
             "waiting_for_reply": "等待HR回复", "completed": "执行已完成", "failed": "执行失败",
             "outcome_indeterminate": "结果待核验", "cancelled": "已取消"}
_QUEUE = {"queued": "已入队", "dispatched": "已调度", "completed": "已完成", "cancelled": "已取消",
          "stale": "绑定或证据已变化", "blocked": "等待人工核验"}
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\ufeff\u200b\ud800-\udfff\ufffe\uffff]")
_SENSITIVE_QUERY = {re.sub(r"[^a-z0-9]", "", key.casefold()) for key in SECRET_FIELDS | CONTACT_FIELDS}


def _text(value) -> str:
    if value is None:
        return ""
    # This is a download-boundary copy, never canonical execution state.
    return _CONTROLS.sub(" ", redact_text(str(value)))


def _source_url(value) -> str:
    """Keep a plain HTTP(S) source, with credentials/contact query fields removed."""
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return ""
        query = [(key, item) for key, item in parse_qsl(parts.query, keep_blank_values=True)
                 if re.sub(r"[^a-z0-9]", "", key.casefold()) not in _SENSITIVE_QUERY]
        return _text(urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), "")))
    except ValueError:
        return ""


def _communication(row: dict) -> str:
    if row.get("has_recruiter_reply"):
        return "已收到HR回复"
    if row.get("delivery_checkpoint") in {"communication_started", "communication_unconfirmed"}:
        return "沟通发送结果待核验"
    if row.get("delivery_status") == "waiting_for_reply" or row.get("board_stage") == "applying":
        return "等待HR回复"
    if row.get("delivery_status") == "waiting_for_user":
        return "等待人工接管"
    if row.get("queue_status") == "queued":
        return "待发起沟通"
    return "未记录沟通状态"


def _receipt(row: dict) -> tuple[str, str]:
    if row.get("delivery_submitted"):
        return "已核实投递", row.get("delivery_reconciled_at") or row.get("delivery_updated_at") or ""
    receipt = row.get("receipt_status")
    if receipt:
        return {"confirmed": "回执显示投递成功，本地状态待同步", "not_observed": "本次未观察到投递记录",
                "conflict": "回执绑定冲突，需人工核验", "unknown": "回执信息不足，需人工核验"}.get(
                    receipt, "回执状态未识别，需人工核验"), row.get("delivery_reconciled_at") or ""
    if row.get("delivery_status") == "outcome_indeterminate":
        return "投递结果待核验", ""
    if row.get("delivery_status") in {"failed", "completed"}:
        return "未确认投递成功", ""
    if row.get("delivery_run_id"):
        return "尚无成功投递回执", ""
    if row.get("board_stage") == "waiting":
        return "看板显示已投递，无本地执行回执", ""
    return "无投递回执", ""


def _receipt_id(value):
    if type(value) is int:
        return value
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_:-]{1,128}", value):
        return _text(value)
    return ""


def _project(row: dict) -> dict:
    result, receipt_time = _receipt(row)
    return {
        "job_id": row.get("job_id"), "title": _text(row.get("title")), "company": _text(row.get("company")),
        "location": _text(row.get("location")), "salary": _text(row.get("salary")), "score": row.get("score"),
        "board_stage": _text(_STAGES.get(row.get("board_stage"), row.get("board_stage"))),
        "board_mode": _text(row.get("board_mode")),
        "on_board": "是" if row.get("board_visible") and not row.get("removed_at") else "否",
        "removed": "是" if row.get("removed_at") else "否", "removed_at": _text(row.get("removed_at")),
        "source": _text(row.get("source")), "source_url": _source_url(row.get("source_url")),
        "communication_status": _communication(row), "recruiter_reply_at": _text(row.get("recruiter_reply_at")),
        "queue_status": _text(_QUEUE.get(row.get("queue_status"), row.get("queue_status"))),
        "delivery_status": _text(_DELIVERY.get(row.get("delivery_status"), row.get("delivery_status"))),
        "receipt_result": result, "receipt_application_id": _receipt_id(row.get("receipt_application_id")),
        "receipt_time": _text(receipt_time), "delivery_started_at": _text(row.get("delivery_started_at")),
        "delivery_updated_at": _text(row.get("delivery_updated_at")),
        "delivery_run_count": row.get("delivery_run_count", 0), "first_seen_at": _text(row.get("first_seen_at")),
    }


def _csv(rows: list[dict]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow([title for _, title, _ in COLUMNS])
    for row in rows:
        values = []
        for key, _, _ in COLUMNS:
            value = row.get(key)
            if isinstance(value, str):
                value = _text(value)
                # Quoting by CSV alone does not stop spreadsheet formula execution.
                if value.lstrip().startswith(("=", "+", "-", "@")):
                    value = "'" + value
            values.append("" if value is None else value)
        writer.writerow(values)
    return output.getvalue().encode("utf-8-sig")


def _xlsx(rows: list[dict]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    if any(isinstance(value, str) and len(value) > 32_767 for row in rows for value in row.values()):
        raise ValueError("导出字段超过 Excel 单元格长度限制，请使用 CSV 格式保留完整内容")
    book = Workbook(write_only=True)
    sheet = None
    for index, row in enumerate(rows or [None]):
        # Excel's sheet row limit includes the header. More data gets another
        # sheet rather than being silently dropped from the complete export.
        if index % 1_048_575 == 0:
            sheet = book.create_sheet("申请列表" if index == 0 else f"申请列表{len(book.worksheets) + 1}")
            sheet.freeze_panes = "A2"
            for column, (_, _, width) in enumerate(COLUMNS, 1):
                sheet.column_dimensions[get_column_letter(column)].width = width
            header = []
            for _, title, _ in COLUMNS:
                cell = WriteOnlyCell(sheet, value=title)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="2463CC")
                header.append(cell)
            sheet.append(header)
        if row is None:
            continue
        cells = []
        for key, _, _ in COLUMNS:
            value = row.get(key)
            if isinstance(value, str):
                value = _text(value)
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"  # Includes leading '=', URL and formula-looking strings.
            cells.append(cell)
        sheet.append(cells)
    content = io.BytesIO()
    book.save(content)
    return content.getvalue()


def export_applications(job_store: ApplicationRows, format: str = "xlsx", scope: str = "all") -> ApplicationExport:
    if format not in {"xlsx", "csv"}:
        raise ValueError("导出格式必须是 xlsx 或 csv")
    if scope not in {"all", "board"}:
        raise ValueError("导出范围必须是 all 或 board")
    rows = [_project(row) for row in job_store.application_export_rows(scope=scope)]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return ApplicationExport(
        content_bytes=_xlsx(rows) if format == "xlsx" else _csv(rows),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if format == "xlsx" else "text/csv; charset=utf-8",
        filename=f"autohunt-applications-{scope}-{stamp}.{format}",
    )
