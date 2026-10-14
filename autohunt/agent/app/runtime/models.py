from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from ..core.security import redact


class SessionCreate(BaseModel):
    title: str = Field(default="新会话", max_length=80)


class RunCreate(BaseModel):
    conversation_id: str = Field(default="default", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    request_id: str = Field(pattern=r"^[a-zA-Z0-9_:-]{1,100}$")
    kind: Literal["chat", "learning_plan"] = "chat"
    message: str = Field(default="", max_length=4000)
    focus: str = Field(default="", max_length=400)

    @model_validator(mode="after")
    def valid_message(self):
        if self.kind == "chat" and not self.message.strip():
            raise ValueError("消息不能为空")
        return self


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    approval_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    approve: StrictBool


class TargetChoiceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    conversation_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    choice_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    step_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    expected_revision: StrictInt = Field(ge=0)
    job_ids: list[StrictInt] = Field(min_length=1, max_length=200)


def public_run(run: dict) -> dict:
    # Checkpoints contain model context and are internal, never an HTTP response.
    return redact({key: value for key, value in run.items() if key not in {"payload", "checkpoint", "request_hash"}} | {
        "checkpoint_phase": (run["checkpoint"] or {}).get("phase"),
        "browser_id": (run["checkpoint"] or {}).get("browser_id"),
        "delivery_id": (run["checkpoint"] or {}).get("delivery_id"),
        "plan_steps": [{key: step.get(key) for key in ("id", "tool", "depends_on", "status", "detail")}
                       for step in ((run["checkpoint"] or {}).get("plan") or {}).get("steps", [])],
        "approvals": [{"id": step["approval_id"], "tool": step["tool"], "arguments": step["arguments"],
                       "detail": step.get("detail", "请核对本次动作范围")}
                      for step in ((run["checkpoint"] or {}).get("plan") or {}).get("steps", [])
                      if step.get("status") == "waiting_approval" and step.get("approval_id")],
        "target_choices": [{"id": step["target_choice"]["id"], "step_id": step["id"],
                            "expected_revision": ((run["checkpoint"] or {}).get("plan") or {}).get("revision", 0),
                            "selection_mode": step["target_choice"].get("selection_mode", "single"),
                            "options": [{key: item.get(key) for key in ("id", "title", "company", "location", "salary", "score")}
                                        for item in step["target_choice"]["options"]]}
                           for step in ((run["checkpoint"] or {}).get("plan") or {}).get("steps", [])
                           if not run["cancel_requested"] and run["status"] not in {"completed", "failed", "cancelled", "timed_out", "interrupted", "outcome_indeterminate"}
                           and step.get("status") == "waiting_input" and step.get("target_choice", {}).get("status") == "pending"],
        "can_resume": bool(run["status"] in {"waiting_user", "waiting_reply"} or (
            run["status"] in {"interrupted", "timed_out", "failed"} and not run["mutation_started"] and (
                run["kind"] == "learning_plan" or (run["checkpoint"] or {}).get("phase") == "tool_loop"))),
    })
