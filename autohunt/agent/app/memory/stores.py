from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..core.models import PrivateProfile
from ..core.security import assert_no_secrets, assert_public_memory
from .data_files import DataFileError, DataFiles


SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,48}$")
ALLOWED_LLM_MODELS = {
    "mock": ["mock-autohunt-v1"],
    "deepseek": ["deepseek-v4-flash", "deepseek-v4-pro"],
}


class MemoryStore:
    def __init__(self, files: DataFiles) -> None:
        self.files = files
        self.path = "memory.md"

    def read(self) -> str:
        try:
            return self.files.read_text(self.path, suffixes={".md"})
        except FileNotFoundError:
            initial = "# AutoHunt Memory\n\n## Job Preferences\n\n- Status: not configured\n"
            self.files.atomic_write(self.path, initial, suffixes={".md"})
            return initial

    def update_fact(self, section: str, key: str, value: str) -> str:
        assert_public_memory(value)
        lines = self.read().splitlines()
        heading = f"## {section.strip()}"
        fact = f"- {key.strip()}: {value.strip()}"
        section_index = next((i for i, line in enumerate(lines) if line.strip() == heading), None)
        if section_index is None:
            lines.extend(["", heading, "", fact])
        else:
            next_heading = next(
                (i for i in range(section_index + 1, len(lines)) if lines[i].startswith("## ")),
                len(lines),
            )
            prefix = f"- {key.strip()}:"
            existing = next(
                (i for i in range(section_index + 1, next_heading) if lines[i].startswith(prefix)),
                None,
            )
            if existing is None:
                lines.insert(next_heading, fact)
            else:
                lines[existing] = fact
        content = "\n".join(lines).rstrip() + "\n"
        self.files.atomic_write(self.path, content, suffixes={".md"})
        return content


