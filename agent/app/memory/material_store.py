from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..core.models import MaterialView
from .resume_store import MAX_EXTRACTED_CHARACTERS, MAX_RESUME_BYTES, ResumeError, extract_resume_text


DOCUMENT_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv"}
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".sql", ".json", ".yaml", ".yml", ".toml", ".html", ".css",
}
ALLOWED_EXTENSIONS = DOCUMENT_EXTENSIONS | CODE_EXTENSIONS | {".zip"}
IGNORED_PARTS = {".git", "node_modules", "dist", "build", ".next", "coverage", "vendor"}


class MaterialError(ValueError):
    pass


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise MaterialError("文件不是可识别的 UTF-8/GB18030 文本")


def _extract_zip(data: bytes) -> str:
    fragments: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) > 300 or sum(item.file_size for item in files) > 8_000_000:
                raise MaterialError("ZIP 解压后的文件数量或大小超过限制")
            for item in files:
                path = Path(item.filename.replace("\\", "/"))
                if any(part in IGNORED_PARTS for part in path.parts):
                    continue
                if path.suffix.lower() not in CODE_EXTENSIONS | {".txt", ".md", ".csv"}:
                    continue
                if item.file_size > 300_000:
                    continue
                try:
                    content = _decode(archive.read(item))
                except MaterialError:
                    continue
                fragments.append(f"\n--- FILE: {path.as_posix()[:240]} ---\n{content[:12_000]}")
                if sum(len(fragment) for fragment in fragments) >= MAX_EXTRACTED_CHARACTERS:
                    break
    except zipfile.BadZipFile as exc:
        raise MaterialError("无法解析 ZIP 项目文件") from exc
    if not fragments:
        raise MaterialError("ZIP 中没有识别到可分析的代码或文本文件")
    return "".join(fragments)[:MAX_EXTRACTED_CHARACTERS]


def extract_material_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise MaterialError("不支持该文件类型；请上传课程报告、项目文档、代码文件或 ZIP")
    if not data:
        raise MaterialError("上传文件为空")
    if len(data) > MAX_RESUME_BYTES:
        raise MaterialError("单个材料不能超过 1.5 MB")
    if suffix == ".zip":
        text = _extract_zip(data)
    elif suffix in {".pdf", ".docx", ".txt", ".md"}:
        try:
            text = extract_resume_text(filename, data)
        except ResumeError as exc:
            raise MaterialError(str(exc)) from exc
    else:
        text = _decode(data)
    text = re.sub(r"\x00", "", text).strip()
    if len(text) < 20:
        raise MaterialError("没有从材料中提取到足够内容")
    return text[:MAX_EXTRACTED_CHARACTERS]


class MaterialStore:
    def __init__(self, data_root: Path, data_key: str | None) -> None:
        self.root = (data_root / "private" / "materials").resolve()
        self.index_path = self.root / "index.json.enc"
        self.cipher = Fernet(data_key.encode()) if data_key else None

    def _require_cipher(self) -> Fernet:
        if self.cipher is None:
            raise RuntimeError("AUTOHUNT_DATA_KEY is required for encrypted candidate materials")
        return self.cipher

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    def _read_index(self) -> list[dict]:
        cipher = self._require_cipher()
        if not self.index_path.exists():
            return []
        try:
            value = json.loads(cipher.decrypt(self.index_path.read_bytes()))
            return value if isinstance(value, list) else []
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("Candidate material index could not be decrypted") from exc

    def save(self, filename: str, content_type: str, data: bytes, kind: str) -> tuple[MaterialView, str]:
        if kind not in {"course", "project", "code"}:
            raise MaterialError("材料类型必须是 course、project 或 code")
        cipher = self._require_cipher()
        text = extract_material_text(filename, data)
        material_id = uuid.uuid4().hex
        record = {
            "id": material_id,
            "filename": Path(filename).name[:200],
            "kind": kind,
            "content_type": (content_type or "application/octet-stream")[:100],
            "size": len(data),
            "uploaded_at": datetime.now(UTC).isoformat(),
            "status": "parsed",
            "extracted_characters": len(text),
        }
        self._atomic_bytes(self.root / f"{material_id}.document.enc", cipher.encrypt(data))
        self._atomic_bytes(self.root / f"{material_id}.text.enc", cipher.encrypt(text.encode("utf-8")))
        index = [record, *self._read_index()][:50]
        self._atomic_bytes(self.index_path, cipher.encrypt(json.dumps(index, ensure_ascii=False).encode("utf-8")))
        return MaterialView.model_validate(record), text

    def list(self) -> list[MaterialView]:
        return [MaterialView.model_validate(item) for item in self._read_index()]
