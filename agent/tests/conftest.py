"""Never let imported API/MCP singletons read or mutate a developer's data.

Loaded before test collection (a fixture would be too late for module imports).
Fixtures needing credentials/providers may still override their own instances.
"""
import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet


_test_data = tempfile.TemporaryDirectory(prefix="autohunt-tests-")
os.environ["AUTOHUNT_DATA_ROOT"] = str(Path(_test_data.name).resolve())
os.environ["AUTOHUNT_DATA_KEY"] = Fernet.generate_key().decode()
os.environ["AUTOHUNT_API_KEY"] = "isolated-test-api-key"
os.environ["AUTOHUNT_LLM_PROVIDER"] = "mock"
os.environ["AUTOHUNT_LLM_MODEL"] = "mock-autohunt-v1"
os.environ.pop("DEEPSEEK_API_KEY", None)
os.environ.pop("AUTOHUNT_ROUTER_SMALL_API_KEY", None)