class SkillStore:
    def __init__(self, files: DataFiles) -> None:
        self.files = files

    @staticmethod
    def validate_name(name: str) -> str:
        if not SKILL_NAME_RE.fullmatch(name):
            raise DataFileError("Invalid skill name")
        return name

    def list(self) -> list[str]:
        skill_dir = self.files.resolve("skills/.keep.md", suffixes={".md"}).parent
        skill_dir.mkdir(parents=True, exist_ok=True)
        return sorted(path.stem for path in skill_dir.glob("*.md") if not path.name.startswith("."))

    def read(self, name: str) -> str:
        name = self.validate_name(name)
        return self.files.read_text(f"skills/{name}.md", suffixes={".md"})

    def update(self, name: str, content: str, reason: str) -> str:
        name = self.validate_name(name)
        assert_no_secrets(content)
        assert_no_secrets(reason)
        timestamp = datetime.now(UTC).isoformat()
        document = (
            f"---\nname: {name}\nupdated_at: {timestamp}\nupdate_reason: {json.dumps(reason, ensure_ascii=False)}\n---\n\n"
            f"{content.strip()}\n"
        )
        try:
            previous = self.read(name)
            history_name = f"skill-history/{name}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}.md"
            self.files.atomic_write(history_name, previous, suffixes={".md"})
        except FileNotFoundError:
            pass
        self.files.atomic_write(f"skills/{name}.md", document, suffixes={".md"})
        return document

    def propose(self, name: str, content: str, reason: str) -> dict:
        name = self.validate_name(name)
        assert_no_secrets(content)
        assert_no_secrets(reason)
        proposal = {
            "id": uuid.uuid4().hex,
            "skill": name,
            "content": content.strip(),
            "reason": reason.strip(),
            "created_at": datetime.now(UTC).isoformat(),
        }
        self.files.atomic_write(
            f"skill-proposals/{proposal['id']}.json",
            json.dumps(proposal, ensure_ascii=False, indent=2) + "\n",
            suffixes={".json"},
        )
        return proposal

    def list_proposals(self) -> list[dict]:
        directory = self.files.resolve("skill-proposals/.keep.json", suffixes={".json"}).parent
        directory.mkdir(parents=True, exist_ok=True)
        proposals = []
        for path in sorted(directory.glob("*.json")):
            proposals.append(json.loads(path.read_text(encoding="utf-8")))
        return proposals

    def approve_proposal(self, proposal_id: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", proposal_id):
            raise DataFileError("Invalid proposal id")
        relative = f"skill-proposals/{proposal_id}.json"
        proposal = json.loads(self.files.read_text(relative, suffixes={".json"}))
        document = self.update(proposal["skill"], proposal["content"], proposal["reason"])
        self.files.resolve(relative, suffixes={".json"}).unlink()
        return document


class PrivateProfileStore:
    def __init__(self, files: DataFiles, data_key: str | None) -> None:
        self.files = files
        self.cipher = Fernet(data_key.encode()) if data_key else None
        self.path = "private/profile.json.enc"

    def _require_cipher(self) -> Fernet:
        if self.cipher is None:
            raise RuntimeError("AUTOHUNT_DATA_KEY is required for private profile storage")
        return self.cipher

    def save(self, profile: PrivateProfile) -> None:
        cipher = self._require_cipher()
        encrypted = cipher.encrypt(profile.model_dump_json().encode()).decode()
        self.files.atomic_write(self.path, encrypted, suffixes={".enc"})

    def read(self) -> PrivateProfile | None:
        cipher = self._require_cipher()
        try:
            encrypted = self.files.read_text(self.path, suffixes={".enc"})
        except FileNotFoundError:
            return None
        try:
            return PrivateProfile.model_validate_json(cipher.decrypt(encrypted.encode()))
        except InvalidToken as exc:
            raise RuntimeError("Private profile could not be decrypted") from exc


class LLMSettingsStore:
    def __init__(self, files: DataFiles, default_provider: str, default_model: str) -> None:
        self.files = files
        self.path = "settings/llm.json"
        self.default = self._validate(default_provider, default_model)

    @staticmethod
    def _validate(provider: str, model: str) -> dict[str, str]:
        if provider not in ALLOWED_LLM_MODELS or model not in ALLOWED_LLM_MODELS[provider]:
            raise ValueError("Unsupported LLM provider or model")
        return {"provider": provider, "model": model}

    def read(self) -> dict[str, str]:
        try:
            stored = json.loads(self.files.read_text(self.path, suffixes={".json"}))
            return self._validate(stored["provider"], stored["model"])
        except FileNotFoundError:
            return self.default.copy()
        except (KeyError, TypeError, json.JSONDecodeError, ValueError):
            return self.default.copy()

    def save(self, provider: str, model: str) -> dict[str, str]:
        config = self._validate(provider, model)
        self.files.atomic_write(
            self.path,
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            suffixes={".json"},
        )
        return config


class LLMCredentialStore:
    def __init__(self, files: DataFiles, data_key: str | None) -> None:
        self.files = files
        self.cipher = Fernet(data_key.encode()) if data_key else None
        self.path = "private/llm-credentials.json.enc"

    def _require_cipher(self) -> Fernet:
        if self.cipher is None:
            raise RuntimeError("AUTOHUNT_DATA_KEY is required for encrypted API key storage")
        return self.cipher

    def _read_all(self) -> dict[str, str]:
        cipher = self._require_cipher()
        try:
            encrypted = self.files.read_text(self.path, suffixes={".enc"})
        except FileNotFoundError:
            return {}
        try:
            payload = cipher.decrypt(encrypted.encode()).decode()
            value = json.loads(payload)
            return value if isinstance(value, dict) else {}
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("LLM credentials could not be decrypted") from exc

    def save(self, provider: str, api_key: str) -> None:
        if provider not in ALLOWED_LLM_MODELS or provider == "mock":
            raise ValueError("Unsupported credential provider")
        api_key = api_key.strip()
        if len(api_key) < 20:
            raise ValueError("API key is too short")
        credentials = self._read_all()
        credentials[provider] = api_key
        encrypted = self._require_cipher().encrypt(
            json.dumps(credentials, ensure_ascii=False).encode()
        ).decode()
        self.files.atomic_write(self.path, encrypted, suffixes={".enc"})

    def read(self, provider: str) -> str | None:
        if self.cipher is None:
            return None
        return self._read_all().get(provider)

    def has(self, provider: str) -> bool:
        return self.read(provider) is not None
