"""Container-only desktop and Agent supervisor. No host credentials are imported."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from urllib.request import ProxyHandler, build_opener

from cryptography.fernet import Fernet


CONFIG_KEYS = ('AUTOHUNT_API_KEY', 'AUTOHUNT_DATA_KEY')


def load_credentials(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        key, separator, value = line.partition('=')
        if not separator or key not in CONFIG_KEYS or key in values or not value:
            raise RuntimeError('Invalid persistent container credentials; restore the config volume.')
        values[key] = value
    if set(values) != set(CONFIG_KEYS):
        raise RuntimeError('Incomplete persistent container credentials; restore the config volume.')
    try:
        Fernet(values['AUTOHUNT_DATA_KEY'].encode())
    except (ValueError, TypeError) as exc:
        raise RuntimeError('Invalid persistent encryption key; restore the config volume.') from exc
    return values


def ensure_credentials(path: Path, data_root: Path) -> dict[str, str]:
    if path.exists():
        return load_credentials(path)
    if data_root.exists() and any(data_root.iterdir()):
        raise RuntimeError('Agent data exists without its config volume; restore both volumes before starting.')
    path.parent.mkdir(parents=True, exist_ok=True)
    values = {
        'AUTOHUNT_API_KEY': secrets.token_urlsafe(32),
        'AUTOHUNT_DATA_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }
    # Atomic creation, private to UID 1000 (also used by the frontend container).
    temporary = path.with_name('.runtime-' + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(''.join(f'{key}={value}\n' for key, value in values.items()))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return values


def wait_display(children, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for _, process in children):
            raise RuntimeError('Desktop process exited during startup.')
        if subprocess.run(['xdpyinfo'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            return
        time.sleep(0.2)
    raise RuntimeError('Virtual display did not become ready.')


def desktop_ready() -> bool:
    opener = build_opener(ProxyHandler({}))
    with opener.open('http://127.0.0.1:9223/json/version', timeout=2) as response:
        if not json.load(response).get('webSocketDebuggerUrl', '').startswith('ws://'):
            return False
    with opener.open('http://127.0.0.1:9223/json/list', timeout=2) as response:
        targets = json.load(response)
    with opener.open('http://127.0.0.1:8080/api/health', timeout=2) as response:
        if json.load(response).get('service') != 'autohunt-mock-site':
            return False
    return any(target.get('type') == 'page' and target.get('url', '').startswith('http://127.0.0.1:8080/')
               for target in targets)


def wait_browser(children, *, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(process.poll() is not None for _, process in children):
            raise RuntimeError('Desktop process exited before browser takeover became ready.')
        try:
            if desktop_ready():
                return
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    raise RuntimeError('Browser CDP and simulator relay did not become ready; Agent was not started.')


def frontend_credentials(path: Path, credentials: dict[str, str]):
    """Give the frontend only its service token, in a separate read-only volume."""
    path.parent.mkdir(parents=True, exist_ok=True)
    expected = 'AUTOHUNT_API_KEY=' + credentials['AUTOHUNT_API_KEY'] + '\n'
    if path.is_file() and path.read_text(encoding='utf-8') == expected:
        return
    temporary = path.with_name('.frontend-' + secrets.token_hex(8))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stop_process(process, *, timeout):
    if process.poll() is not None:
        process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except ProcessLookupError:
        process.wait()
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)


def run():
    config = Path(os.environ.get('AUTOHUNT_CONFIG_FILE', '/run/autohunt-config/runtime.env'))
    data = Path(os.environ.get('AUTOHUNT_DATA_ROOT', '/var/lib/autohunt'))
    credentials = ensure_credentials(config, data)
    frontend_credentials(Path('/run/autohunt-frontend/runtime.env'), credentials)
    os.environ.update(credentials)
    data.mkdir(parents=True, exist_ok=True)
    profile = data / 'browser-profile'
    profile.mkdir(exist_ok=True)
    stopping = False

    def request_stop(_signal, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    children = []

    def start(name, command):
        # Desktop daemons do not need Agent service/encryption credentials.
        env = os.environ.copy()
        if name != 'agent':
            for key in CONFIG_KEYS:
                env.pop(key, None)
        children.append((name, subprocess.Popen(command, env=env, start_new_session=True)))

    try:
        start('display', ['Xvfb', os.environ.get('DISPLAY', ':99'), '-screen', '0', '1440x1000x24', '-nolisten', 'tcp', '-ac'])
        wait_display(children)
        start('window-manager', ['openbox', '--sm-disable'])
        # Preserve all existing localhost-only mock URL and CDP checks.
        start('mock-relay', ['socat', 'TCP-LISTEN:8080,bind=127.0.0.1,reuseaddr,fork', 'TCP:mock-site:8080'])
        start('vnc', ['x11vnc', '-display', os.environ.get('DISPLAY', ':99'), '-forever', '-shared', '-localhost', '-rfbport', '5900', '-nopw', '-quiet'])
        start('desktop-web', ['websockify', '--web=/usr/share/novnc/', '0.0.0.0:6080', '127.0.0.1:5900'])
        start('browser', ['chromium', '--no-sandbox', '--disable-dev-shm-usage', '--no-first-run',
                          '--no-default-browser-check', '--password-store=basic', '--disable-session-crashed-bubble',
                          '--remote-debugging-address=127.0.0.1', '--remote-debugging-port=9223',
                          f'--user-data-dir={profile}', '--window-size=1360,900', 'http://127.0.0.1:8080'])
        # Persisted queues may resume in the ASGI lifespan: never start it before
        # the same visible browser is actually available for takeover.
        wait_browser(children)
        start('agent', [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '0.0.0.0', '--port', '8000',
                        '--timeout-graceful-shutdown', '35'])
        while not stopping:
            exited = next((name for name, process in children if process.poll() is not None), None)
            if exited:
                raise RuntimeError(f'Container component stopped: {exited}.')
            time.sleep(0.3)
    finally:
        # Keep the browser and relay available while Agent saves its checkpoints.
        shutdown_order = ('agent', 'browser', 'desktop-web', 'vnc', 'window-manager', 'mock-relay', 'display')
        ordered = sorted(children, key=lambda item: shutdown_order.index(item[0]))
        for name, process in ordered:
            try:
                stop_process(process, timeout=40 if name == 'agent' else 3)
            except Exception:
                print(f'Container component cleanup failed: {name}.', file=sys.stderr, flush=True)


if __name__ == '__main__':
    try:
        run()
    except Exception as exc:
        print(f'AutoHunt container could not continue: {exc}', file=sys.stderr, flush=True)
        sys.exit(1)
