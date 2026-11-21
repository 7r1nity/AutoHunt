"""Unified launcher contracts. No user data, model calls or recruitment actions."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('unified_launcher', Path(__file__).with_name('local_launcher.py'))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


def arguments(services='all', **overrides):
    values = dict(services=services, backend_port=8000, frontend_port=3000,
                  instance='synthetic-test', node='node.exe')
    values.update(overrides)
    return SimpleNamespace(**values)


class ServiceSelectionTests(unittest.TestCase):
    def test_all_starts_site_before_agent_and_frontend(self):
        self.assertEqual(list(launcher.service_ports(arguments())), ['mock', 'backend', 'frontend'])

    def test_standalone_site_does_not_reserve_agent_ports(self):
        self.assertEqual(launcher.service_ports(arguments('mock')), {'mock': 8080})

    def test_agent_only_does_not_reserve_site_port(self):
        self.assertEqual(launcher.service_ports(arguments('autohunt')), {'backend': 8000, 'frontend': 3000})

    def test_port_collisions_and_invalid_ports_are_rejected(self):
        for options in ({'frontend_port': 8080}, {'backend_port': 8080},
                        {'frontend_port': 8000}, {'backend_port': 80}, {'frontend_port': 65536}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                launcher.service_ports(arguments(**options))

    def test_legacy_two_service_instance_cannot_claim_all_ready(self):
        state = dict(children={'backend': {}, 'frontend': {}}, backend_port=8000, frontend_port=3000)
        with self.assertRaisesRegex(RuntimeError, '先运行 stop-local'):
            launcher.check_existing(state, arguments())
        launcher.check_existing(state, arguments('autohunt'))

    def test_repeat_requires_same_services_ports_and_child_records(self):
        state = dict(services=['mock', 'backend', 'frontend'], mock_port=8080,
                     backend_port=8000, frontend_port=3000,
                     children={'mock': {}, 'backend': {}, 'frontend': {}})
        launcher.check_existing(state, arguments())
        with self.assertRaises(RuntimeError):
            launcher.check_existing(state, arguments(frontend_port=3100))
        with self.assertRaises(RuntimeError):
            launcher.check_existing(state, arguments('mock'))
        del state['children']['mock']
        with self.assertRaises(RuntimeError):
            launcher.check_existing(state, arguments())

    def test_standalone_mock_preparation_needs_no_node_or_browser(self):
        with patch.object(launcher, 'PYTHON', Path(__file__)), \
                patch.object(launcher, 'prepare_mock') as prepare_mock, \
                patch.object(launcher, 'find_node', side_effect=AssertionError('Node not needed')):
            self.assertEqual(launcher.prepare('mock'), '')
            prepare_mock.assert_called_once()


class HealthAndShutdownTests(unittest.TestCase):
    def test_mock_health_checks_service_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Mock()
            child.poll.return_value = None
            with patch.object(launcher, 'local_json', side_effect=[
                {'discovered': 0, 'stored': 0, 'applying': 0},
                {'service': 'other-service', 'status': 'ok'},
                {'service': 'autohunt-mock-site', 'status': 'ok'},
            ]) as request, patch.object(launcher.time, 'sleep'):
                launcher.wait_ready('http://127.0.0.1:8080/api/health', child,
                                    Path(folder) / 'stop', service='mock')
                self.assertEqual(request.call_count, 3)

    def test_starting_process_exit_is_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            child = Mock()
            child.poll.return_value = 1
            with self.assertRaisesRegex(RuntimeError, 'mock 服务提前退出'):
                launcher.wait_ready('unused', child, Path(folder) / 'stop', service='mock')

    def test_backend_drains_before_mock_stop_and_all_children_are_reaped(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = Path(folder) / 'instance.stop'
            mock_stop = stop.with_suffix('.mock.stop')
            events = []
            backend, mock, frontend = Mock(), Mock(), Mock()
            def backend_wait(**kwargs):
                if not events:
                    self.assertTrue(stop.exists())
                    self.assertFalse(mock_stop.exists())
                events.append('backend')
            def mock_wait(**kwargs):
                self.assertTrue(mock_stop.exists())
                self.assertIn('backend', events)
                events.append('mock')
            backend.wait.side_effect = backend_wait
            mock.wait.side_effect = mock_wait
            children = [('mock', mock, {'pid': 1}), ('backend', backend, {'pid': 2}),
                        ('frontend', frontend, {'pid': 3})]
            with patch.object(launcher, 'kill_owned') as kill:
                launcher.shutdown_children(children, stop)
            self.assertEqual(events[:2], ['backend', 'mock'])
            self.assertEqual([call.args[0]['pid'] for call in kill.call_args_list], [3, 2, 1])

    def test_backend_timeout_does_not_skip_mock_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = Path(folder) / 'instance.stop'
            backend, mock = Mock(), Mock()
            backend.wait.side_effect = subprocess.TimeoutExpired('synthetic-backend', 0)
            with patch.object(launcher, 'kill_owned') as kill:
                launcher.shutdown_children([('mock', mock, {'pid': 1}),
                                            ('backend', backend, {'pid': 2})], stop)
            self.assertTrue(stop.with_suffix('.mock.stop').exists())
            self.assertTrue(mock.wait.called)
            self.assertIn({'pid': 1}, [call.args[0] for call in kill.call_args_list])

    def test_startup_failure_rolls_back_all_owned_children(self):
        with tempfile.TemporaryDirectory() as folder:
            logs = Path(folder)
            mock, backend = Mock(pid=101), Mock(pid=102)
            with patch.object(launcher, 'LOGS', logs), patch.object(launcher, 'STATE', logs / 'state.json'), \
                    patch.object(launcher, 'identity', side_effect=lambda pid: {'pid': pid, 'stamp': 1}), \
                    patch.object(launcher, 'configure', return_value=({'AUTOHUNT_API_KEY': 'synthetic'}, {})), \
                    patch.object(launcher, 'available_port'), \
                    patch.object(launcher.subprocess, 'Popen', side_effect=[mock, backend]), \
                    patch.object(launcher, 'wait_ready', side_effect=[None, RuntimeError('backend failed')]), \
                    patch.object(launcher, 'shutdown_children') as cleanup:
                launcher.supervise(arguments())
                self.assertEqual([x[0] for x in cleanup.call_args.args[0]], ['mock', 'backend'])
                state = json.loads((logs / 'state.json').read_text(encoding='utf-8'))
                self.assertEqual(state['phase'], 'failed')
                self.assertEqual(state['error'], 'backend failed')


class SourcePublicationTests(unittest.TestCase):
    @staticmethod
    def git_environment():
        # Repository ignore rules must not depend on a host's injected Git options.
        return {key: value for key, value in os.environ.items() if not key.startswith('GIT_CONFIG_')}

    def test_runtime_and_personal_file_types_remain_ignored(self):
        root = Path(__file__).resolve().parents[2]
        paths = ['mock-site/job_site.db', 'mock-site/job_site.db-wal', 'mock-site/job_site.db-shm',
                 'mock-site/.env', 'mock-site/.venv/Scripts/python.exe', 'mock-site/.logs/mock.log',
                 'mock-site/resume.pdf', 'mock-site/backup.zip', 'mock-site/app/private.json',
                 'mock-site/frontend/resume.docx', 'mock-site/frontend/screenshot.png',
                 'mock-site/app/__pycache__/main.cpython-312.pyc',
                 'autohunt/agent/data/autohunt.db', 'autohunt/agent/.env']
        result = subprocess.run(['git', 'check-ignore', '--no-index', *paths], cwd=root,
                                capture_output=True, text=True, check=True, env=self.git_environment())
        self.assertEqual(set(result.stdout.splitlines()), set(paths))

    def test_simulator_sources_can_be_published(self):
        root = Path(__file__).resolve().parents[2]
        paths = ['mock-site/app/main.py', 'mock-site/app/routers/jobs.py',
                 'mock-site/common/skills.py', 'mock-site/frontend/index.html',
                 'mock-site/frontend/js/api.js', 'mock-site/frontend/css/style.css',
                 'mock-site/README.md', 'mock-site/requirements.txt',
                 'mock-site/start-local.cmd', 'mock-site/stop-local.cmd']
        result = subprocess.run(['git', 'check-ignore', '--no-index', *paths], cwd=root,
                                capture_output=True, text=True, env=self.git_environment())
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
