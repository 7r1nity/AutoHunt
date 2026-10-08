import io
import zipfile
from pathlib import Path

from cryptography.fernet import Fernet

from app.memory.material_store import MaterialStore, extract_material_text


def test_code_zip_extracts_allowlisted_files_and_ignores_dependencies() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/agent.py", "from fastapi import FastAPI\napp = FastAPI()\n# Python project code\n")
        archive.writestr("README.md", "LLM job matching project using Python and FastAPI.")
        archive.writestr("node_modules/pkg/index.js", "should not be included")
    text = extract_material_text("project.zip", buffer.getvalue())
    assert "src/agent.py" in text
    assert "FastAPI" in text
    assert "should not be included" not in text


def test_material_store_encrypts_project_and_lists_metadata(tmp_path: Path) -> None:
    store = MaterialStore(tmp_path, Fernet.generate_key().decode())
    content = b"Python FastAPI project with Docker deployment and automated tests."
    material, text = store.save("project.py", "text/x-python", content, "code")
    assert material.kind == "code"
    assert "FastAPI" in text
    assert store.list()[0].id == material.id
    encrypted = (tmp_path / "private" / "materials" / f"{material.id}.document.enc").read_bytes()
    assert content not in encrypted
