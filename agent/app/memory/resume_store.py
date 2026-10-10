from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from xml.etree import ElementTree

from cryptography.fernet import Fernet, InvalidToken
from pypdf import PdfReader

from ..core.models import ResumeView


MAX_RESUME_BYTES = 1_500_000
MAX_EXTRACTED_CHARACTERS = 80_000
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md"}
WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SKILL_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("TypeScript", ("typescript",)),
    ("JavaScript", ("javascript",)),
    ("React", ("react", "react.js")),
    ("Next.js", ("next.js", "nextjs")),
    ("Vue", ("vue", "vue.js")),
    ("Angular", ("angular",)),
    ("Node.js", ("node.js", "nodejs")),
    ("Python", ("python",)),
    ("Java", ("java",)),
    ("Go", ("golang", " go ")),
    ("C++", ("c++",)),
    ("C#", ("c#", ".net")),
    ("FastAPI", ("fastapi",)),
    ("Django", ("django",)),
    ("Spring Boot", ("spring boot",)),
    ("AWS", ("aws", "amazon web services")),
    ("Azure", ("azure",)),
    ("GCP", ("gcp", "google cloud")),
    ("Docker", ("docker",)),
    ("Kubernetes", ("kubernetes", "k8s")),
    ("SQL", (" sql", "mysql", "postgresql", "postgres")),
    ("MongoDB", ("mongodb",)),
    ("Redis", ("redis",)),
    ("PyTorch", ("pytorch",)),
    ("TensorFlow", ("tensorflow",)),
    ("LLM", ("large language model", " llm", "大语言模型")),
    ("RAG", (" rag", "检索增强生成")),
)

class ResumeError(ValueError):
    pass


def _decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ResumeError("无法识别文本简历编码，请使用 UTF-8、PDF 或 DOCX")


