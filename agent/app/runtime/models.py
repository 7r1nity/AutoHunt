from typing import Literal

from pydantic import BaseModel, Field, model_validator


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


def public_run(run: dict) -> dict:
    # Checkpoints contain model context and are internal, never an HTTP response.
    return {key: value for key, value in run.items() if key not in {"payload", "checkpoint", "request_hash"}} | {
        "checkpoint_phase": (run["checkpoint"] or {}).get("phase"),
        "browser_id": (run["checkpoint"] or {}).get("browser_id"),
        "delivery_id": (run["checkpoint"] or {}).get("delivery_id"),
        "plan_steps": [{key: step.get(key) for key in ("id", "tool", "depends_on", "status", "detail")}
                       for step in ((run["checkpoint"] or {}).get("plan") or {}).get("steps", [])],
        "can_resume": bool(run["status"] in {"waiting_user", "waiting_reply"} or (
            run["status"] in {"interrupted", "timed_out", "failed"} and not run["mutation_started"] and (
                run["kind"] == "learning_plan" or (run["checkpoint"] or {}).get("phase") == "tool_loop"))),
    }
