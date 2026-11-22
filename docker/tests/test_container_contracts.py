"""Container credentials, lifecycle and network contracts without live services."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('container_entrypoint', ROOT / 'docker' / 'agent_entrypoint.py')
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class CredentialContracts(unittest.TestCase):
    def test_initial_keys_are_persistent_and_decrypt_original_material(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path, data = root / 'config' / 'runtime.env', root / 'data'
            first = entry.ensure_credentials(path, data)
            ciphertext = entry.Fernet(first['AUTOHUNT_DATA_KEY'].encode()).encrypt(b'synthetic resume')
            data.mkdir()
            (data / 'material.enc').write_bytes(ciphertext)
            second = entry.ensure_credentials(path, data)
            self.assertEqual(first, second)
            self.assertEqual(entry.Fernet(second['AUTOHUNT_DATA_KEY'].encode()).decrypt(ciphertext), b'synthetic resume')

    def test_lost_config_does_not_silently_replace_an_encryption_key(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            (root / 'data' / 'material.enc').write_bytes(b'existing encrypted material')
            path = root / 'config' / 'runtime.env'
            with self.assertRaisesRegex(RuntimeError, 'restore both volumes'):
                entry.ensure_credentials(path, root / 'data')
            self.assertFalse(path.exists())

    def test_incomplete_or_duplicate_keys_are_not_repaired_by_regeneration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root / 'runtime.env'
            cases = ('AUTOHUNT_API_KEY=synthetic\n',
                     'AUTOHUNT_API_KEY=one\nAUTOHUNT_API_KEY=two\n',
                     'AUTOHUNT_API_KEY=synthetic\nAUTOHUNT_DATA_KEY=bad\n')
            for content in cases:
                with self.subTest(content=content):
                    path.write_text(content, encoding='utf-8')
                    with self.assertRaises(RuntimeError):
                        entry.ensure_credentials(path, root / 'data')
                    self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_frontend_receives_only_service_token_and_can_be_recreated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            credentials = entry.ensure_credentials(root / 'config.env', root / 'data')
            frontend = root / 'frontend' / 'runtime.env'
            entry.frontend_credentials(frontend, credentials)
            content = frontend.read_bytes()
            self.assertNotIn(b'AUTOHUNT_DATA_KEY', content)
            self.assertEqual(content.decode(), 'AUTOHUNT_API_KEY=' + credentials['AUTOHUNT_API_KEY'] + '\n')
            frontend.unlink()
            entry.frontend_credentials(frontend, credentials)
            self.assertEqual(frontend.read_bytes(), content)


class LifecycleContracts(unittest.TestCase):
    def test_browser_closes_through_cdp_without_terminating_its_process_group(self):
        process, helper = Mock(), Mock()
        process.poll.return_value = None
        helper.poll.return_value = 0
        with patch.object(entry.subprocess, 'Popen', return_value=helper) as launch, \
                patch.object(entry, 'stop_process') as forced_stop:
            entry.stop_browser(process)
        self.assertEqual(launch.call_args.args[0][-1], '--close-browser')
        helper.wait.assert_called_once_with(timeout=6)
        process.wait.assert_called_once_with(timeout=6)
        forced_stop.assert_not_called()
        self.assertNotIn('AUTOHUNT_DATA_KEY', launch.call_args.kwargs['env'])

    def test_browser_close_timeout_falls_back_to_bounded_owned_process_stop(self):
        process, helper = Mock(), Mock()
        process.poll.return_value = None
        process.wait.side_effect = subprocess.TimeoutExpired('synthetic browser', 6)
        helper.poll.return_value = 0
        with patch.object(entry.subprocess, 'Popen', return_value=helper), \
                patch.object(entry, 'stop_process') as forced_stop:
            entry.stop_browser(process)
        forced_stop.assert_called_once_with(process, timeout=3)

    def test_frozen_browser_close_helper_is_stopped_before_browser_fallback(self):
        process, helper = Mock(), Mock()
        process.poll.return_value = None
        helper.poll.return_value = 0
        helper.wait.side_effect = subprocess.TimeoutExpired('synthetic helper', 6)
        with patch.object(entry.subprocess, 'Popen', return_value=helper), \
                patch.object(entry, 'stop_process') as forced_stop:
            entry.stop_browser(process)
        forced_stop.assert_called_once_with(helper, timeout=0.5)
        process.wait.assert_called_once_with(timeout=6)

    def test_slow_browser_is_awaited_before_agent_may_restore_queues(self):
        process = Mock()
        process.poll.return_value = None
        with patch.object(entry, 'desktop_ready', side_effect=[ConnectionRefusedError(), False, True]) as ready, \
                patch.object(entry.time, 'sleep') as sleep:
            entry.wait_browser([('browser', process)])
        self.assertEqual(ready.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_browser_startup_failure_does_not_restore_queues(self):
        process = Mock()
        process.poll.return_value = 1
        with patch.object(entry, 'desktop_ready') as ready, self.assertRaisesRegex(RuntimeError, 'Desktop process exited'):
            entry.wait_browser([('browser', process)])
        ready.assert_not_called()

    def test_browser_startup_wait_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, 'Agent was not started'):
            entry.wait_browser([], timeout=0)

    def test_live_children_get_graceful_stop_before_bounded_kill(self):
        process = Mock(pid=43210)
        process.poll.return_value = None
        process.wait.side_effect = [subprocess.TimeoutExpired('synthetic', 1), 0]
        with patch.object(entry.os, 'killpg', create=True) as kill, \
                patch.object(entry.signal, 'SIGKILL', 9, create=True):
            entry.stop_process(process, timeout=1)
        self.assertEqual([call.args for call in kill.call_args_list],
                         [(43210, signal.SIGTERM), (43210, 9)])
        self.assertEqual(process.wait.call_count, 2)

    def test_exited_child_is_reaped_without_signalling_recycled_pid(self):
        process = Mock(pid=43210)
        process.poll.return_value = 0
        with patch.object(entry.os, 'killpg', create=True) as kill:
            entry.stop_process(process, timeout=1)
        kill.assert_not_called()
        process.wait.assert_called_once()


class ComposeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / 'compose.yaml').read_text(encoding='utf-8'))

    def test_only_workbench_mock_and_desktop_ports_are_published_on_loopback(self):
        published = [port for value in self.compose['services'].values() for port in value.get('ports', [])]
        self.assertCountEqual(published, ['127.0.0.1:3000:3000', '127.0.0.1:8080:8080', '127.0.0.1:6080:6080'])
        for value in self.compose['services'].values():
            self.assertTrue(value['init'])
            self.assertNotIn('privileged', value)
            self.assertNotIn('network_mode', value)
            self.assertNotIn('env_file', value)

    def test_frontend_cannot_read_encryption_config_or_agent_data(self):
        mounts = self.compose['services']['frontend']['volumes']
        self.assertEqual(mounts, ['frontend-config:/run/autohunt-config:ro'])
        self.assertNotIn('AUTOHUNT_DATA_KEY', self.compose['services']['frontend']['environment'])
        self.assertEqual(self.compose['services']['agent']['environment']['AUTOHUNT_BROWSER_CDP_URL'],
                         'http://127.0.0.1:9223')

    def test_dependency_gates_wait_for_mock_and_agent_health(self):
        services = self.compose['services']
        self.assertEqual(services['agent']['depends_on']['mock-site']['condition'], 'service_healthy')
        self.assertEqual(services['frontend']['depends_on']['agent']['condition'], 'service_healthy')
        for service in services.values():
            self.assertIn('healthcheck', service)
        self.assertEqual(set(self.compose['volumes']), {'agent-data', 'mock-data', 'config', 'frontend-config'})

    def test_test_override_never_enables_writes_in_normal_compose(self):
        self.assertNotIn('AUTOHUNT_DOCKER_TEST', self.compose['services']['agent']['environment'])
        override = yaml.safe_load((ROOT / 'docker' / 'compose.test.yaml').read_text(encoding='utf-8'))
        self.assertEqual(override['services']['agent']['environment']['AUTOHUNT_DOCKER_TEST'], '1')

    def test_container_host_allowlist_is_explicit_and_does_not_change_local_defaults(self):
        code = ('import json; from app.core.config import settings; '
                'print(json.dumps(settings.trusted_hosts))')
        env = {key: value for key, value in os.environ.items() if key != 'AUTOHUNT_TRUSTED_HOSTS'}
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT / 'autohunt' / 'agent',
                                env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['127.0.0.1', 'localhost', 'testserver'])
        env['AUTOHUNT_TRUSTED_HOSTS'] = '127.0.0.1, localhost,agent'
        result = subprocess.run([sys.executable, '-c', code], cwd=ROOT / 'autohunt' / 'agent',
                                env=env, text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), ['127.0.0.1', 'localhost', 'agent'])


if __name__ == '__main__':
    unittest.main()
