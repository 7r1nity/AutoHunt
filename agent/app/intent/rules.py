"""Rule engine (Layer 0): deterministic intent matching with hard safety bounds.

Rules run on normalized text, are data-driven (regex), carry stable ids and
priorities, and are versioned: the builtin v1 rules can be overlaid by
``<rules_dir>/intent_rules_<version>.json`` files, and the active version is a
single pointer in :class:`RouterStore` so rollback is a pointer switch plus an
audit row.

Safety invariants:
- P0 rules are hard blocks: no overlay file may modify or disable them.
- Negation prefixes ("不要/别/先不/…") suppress action intents so a negated
  command never turns into a side effect.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .normalize import normalize_text, security_text
from .store import RouterStore
from ..core.search_language import parse_search_message, COMMAND_PATTERN

BUILTIN_RULESET_VERSION = 1

# Intents suppressed while the message carries a negation prefix. stop_search is
# deliberately excluded: "别搜了" IS a stop command.
ACTION_INTENTS = frozenset({
    "search_jobs",
    "pause_search",
    "resume_search",
    "update_preferences",
    "job_decision",
    "manage_skills",
    "upload_resume",
    "tailor_resume",
    "llm_settings",
})

_NEGATION_RE = re.compile(r"^(?:不要|别|先不|暂时不|不用|无需)")


@dataclass(frozen=True)
class Rule:
    id: str
    category: str  # P0..P5
    priority: int  # lower runs first
    patterns: tuple[str, ...]  # regexes matched against normalized text
    intent: str
    reason: str = ""  # P0 only: key into the router's block messages


@dataclass(frozen=True)
class RuleMatch:
    rule_id: str
    category: str
    intent: str
    reason: str = ""


def _rules_payload_hash(rules: list[Rule]) -> str:
    canonical = json.dumps(
        [
            {
                "id": rule.id, "category": rule.category, "priority": rule.priority,
                "patterns": list(rule.patterns), "intent": rule.intent, "reason": rule.reason,
            }
            for rule in sorted(rules, key=lambda item: (item.priority, item.id))
        ],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


DEFAULT_RULES: list[Rule] = [
    # ------------------------------------------------------------------ P0 safety
    Rule("p0_injection_ignore", "P0", 0,
         (r"忽略(?:以上|之前|此前|上述)?(?:的)?(?:所有|全部)?指令",),
         "unsafe", "injection"),
    Rule("p0_role_rewrite", "P0", 0,
         (r"你是\s*(?:dan|开发者模式|不受限制|没有限制)",),
         "unsafe", "injection"),
    Rule("p0_system_prompt", "P0", 0,
         (r"system\s*prompt|系统提示词|系统指令",),
         "unsafe", "injection"),
    Rule("p0_trusted_tags", "P0", 0,
         (r"<trusted_context>|<user_request>",),
         "unsafe", "injection"),
    Rule("p0_api_key", "P0", 0,
         (r"(?<![a-z0-9_])sk-[a-z0-9_-]{8,}|bearer\s+[a-z0-9._-]{8,}|ghp_[a-z0-9]{10,}",),
         "unsafe", "credentials"),
    Rule("p0_fernet_key", "P0", 0,
         (r"gAAAA[a-z0-9_-]{20,}",),
         "unsafe", "credentials"),
    Rule("p0_candidate_pii_out", "P0", 0,
         (r"候选人(?:的)?(?:电话|手机号|手机号码|邮箱|地址|联系方式)",),
         "unsafe", "pii_out"),
    Rule("p0_destructive", "P0", 0,
         (r"(?:删除|清空)(?:所有|全部)?(?:职位|数据|数据库|看板|记录)",),
         "unsafe", "destructive"),
    Rule("p0_path_escape", "P0", 0,
         (r"\.\./|\.\.\\|(?:^|\s)(?:[a-z]:\\)|/etc/(?:passwd|shadow)|(?:sandbox\s*(?:逃逸|绕过)|(?:逃逸|绕过)\s*sandbox)",),
         "unsafe", "path_escape"),
    # -------------------------------------------------------- P1 exact commands
    Rule("p1_stop_search", "P1", 10,
         (r"^(?:停止搜索|中断搜索|停止任务|中断任务|取消搜索|取消任务|停止浏览器任务|别搜了|停止自动搜索|结束搜索)$",),
         "stop_search"),
    Rule("p1_pause_search", "P1", 10,
         (r"^(?:暂停搜索|暂停任务|暂停一下|先停一下|停一停|暂停浏览器任务)$",),
         "pause_search"),
    Rule("p1_resume_search", "P1", 10,
         (r"^(?:继续搜索|继续任务|接着搜索|接着跑|恢复搜索|恢复任务|确认登录|登录完成|验证完成继续)$",),
         "resume_search"),
    Rule("p1_task_status", "P1", 10,
         (r"^(?:任务状态|任务状态是什么|搜索进度|进度如何|现在在干什么|进行到哪了|搜索状态|"
          r"浏览器任务状态|搜索进展|查看任务状态|查看进度|查看(?:当前)?申请进度)$",),
         "task_status"),
    Rule("p1_job_stats", "P1", 10,
         (r"^(?:统计职位|职位统计|看板统计|搜索统计|保存了多少职位|统计一下搜索|搜索结果统计|数据统计|职位数据|职位分布)$",),
         "job_stats"),
    Rule("p1_list_jobs", "P1", 10,
         (r"^(?:看看职位|查看职位|列出职位|最近找到的职位|有哪些职位|职位列表|看看找到的职位|推荐职位|职位搜索结果|岗位搜索结果|搜索结果|查看搜索结果|查看职位结果|找到的职位|找到的岗位)$",),
         "list_jobs"),
    Rule("p1_view_resume", "P1", 10,
         (r"^(?:查看简历|看看简历|简历内容|我的简历|查看我的简历|简历写了什么|简历长什么样)$",),
         "view_resume"),
    Rule("p1_download_resume", "P1", 10,
         (r"^(?:下载简历|导出简历|导出我的简历|简历文件|下载我的简历)$",),
         "download_resume"),
    Rule("p1_upload_resume", "P1", 10,
         (r"^(?:上传简历|传简历|上传我的简历|上传一份简历)$",),
         "upload_resume"),
    Rule("p1_llm_settings_read", "P1", 10,
         (r"^(?:查看模型配置|当前模型|模型设置|查看模型|用什么模型|模型配置|模型是什么)$",),
         "llm_settings"),
    Rule("p1_help", "P1", 10,
         (r"^(?:帮助|你能做什么|你会什么|有哪些功能|使用说明|能干什么|怎么使用|怎么用)$",),
         "help"),
    # ----------------------------------------------------- P2 parameter templates
    Rule("p2_start_search", "P2", 20,
         (COMMAND_PATTERN,),
         "search_jobs"),
    Rule("p2_update_preferences", "P2", 20,
         (r"^(?:更新偏好|修改偏好|调整偏好|设置偏好|把偏好|目标岗位改成|目标方向改成|"
          r"工作城市加上|加上城市|薪资要求|排除|去除|删除|移除)",),
         "update_preferences"),
    Rule("p2_job_decision", "P2", 20,
         (r"^(?:关注|标记|跳过|忽略|不合适|感兴趣|收藏|拉黑)",),
         "job_decision"),
]


class RuleEngine:
    """Matches normalized text against the active, versioned rule set."""

    def __init__(self, store: RouterStore | None = None, rules_dir: Path | None = None) -> None:
        self.store = store
        self.rules_dir = rules_dir
        self._rules: list[Rule] = []
        self._compiled: list[tuple[Rule, list[re.Pattern[str]]]] = []
        self._version: int = BUILTIN_RULESET_VERSION
        self.reload()

    # ----------------------------------------------------------------- versions
    @property
    def version(self) -> int:
        return self._version

    def reload(self) -> None:
        """Load builtin rules plus the active version's overlay file, if any."""
        self._version = BUILTIN_RULESET_VERSION
        if self.store is not None:
            active = self.store.current_version()
            if active is not None:
                self._version = int(active)
        merged = {rule.id: rule for rule in DEFAULT_RULES}
        overlay = self._load_overlay(self._version)
        for rule in overlay:
            if rule.category == "P0":
                # Safety rules are immutable from outside the codebase.
                continue
            merged[rule.id] = rule
        self._rules = sorted(merged.values(), key=lambda item: (item.priority, item.id))
        self._compiled = [(rule, [re.compile(pattern) for pattern in rule.patterns]) for rule in self._rules]

    def _load_overlay(self, version: int) -> list[Rule]:
        if self.rules_dir is None:
            return []
        path = self.rules_dir / f"intent_rules_{version}.json"
        if not path.is_file():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [
                Rule(
                    id=str(item["id"]),
                    category=str(item.get("category", "P2"))[:3],
                    priority=int(item.get("priority", 20)),
                    patterns=tuple(str(pattern) for pattern in item.get("patterns", [])),
                    intent=str(item["intent"]),
                    reason=str(item.get("reason", "")),
                )
                for item in payload.get("rules", [])
            ]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return []

    def activate_version(self, version: int, note: str = "") -> bool:
        """Activate an overlay version by pointer; False when the file is missing."""
        if self.rules_dir is None or not (self.rules_dir / f"intent_rules_{version}.json").is_file():
            return False
        payload_hash = _rules_payload_hash(self._effective_rules(version))
        if self.store is not None:
            self.store.activate_version(
                version, payload_hash, note, baseline=self._version,
            )
        self.reload()
        return True

    def _effective_rules(self, version: int) -> list[Rule]:
        merged = {rule.id: rule for rule in DEFAULT_RULES}
        for rule in self._load_overlay(version):
            if rule.category != "P0":
                merged[rule.id] = rule
        return sorted(merged.values(), key=lambda item: (item.priority, item.id))

    def rollback(self) -> int | None:
        """Re-activate the previously retired version; None when nothing to roll back."""
        if self.store is None:
            return None
        previous = self.store.rollback()
        self.reload()
        return previous

    # ------------------------------------------------------------------- match
    def match(self, text: str) -> RuleMatch | None:
        """Match raw text: P0 safety rules run against the structural form
        (punctuation/path markers preserved), all others against the clean
        normalized form."""
        normalized = normalize_text(text)
        security = security_text(text)
        negated = bool(_NEGATION_RE.search(normalized))
        parsed_search = parse_search_message(text)
        if negated:
            self._hit("p4_negation_guard")
        for rule, compiled in self._compiled:
            if negated and rule.intent in ACTION_INTENTS:
                continue
            if rule.intent in {"pause_search", "resume_search", "stop_search"}:
                from ..workflow.planning import command_grounded
                tool = {"pause_search": "browser_task_pause", "resume_search": "browser_task_resume", "stop_search": "browser_task_stop"}[rule.intent]
                if not command_grounded(tool, text, [text]):
                    continue  # Normalization must not erase quotation/question polarity.
                action = {"pause_search": "pause", "resume_search": "resume", "stop_search": "stop"}[rule.intent]
                if parsed_search.complete and parsed_search.action == action:
                    self._hit(rule.id)
                    return RuleMatch(rule.id, rule.category, rule.intent, rule.reason)
            subject = security if rule.category == "P0" else normalized
            if rule.intent == "list_jobs" and parsed_search.action == "results":
                self._hit(rule.id)
                return RuleMatch(rule.id, rule.category, rule.intent, rule.reason)
            if rule.intent == "search_jobs":
                # Overlays cannot authorize partly parsed prose or negative/
                # interrogative requests just because a loose regex matches.
                if parsed_search.authorized and parsed_search.complete:
                    self._hit(rule.id)
                    return RuleMatch(rule.id, rule.category, rule.intent, rule.reason)
                continue
            for pattern in compiled:
                if pattern.search(subject):
                    self._hit(rule.id)
                    return RuleMatch(rule.id, rule.category, rule.intent, rule.reason)
        return None

    def _hit(self, rule_id: str) -> None:
        if self.store is not None:
            self.store.hit_rule(rule_id, self._version)

    def snapshot(self) -> list[dict]:
        """Active rules for monitoring/debug endpoints."""
        return [
            {
                "id": rule.id, "category": rule.category, "priority": rule.priority,
                "intent": rule.intent, "patterns": list(rule.patterns),
            }
            for rule in self._rules
        ]
