import io
import zipfile
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.memory.resume_store import ResumeError, ResumeStore, analyze_resume_text, extract_resume_text


def test_resume_text_generates_role_and_keyword_query() -> None:
    text = (
        "Software Engineer with five years of full stack experience. "
        "Built React and Next.js products with TypeScript, Python, FastAPI, Docker and AWS."
    )
    skills, roles, query = analyze_resume_text(text)
    assert "TypeScript" in skills
    assert "Python" in skills
    assert roles[0] == "全栈工程师"
    assert "全栈工程师" in query


def test_docx_is_extracted_without_office_dependency() -> None:
    document_xml = b'''<?xml version="1.0" encoding="UTF-8"?>
    <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
      <w:body><w:p><w:r><w:t>Frontend Engineer using React, TypeScript and Next.js for production applications.</w:t></w:r></w:p></w:body>
    </w:document>'''
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
    extracted = extract_resume_text("resume.docx", buffer.getvalue())
    assert "Frontend Engineer" in extracted


def test_resume_store_encrypts_original_and_returns_safe_metadata(tmp_path: Path) -> None:
    store = ResumeStore(tmp_path, Fernet.generate_key().decode())
    content = (
        b"Backend Software Engineer. Python FastAPI PostgreSQL Docker AWS. "
        b"Designed APIs and distributed services for business applications."
    )
    view = store.save("candidate.txt", "text/plain", content)
    assert view.filename == "candidate.txt"
    assert "Python" in view.keywords
    assert store.latest() == view
    encrypted = (tmp_path / "private" / "resumes" / f"{view.id}.document.enc").read_bytes()
    assert content not in encrypted


def test_resume_original_round_trips_exact_bytes_and_can_be_deleted(tmp_path: Path) -> None:
    store = ResumeStore(tmp_path, Fernet.generate_key().decode())
    content = b"Original resume bytes with Python FastAPI React and enough content for parsing."
    view = store.save("original.txt", "text/plain", content)

    restored, original = store.read_original(view.id)
    assert restored == view
    assert original == content

    deleted = store.delete(view.id)
    assert deleted == view
    assert store.list() == []
    assert not (tmp_path / "private" / "resumes" / f"{view.id}.document.enc").exists()
    assert not (tmp_path / "private" / "resumes" / f"{view.id}.text.enc").exists()
    with pytest.raises(FileNotFoundError):
        store.read_original(view.id)


def test_resume_original_round_trips_exact_bytes_and_can_be_deleted(tmp_path: Path) -> None:
    store = ResumeStore(tmp_path, Fernet.generate_key().decode())
    content = b"Original resume bytes with Python FastAPI React and enough content for parsing."
    view = store.save("original.txt", "text/plain", content)

    restored, original = store.read_original(view.id)
    assert restored == view
    assert original == content

    deleted = store.delete(view.id)
    assert deleted == view
    assert store.list() == []
    assert not (tmp_path / "private" / "resumes" / f"{view.id}.document.enc").exists()
    assert not (tmp_path / "private" / "resumes" / f"{view.id}.text.enc").exists()
    with pytest.raises(FileNotFoundError):
        store.read_original(view.id)


def test_resume_rejects_unsupported_or_tiny_files() -> None:
    with pytest.raises(ResumeError):
        extract_resume_text("resume.exe", b"not allowed")
    with pytest.raises(ResumeError):
        extract_resume_text("resume.txt", b"too short")
