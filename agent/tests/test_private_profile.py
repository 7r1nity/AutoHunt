from pathlib import Path

from cryptography.fernet import Fernet

from app.core.models import PrivateProfile
from app.memory.stores import PrivateProfileStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


def test_private_profile_is_encrypted_at_rest(tmp_path: Path) -> None:
    store = PrivateProfileStore(WorkspaceSandbox(tmp_path), Fernet.generate_key().decode())
    profile = PrivateProfile(name="Test User", email="person@example.com", phone="+61 400 000 000")
    store.save(profile)

    raw = (tmp_path / "private" / "profile.json.enc").read_text(encoding="utf-8")
    assert "person@example.com" not in raw
    assert store.read() == profile
