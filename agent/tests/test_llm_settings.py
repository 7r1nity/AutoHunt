from cryptography.fernet import Fernet
import pytest

from app.memory.stores import LLMCredentialStore, LLMSettingsStore
from app.sandbox_backend.filesystem import WorkspaceSandbox


def test_llm_settings_use_provider_model_allowlist(tmp_path) -> None:
    store = LLMSettingsStore(WorkspaceSandbox(tmp_path), "mock", "mock-autohunt-v1")
    assert store.save("deepseek", "deepseek-v4-flash")["model"] == "deepseek-v4-flash"
    with pytest.raises(ValueError):
        store.save("deepseek", "user-controlled-model")


def test_llm_key_is_encrypted_and_can_be_read(tmp_path) -> None:
    sandbox = WorkspaceSandbox(tmp_path)
    key = Fernet.generate_key().decode()
    secret = "sk-test-secret-abcdefghijklmnop"
    store = LLMCredentialStore(sandbox, key)
    store.save("deepseek", secret)

    raw = sandbox.read_text("private/llm-credentials.json.enc", suffixes={".enc"})
    assert secret not in raw
    assert store.read("deepseek") == secret


def test_llm_key_storage_requires_data_key(tmp_path) -> None:
    store = LLMCredentialStore(WorkspaceSandbox(tmp_path), None)
    with pytest.raises(RuntimeError):
        store.save("deepseek", "sk-test-secret-abcdefghijklmnop")