def _extract_docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "word/document.xml" not in set(archive.namelist()):
                raise ResumeError("DOCX 文件结构无效")
            if sum(item.file_size for item in archive.infolist()) > 8_000_000:
                raise ResumeError("DOCX 解压后内容过大")
            root = ElementTree.fromstring(archive.read("word/document.xml"))
    except (zipfile.BadZipFile, ElementTree.ParseError) as exc:
        raise ResumeError("无法解析 DOCX 文件") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{WORD_NAMESPACE}p"):
        text = "".join(node.text or "" for node in paragraph.iter(f"{WORD_NAMESPACE}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return "\n".join(paragraphs)


def _extract_pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ResumeError("暂不支持加密 PDF，请先解除密码后上传")
        if len(reader.pages) > 30:
            raise ResumeError("简历 PDF 页数不能超过 30 页")
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text(extraction_mode="layout") or "")
            except (TypeError, ValueError):
                pages.append(page.extract_text() or "")
        return "\n".join(pages)
    except ResumeError:
        raise
    except Exception as exc:
        raise ResumeError("无法解析 PDF；扫描版 PDF 请先进行 OCR") from exc


def extract_resume_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ResumeError("仅支持 PDF、DOCX、TXT 和 Markdown 简历")
    if not data:
        raise ResumeError("上传的简历为空")
    if len(data) > MAX_RESUME_BYTES:
        raise ResumeError("简历不能超过 1.5 MB")
    if suffix == ".pdf":
        text = _extract_pdf(data)
    elif suffix == ".docx":
        text = _extract_docx(data)
    else:
        text = _decode_text(data)
    text = re.sub(r"[\t\r ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < 40:
        raise ResumeError("没有从简历中提取到足够文本；扫描版 PDF 请先进行 OCR")
    if len(text) > MAX_EXTRACTED_CHARACTERS:
        raise ResumeError("简历文本超过解析上限，请拆分文件；未静默截断原文")
    return text


def analyze_resume_text(text: str) -> tuple[list[str], list[str], str]:
    """Local skill highlighting only; semantic role/search extraction is model-owned."""
    normalized = f" {text.lower()} "
    skills = [label for label, patterns in SKILL_PATTERNS if any(pattern in normalized for pattern in patterns)]
    return skills[:20], [], ""


class ResumeStore:
    """Encrypted resume storage outside public memory and skill files."""

    def __init__(self, data_root: Path, data_key: str | None) -> None:
        self.root = (data_root / "private" / "resumes").resolve()
        self.cipher = Fernet(data_key.encode()) if data_key else None
        self.index_path = self.root / "index.json.enc"

    def _require_cipher(self) -> Fernet:
        if self.cipher is None:
            raise RuntimeError("AUTOHUNT_DATA_KEY is required for encrypted resume storage")
        return self.cipher

    def _read_index(self) -> list[dict]:
        cipher = self._require_cipher()
        if not self.index_path.exists():
            return []
        try:
            payload = cipher.decrypt(self.index_path.read_bytes())
            value = json.loads(payload)
            return value if isinstance(value, list) else []
        except (InvalidToken, json.JSONDecodeError) as exc:
            raise RuntimeError("Resume index could not be decrypted") from exc

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    def save(self, filename: str, content_type: str, data: bytes) -> ResumeView:
        cipher = self._require_cipher()
        self.root.mkdir(parents=True, exist_ok=True)
        text = extract_resume_text(filename, data)
        keywords, roles, suggested_query = analyze_resume_text(text)
        resume_id = uuid.uuid4().hex
        record = {
            "id": resume_id,
            "filename": Path(filename).name[:200],
            "content_type": (content_type or "application/octet-stream")[:100],
            "size": len(data),
            "uploaded_at": datetime.now(UTC).isoformat(),
            "status": "parsed",
            "extracted_characters": len(text),
            "keywords": keywords,
            "role_suggestions": roles,
            "suggested_query": suggested_query,
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        self._atomic_bytes(self.root / f"{resume_id}.document.enc", cipher.encrypt(data))
        self._atomic_bytes(self.root / f"{resume_id}.text.enc", cipher.encrypt(text.encode("utf-8")))
        index = [record, *[item for item in self._read_index() if item.get("sha256") != record["sha256"]]][:20]
        self._atomic_bytes(self.index_path, cipher.encrypt(json.dumps(index, ensure_ascii=False).encode("utf-8")))
        return ResumeView.model_validate(record)

    def list(self) -> list[ResumeView]:
        return [ResumeView.model_validate(item) for item in self._read_index()]

    def latest(self) -> ResumeView | None:
        records = self.list()
        return records[0] if records else None

    def get(self, resume_id: str) -> ResumeView:
        self._validate_id(resume_id)
        record = next((item for item in self.list() if item.id == resume_id), None)
        if record is None:
            raise FileNotFoundError(resume_id)
        return record

    @staticmethod
    def _validate_id(resume_id: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{32}", resume_id):
            raise ResumeError("Invalid resume id")

    def read_original(self, resume_id: str) -> tuple[ResumeView, bytes]:
        """Return the exact uploaded bytes; never reconstruct the file from extracted text."""
        self._validate_id(resume_id)
        record = self.get(resume_id)
        path = self.root / f"{resume_id}.document.enc"
        if not path.exists():
            raise FileNotFoundError(resume_id)
        try:
            return record, self._require_cipher().decrypt(path.read_bytes())
        except InvalidToken as exc:
            raise RuntimeError("Resume document could not be decrypted") from exc

    def delete(self, resume_id: str) -> ResumeView:
        """Delete one uploaded original and its extracted-text copy."""
        self._validate_id(resume_id)
        records = self.list()
        record = next((item for item in records if item.id == resume_id), None)
        if record is None:
            raise FileNotFoundError(resume_id)
        for suffix in ("document.enc", "text.enc"):
            (self.root / f"{resume_id}.{suffix}").unlink(missing_ok=True)
        remaining = [item.model_dump(mode="json") for item in records if item.id != resume_id]
        payload = json.dumps(remaining, ensure_ascii=False).encode("utf-8")
        self._atomic_bytes(self.index_path, self._require_cipher().encrypt(payload))
        return record

    def read_text(self, resume_id: str) -> str:
        self._validate_id(resume_id)
        cipher = self._require_cipher()
        path = self.root / f"{resume_id}.text.enc"
        if not path.exists():
            raise FileNotFoundError(resume_id)
        try:
            return cipher.decrypt(path.read_bytes()).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError) as exc:
            raise RuntimeError("Resume text could not be decrypted") from exc
