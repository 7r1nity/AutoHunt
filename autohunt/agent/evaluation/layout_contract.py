"""Offline split-layout/storage contracts; temporary synthetic data only."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime

from cryptography.fernet import Fernet

from app.core.config import BACKEND_ROOT, Settings
from app.core.models import PrivateProfile
from app.memory.data_files import DataFileError, DataFiles
from app.memory.stores import (
    LLMCredentialStore, LLMSettingsStore, MemoryStore, PrivateProfileStore, SkillStore,
)

WORKSPACE = Path(__file__).resolve().parents[3]


class LayoutContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="autohunt-layout-")
        self.addCleanup(self.temp.cleanup)
        self.files = DataFiles(Path(self.temp.name) / "data")

    def test_applications_are_separate(self):
        self.assertEqual(BACKEND_ROOT, WORKSPACE / "autohunt" / "agent")
        for relative in ("autohunt/frontend/package.json", "mock-site/app/main.py",
                         "mock-site/frontend/job.html", "mock-site/requirements.txt"):
            self.assertTrue((WORKSPACE / relative).is_file(), relative)

    def test_removed_modules_are_not_importable(self):
        for name in ("multiagent", "sandbox_backend"):
            self.assertFalse((BACKEND_ROOT / "app" / name).exists())
            self.assertIsNone(importlib.util.find_spec("app." + name))

    def test_empty_mock_database_does_not_enable_history_reset(self):
        if not os.getenv("AUTOHUNT_MOCK_HISTORY_DATABASE"):
            self.assertIsNone(Settings().mock_history_database)

    def test_atomic_unicode_roundtrip(self):
        self.files.atomic_write("memory.md", "金融分析\n", suffixes={".md"})
        self.files.atomic_write("memory.md", "新的记忆\n", suffixes={".md"})
        self.assertEqual(self.files.read_text("memory.md", suffixes={".md"}), "新的记忆\n")
        self.assertEqual([p.name for p in self.files.root.iterdir()], ["memory.md"])

    def test_path_escape_rejected(self):
        for path in ("../outside.md", str(Path(self.temp.name) / "outside.md")):
            with self.assertRaises(DataFileError):
                self.files.atomic_write(path, "x", suffixes={".md"})

    def test_invalid_file_type_rejected(self):
        with self.assertRaises(DataFileError):
            self.files.atomic_write("program.py", "x", suffixes={".md"})

    def test_size_checks_use_encoded_bytes_on_read_and_write(self):
        self.files.atomic_write("memory.md", "中文中文", suffixes={".md"})
        small = DataFiles(self.files.root, max_bytes=8)
        with self.assertRaises(DataFileError):
            small.read_text("memory.md", suffixes={".md"})
        with self.assertRaises(DataFileError):
            small.atomic_write("new.md", "中文中文", suffixes={".md"})

    def test_memory_can_update_existing_fact(self):
        store = MemoryStore(self.files)
        store.update_fact("Job Preferences", "City", "上海")
        result = store.update_fact("Job Preferences", "City", "深圳")
        self.assertIn("City: 深圳", result)
        self.assertNotIn("City: 上海", result)

    def test_skill_proposal_and_history_remain_usable(self):
        store = SkillStore(self.files)
        proposal = store.propose("synthetic-skill", "仅用于合成验证", "验证存储")
        self.assertEqual(len(store.list_proposals()), 1)
        store.approve_proposal(proposal["id"])
        store.update("synthetic-skill", "第二版合成验证", "验证历史")
        self.assertEqual(store.list(), ["synthetic-skill"])
        self.assertEqual(len(list((self.files.root / "skill-history").glob("*.md"))), 1)
        self.assertEqual(store.list_proposals(), [])

    def test_encrypted_profile_and_credentials_survive_reopen(self):
        key = Fernet.generate_key().decode()
        profile = PrivateProfile(name="Synthetic Candidate", email="fixture@example.invalid", phone="000000")
        PrivateProfileStore(self.files, key).save(profile)
        synthetic_key = "synthetic-credential-only-for-offline-test"
        LLMCredentialStore(self.files, key).save("deepseek", synthetic_key)
        reopened = DataFiles(self.files.root)
        self.assertEqual(PrivateProfileStore(reopened, key).read(), profile)
        self.assertEqual(LLMCredentialStore(reopened, key).read("deepseek"), synthetic_key)
        self.assertNotIn(synthetic_key, (self.files.root / "private/llm-credentials.json.enc").read_text())
        self.assertNotIn(profile.email, (self.files.root / "private/profile.json.enc").read_text())

    def test_model_configuration_uses_ordinary_storage(self):
        store = LLMSettingsStore(self.files, "mock", "mock-autohunt-v1")
        self.assertEqual(store.read()["provider"], "mock")
        store.save("deepseek", "deepseek-v4-flash")
        self.assertEqual(LLMSettingsStore(self.files, "mock", "mock-autohunt-v1").read()["provider"], "deepseek")

    def test_dependency_assembly_isolated_from_user_data(self):
        env = dict(os.environ, AUTOHUNT_DATA_ROOT=str(self.files.root),
                   AUTOHUNT_DATA_KEY=Fernet.generate_key().decode(), AUTOHUNT_API_KEY="synthetic-test",
                   AUTOHUNT_LLM_PROVIDER="mock", AUTOHUNT_LLM_MODEL="mock-autohunt-v1")
        for key in ("DEEPSEEK_API_KEY", "AUTOHUNT_ROUTER_SMALL_API_KEY"):
            env.pop(key, None)
        result = subprocess.run([str(BACKEND_ROOT / ".venv/Scripts/python.exe"), "-c",
            "from app.core.dependencies import data_files; from app.main import app; assert app; print(type(data_files).__name__)"],
            cwd=BACKEND_ROOT, env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "DataFiles")

    def test_mock_site_independent_static_and_api_smoke(self):
        mock_root = WORKSPACE / "mock-site"
        interpreter = mock_root / ".venv/Scripts/python.exe"
        self.assertTrue(interpreter.is_file(), "Install mock-site dependencies before running this contract")
        env = dict(os.environ, DATABASE_URL="sqlite+aiosqlite:///" +
                   (Path(self.temp.name) / "synthetic-site.db").as_posix())
        code = """
import asyncio
from pathlib import Path
import httpx
from app.main import app
import app.database as database
from app.database import DATABASE_PATH, engine
assert DATABASE_PATH == Path(database.__file__).resolve().parents[1] / 'job_site.db'
async def smoke():
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            for url in ('/', '/job.html', '/employer-messages.html', '/api/jobs'):
                response = await client.get(url)
                assert response.status_code == 200, (url, response.status_code)
    await engine.dispose()
asyncio.run(smoke())
print('mock smoke passed')
"""
        result = subprocess.run([str(interpreter), "-c", code], cwd=mock_root, env=env,
                                text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("mock smoke passed", result.stdout)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LayoutContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    output = BACKEND_ROOT / "test-results/layout" / datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    output.mkdir(parents=True)
    summary = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                   external_model_calls=0, browser_runs=0, live_submissions=0,
                   coverage="offline storage, split paths, isolated backend import and synthetic ASGI site smoke; not browser E2E")
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "failures.json").write_text(json.dumps([{ "test": str(test), "detail": detail }
        for test, detail in result.failures + result.errors], ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), **summary}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
