"""Isolated configuration and process-ownership tests; no recruitment operations."""
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('local_launcher', Path(__file__).with_name('local_launcher.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='autohunt launcher ')
        self.root = Path(self.temp.name)
        (self.root / 'agent').mkdir()
        (self.root / 'frontend').mkdir()
        self.project = patch.object(launcher, 'PROJECT', self.root)
        self.project.start()
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.project.stop()
        self.temp.cleanup()

    def test_first_run_and_repeat_preserve_configuration(self):
        back, front = launcher.configure()
        self.assertEqual(back['AUTOHUNT_API_KEY'], front['AUTOHUNT_BACKEND_TOKEN'])
        files = [self.root / 'agent' / '.env', self.root / 'frontend' / '.env.local']
        original = [p.read_bytes() for p in files]
        launcher.configure()
        self.assertEqual(original, [p.read_bytes() for p in files])

    def test_missing_frontend_uses_original_token(self):
        launcher.configure()
        token = (self.root / 'agent' / '.env').read_bytes()
        (self.root / 'frontend' / '.env.local').unlink()
        back, front = launcher.configure()
        self.assertEqual(back['AUTOHUNT_API_KEY'], front['AUTOHUNT_BACKEND_TOKEN'])
        self.assertEqual(token, (self.root / 'agent' / '.env').read_bytes())

    def test_missing_key_never_rekeys_existing_data(self):
        data = self.root / 'agent' / 'data'
        data.mkdir()
        (data / 'runtime.db').write_bytes(b'existing data')
        with self.assertRaisesRegex(RuntimeError, '加密密钥'):
            launcher.configure()
        self.assertFalse((self.root / 'agent' / '.env').exists())

    def test_token_conflict_preserves_files(self):
        launcher.configure()
        path = self.root / 'frontend' / '.env.local'
        path.write_text('AUTOHUNT_BACKEND_TOKEN=different\n', encoding='utf-8')
        with self.assertRaisesRegex(RuntimeError, '不一致'):
            launcher.configure()
        self.assertIn('different', path.read_text())

    def test_existing_model_config_is_preserved(self):
        launcher.configure()
        path = self.root / 'agent' / '.env'
        with path.open('a', encoding='utf-8') as stream:
            stream.write('CUSTOM_SETTING=keep-me\n')
        before = path.read_bytes()
        launcher.configure()
        self.assertEqual(before, path.read_bytes())


@unittest.skipUnless(os.name == 'nt', 'Windows lifecycle')
class LifecycleTests(unittest.TestCase):
    def test_recycled_pid_is_not_killed(self):
        record = launcher.identity(os.getpid())
        self.assertTrue(launcher.owned_process(record))
        record['stamp'] += 1
        with patch.object(launcher.subprocess, 'run') as run:
            launcher.kill_owned(record)
            run.assert_not_called()

    def test_port_conflict_does_not_touch_listener(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            with self.assertRaisesRegex(RuntimeError, '已被占用'):
                launcher.available_port(listener.getsockname()[1])
            self.assertGreater(listener.fileno(), 0)

    def test_command_lock_serializes_processes(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(launcher, 'LOGS', Path(folder)):
            with launcher.command_lock():
                command = ('import importlib.util; from pathlib import Path; '
                           f's=importlib.util.spec_from_file_location("launch",{str(launcher.SCRIPT)!r}); '
                           'm=importlib.util.module_from_spec(s); s.loader.exec_module(m); '
                           f'm.LOGS=Path({folder!r}); m.command_lock()')
                result = subprocess.run([sys.executable, '-c', command], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
            with launcher.command_lock():
                pass


if __name__ == '__main__':
    unittest.main()
