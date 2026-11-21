"""Windows local launcher. No application imports in the command process.

Only the supervisor owns service processes. A file lock serializes commands;
PID creation times protect against recycled PIDs. Stop requests let Uvicorn run
its lifespan shutdown before the bounded process-tree fallback.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

PROJECT = Path(__file__).resolve().parents[1]
MOCK_SITE = PROJECT.parent / 'mock-site'
MOCK_PORT = 8080  # Production delivery adapters intentionally allow only this local port.
SCRIPT = Path(__file__).resolve()
LOGS = PROJECT / '.runlogs' / 'launcher'
STATE = LOGS / 'state.json'
PYTHON = PROJECT / 'agent' / '.venv' / 'Scripts' / 'python.exe'
NEXT = PROJECT / 'frontend' / 'node_modules' / 'next' / 'dist' / 'bin' / 'next'
HIDDEN = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def say(message):
    print(message, flush=True)


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def read_state():
    try:
        return json.loads(STATE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def process_stamp(pid):
    """Return the Windows process creation time, only while still alive."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *[ctypes.byref(s) for s in stamps]):
            return None
        return (stamps[0].dwHighDateTime << 32) | stamps[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)


def owned_process(record):
    return bool(record.get('stamp') and process_stamp(record['pid']) == record['stamp'])


def identity(pid):
    stamp = process_stamp(pid)
    if stamp is None:
        raise RuntimeError('服务进程提前退出，请查看日志。')
    return {'pid': pid, 'stamp': stamp}


def command_lock():
    import msvcrt
    LOGS.mkdir(parents=True, exist_ok=True)
    handle = (LOGS / 'command.lock').open('a+b')
    handle.seek(0)
    if not handle.read(1):
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError('另一个启动/停止操作正在进行，请稍后重试。')
    return handle


def available_port(port):
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'端口 {port} 已被占用。请先关闭对应服务，或指定其他端口；启动器不会结束外部进程。')


def run_checked(command, *, cwd=PROJECT):
    result = subprocess.run([str(part) for part in command], cwd=cwd)
    if result.returncode:
        raise RuntimeError(f'命令执行失败（退出码 {result.returncode}）：{Path(str(command[0])).name}。修复上方错误后重新启动。')


def find_node():
    candidates = [os.environ.get('AUTOHUNT_NODE'), shutil.which('node')]
    candidates += [str(Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'nodejs' / 'node.exe')]
    try:
        candidates.append(json.loads((LOGS / 'dependencies.json').read_text())['node'])
    except (OSError, ValueError, KeyError):
        pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            version = subprocess.check_output([candidate, '--version'], text=True).strip().lstrip('v')
            if tuple(int(x) for x in version.split('.')[:2]) < (20, 9):
                raise RuntimeError('Node.js 版本过低，请安装 Node.js 20.9+（推荐 LTS）。')
            return str(Path(candidate).resolve())
    raise RuntimeError('未找到 Node.js，请安装 Node.js 20.9+，或设置 AUTOHUNT_NODE 指向 node.exe。')


def service_ports(args):
    ports = {}
    if args.services in ('all', 'mock'):
        ports['mock'] = MOCK_PORT
    if args.services in ('all', 'autohunt'):
        ports.update(backend=args.backend_port, frontend=args.frontend_port)
    if len(set(ports.values())) != len(ports) or not all(1024 <= p <= 65535 for p in ports.values()):
        raise RuntimeError('服务端口必须不同，且位于 1024–65535；模拟站固定使用 8080。')
    return ports


def check_existing(state, args):
    expected = service_ports(args)
    # Old launcher records have no services field; never mistake them for a full stack.
    actual = state.get('services', list(state.get('children', {})))
    if set(actual) != set(expected) or set(state.get('children', {})) != set(expected) or any(
        state.get(name + '_port') != port for name, port in expected.items()
    ):
        raise RuntimeError('现有实例的服务或端口不同，请先运行 stop-local.cmd，再按需要重新启动。')


def prepare_mock():
    requirements = MOCK_SITE / 'requirements.txt'
    if not requirements.is_file() or not (MOCK_SITE / 'app' / 'main.py').is_file():
        raise RuntimeError('缺少 mock-site 源码，请使用包含完整模拟站的仓库版本。')
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    stamp = LOGS / 'mock-dependencies.json'
    try:
        old = json.loads(stamp.read_text())['fingerprint']
    except (OSError, ValueError, KeyError):
        old = None
    probe = 'import fastapi, uvicorn, sqlalchemy, aiosqlite, greenlet, httpx, pydantic, multipart'
    ready = subprocess.run([str(PYTHON), '-c', probe], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL).returncode == 0
    if not ready or old != fingerprint:
        say('准备模拟招聘网站依赖…')
        run_checked([PYTHON, '-m', 'pip', 'install', '-r', requirements])
    write_json(stamp, {'fingerprint': fingerprint})


def prepare(services='all'):
    if sys.version_info < (3, 12):
        raise RuntimeError('需要 Python 3.12 或更高版本。')
    if not PYTHON.is_file():
        say('首次启动：创建 Python 虚拟环境…')
        run_checked([sys.executable, '-m', 'venv', PYTHON.parents[1]])
    if services in ('all', 'mock'):
        prepare_mock()
    if services == 'mock':
        return ''  # Standalone mock site does not require Node, Chromium or Agent config.
    node = find_node()
    # Child tools need the selected Node on PATH, including when supplied explicitly.
    os.environ['PATH'] = str(Path(node).parent) + os.pathsep + os.environ.get('PATH', '')
    fingerprint_files = [PROJECT / 'agent' / 'pyproject.toml', PROJECT / 'pnpm-lock.yaml',
                         PROJECT / 'frontend' / 'package.json', PROJECT / 'package.json']
    fingerprint = hashlib.sha256(b''.join(p.read_bytes() for p in fingerprint_files)).hexdigest()
    stamp = LOGS / 'dependencies.json'
    try:
        old = json.loads(stamp.read_text())['fingerprint']
    except (OSError, ValueError, KeyError):
        old = None
    probe = 'import fastapi, uvicorn, dotenv, cryptography, mcp, openai, playwright, pypdf, multipart, httpx'
    ready = subprocess.run([str(PYTHON), '-c', probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not ready or (old is not None and old != fingerprint):
        say('安装后端依赖，首次运行需要联网…')
        run_checked([PYTHON, '-m', 'pip', 'install', '-e', './agent'])
    if not NEXT.is_file() or (old is not None and old != fingerprint):
        pnpm = shutil.which('pnpm.cmd') or shutil.which('pnpm')
        if pnpm:
            command = [pnpm, 'install', '--frozen-lockfile']
        else:
            npm = shutil.which('npm.cmd') or shutil.which('npm')
            if not npm:
                raise RuntimeError('未找到 pnpm 或 npm。请完整安装 Node.js（含 npm），或安装项目要求的 pnpm。')
            manager = json.loads((PROJECT / 'package.json').read_text())['packageManager']
            command = [npm, 'exec', '--yes', '--package=' + manager, '--', 'pnpm', 'install', '--frozen-lockfile']
        say('安装前端依赖，首次运行需要联网…')
        run_checked(command)
    browser_probe = 'from pathlib import Path; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); ok=Path(p.chromium.executable_path).is_file(); p.stop(); raise SystemExit(0 if ok else 1)'
    if subprocess.run([str(PYTHON), '-c', browser_probe], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        say('安装浏览器运行组件…')
        run_checked([PYTHON, '-m', 'playwright', 'install', 'chromium'])
    write_json(stamp, {'fingerprint': fingerprint, 'node': node})
    return node


def configure():
    from dotenv import dotenv_values
    back_path = PROJECT / 'agent' / '.env'
    front_path = PROJECT / 'frontend' / '.env.local'
    backend = dict(dotenv_values(back_path, encoding='utf-8-sig')) if back_path.exists() else {}
    frontend = dict(dotenv_values(front_path, encoding='utf-8-sig')) if front_path.exists() else {}
    # Never invent a new key for existing encrypted material.
    if not backend.get('AUTOHUNT_DATA_KEY'):
        data_path = Path(os.environ.get('AUTOHUNT_DATA_ROOT') or backend.get('AUTOHUNT_DATA_ROOT') or PROJECT / 'agent' / 'data')
        if not data_path.is_absolute():
            data_path = PROJECT / 'agent' / data_path
        if data_path.exists() and any(data_path.iterdir()):
            raise RuntimeError('已有数据但缺少 AUTOHUNT_DATA_KEY。请恢复原 agent/.env 的加密密钥后启动。')
    btoken, ftoken = backend.get('AUTOHUNT_API_KEY'), frontend.get('AUTOHUNT_BACKEND_TOKEN')
    if btoken and ftoken and btoken != ftoken:
        raise RuntimeError('前后端服务 Token 不一致，请核对 agent/.env 与 frontend/.env.local；未覆盖配置。')
    token = btoken or ftoken or secrets.token_urlsafe(32)
    defaults = {
        'AUTOHUNT_API_KEY': token,
        'AUTOHUNT_DATA_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        'AUTOHUNT_ALLOWED_ORIGINS': 'http://127.0.0.1:3000,http://localhost:3000',
        'AUTOHUNT_LLM_PROVIDER': 'mock', 'AUTOHUNT_LLM_MODEL': 'mock-autohunt-v1',
    }
    for path, values, additions in [(back_path, backend, defaults), (front_path, frontend, {
        'AUTOHUNT_BACKEND_TOKEN': token, 'AUTOHUNT_BACKEND_URL': 'http://127.0.0.1:8000'})]:
        missing = {key: val for key, val in additions.items() if not values.get(key)}
        if missing:
            with path.open('a', encoding='utf-8') as stream:
                stream.write('\n' + ''.join(f'{k}={v}\n' for k, v in missing.items()))
            values.update(missing)
            say(f'已补齐配置：{path.relative_to(PROJECT)}')
    return backend, frontend


def local_json(url, token=None):
    request = urllib.request.Request(url, headers={'X-AutoHunt-Key': token} if token else {})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3) as response:
        return json.load(response)


def wait_ready(url, child, stop, token=None, timeout=120, service='backend'):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError(f'{service} 服务提前退出，请查看 {service}.log。')
        if stop.exists():
            raise RuntimeError('启动已取消。')
        try:
            value = local_json(url, token)
            if isinstance(value, dict):
                if service == 'mock':
                    if value.get('service') == 'autohunt-mock-site' and value.get('status') == 'ok':
                        return
                elif all(key in value for key in ('discovered', 'stored', 'applying')):
                    return
        except (OSError, ValueError):
            pass
        time.sleep(.5)
    raise RuntimeError(f'{service} 服务健康检查超时，请查看日志。')


def kill_owned(record):
    if owned_process(record):
        # Exact, creation-time-checked PID; includes Node's worker subprocesses.
        subprocess.run(['taskkill.exe', '/PID', str(record['pid']), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=HIDDEN)


def shutdown_children(children, stop):
    # The Agent drains first; its last reconciliation may still need the mock site.
    stop.touch(exist_ok=True)
    for name, process, record in children:
        if name == 'backend':
            try:
                process.wait(timeout=35)
            except subprocess.TimeoutExpired:
                say('后端正常退出超时，将关闭本启动器的进程；未完成任务由现有恢复机制处理。')
                kill_owned(record)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
    stop.with_suffix('.mock.stop').touch(exist_ok=True)
    for name, process, _ in children:
        if name == 'mock':
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                say('模拟站正常退出超时，将关闭本启动器的模拟站进程。')
    for _, process, record in reversed(children):
        kill_owned(record)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


async def backend_worker(stop, port, service='backend'):
    import uvicorn
    if service == 'mock':
        sys.path.insert(0, str(MOCK_SITE))
        # An unrelated inherited DATABASE_URL must never select another application's DB.
        os.environ['DATABASE_URL'] = os.environ.get('AUTOHUNT_MOCK_DATABASE_URL') or (
            'sqlite+aiosqlite:///' + (MOCK_SITE / 'job_site.db').as_posix())
    else:
        from dotenv import load_dotenv
        load_dotenv(PROJECT / 'agent' / '.env', override=False, encoding='utf-8-sig')
        sys.path.insert(0, str(PROJECT / 'agent'))
    server = uvicorn.Server(uvicorn.Config('app.main:app', host='127.0.0.1', port=port,
                                          timeout_graceful_shutdown=20 if service == 'backend' else 10))
    async def watch():
        while not stop.exists():
            await asyncio.sleep(.25)
        server.should_exit = True
    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
        if not server.started:
            raise RuntimeError(f'{service} 服务未成功启动。')
    finally:
        watcher.cancel()


def supervise(args):
    stop = LOGS / (args.instance + '.stop')
    ports = service_ports(args)
    state = {'instance': args.instance, 'supervisor': identity(os.getpid()), 'phase': 'starting',
             'url': f'http://127.0.0.1:{MOCK_PORT if args.services == "mock" else args.frontend_port}',
             'services': list(ports), 'children': {},
             **{name + '_port': port for name, port in ports.items()}}
    if 'mock' in ports:
        state['mock_url'] = f'http://127.0.0.1:{MOCK_PORT}'
    write_json(STATE, state)
    children = []
    phase, error = 'stopped', None
    try:
        backend = configure()[0] if args.services != 'mock' else {}
        env = os.environ.copy()
        # Match authentication and endpoints, including port overrides, only in
        # child environments. Existing user configuration is never overwritten.
        env.update(PYTHONUTF8='1', NEXT_TELEMETRY_DISABLED='1')
        if backend:
            env.update(AUTOHUNT_API_KEY=backend['AUTOHUNT_API_KEY'],
                       AUTOHUNT_BACKEND_TOKEN=backend['AUTOHUNT_API_KEY'],
                       AUTOHUNT_BACKEND_URL=f'http://127.0.0.1:{args.backend_port}',
                       AUTOHUNT_ALLOWED_ORIGINS=f'http://127.0.0.1:{args.frontend_port},http://localhost:{args.frontend_port}')
        commands = [
            ('mock', [str(PYTHON), str(SCRIPT), 'mock', '--instance', args.instance], MOCK_SITE),
            ('backend', [str(PYTHON), str(SCRIPT), 'backend', '--instance', args.instance,
                         '--backend-port', str(args.backend_port)], PROJECT / 'agent'),
            ('frontend', [args.node, str(NEXT), 'dev', '--webpack', '--hostname', '127.0.0.1',
                          '--port', str(args.frontend_port)], PROJECT / 'frontend'),
        ]
        for name, command, cwd in commands:
            if name not in ports:
                continue
            available_port(ports[name])
            with (LOGS / f'{name}.log').open('ab') as log:
                process = subprocess.Popen(command, cwd=cwd, env=env, stdout=log, stderr=log,
                                           stdin=subprocess.DEVNULL, creationflags=HIDDEN)
            record = identity(process.pid)
            children.append((name, process, record))
            state['children'][name] = record
            write_json(STATE, state)
            paths = {'mock': f'http://127.0.0.1:{MOCK_PORT}/api/health',
                     'backend': f'http://127.0.0.1:{args.backend_port}/api/v1/automation/stats',
                     'frontend': state['url'] + '/api/agent/stats'}
            wait_ready(paths[name], process, stop,
                       backend['AUTOHUNT_API_KEY'] if name == 'backend' else None, service=name)
        state['phase'] = 'ready'
        write_json(STATE, state)
        while not stop.exists():
            if any(p.poll() is not None for _, p, _ in children):
                raise RuntimeError('服务意外退出，其余服务已关闭；请查看日志后重新启动。')
            time.sleep(.5)
    except Exception as exc:
        phase, error = 'failed', str(exc)
        say(error)
    finally:
        state['phase'] = 'stopping'
        write_json(STATE, state)
        shutdown_children(children, stop)
        state.update(phase=phase, error=error)
        write_json(STATE, state)
        stop.unlink(missing_ok=True)
        stop.with_suffix('.mock.stop').unlink(missing_ok=True)


def stop_current(state):
    if not state:
        say('当前没有由一键启动器管理的服务。')
        return
    if owned_process(state.get('supervisor', {})):
        (LOGS / (state['instance'] + '.stop')).touch()
        say('正在关闭服务并保存任务状态…')
        deadline = time.monotonic() + 100
        while time.monotonic() < deadline and owned_process(state['supervisor']):
            time.sleep(.5)
        if owned_process(state['supervisor']):
            raise RuntimeError('停止尚未完成。请查看 supervisor.log，稍后重试。')
    # Also handles orphaned children after an abrupt supervisor crash.
    for record in state.get('children', {}).values():
        kill_owned(record)
    say('服务已停止。日志、任务记录、配置和数据均保留。')


def main():
    parser = argparse.ArgumentParser(description='AutoHunt Windows local launcher')
    parser.add_argument('action', choices=['start', 'stop', 'status', 'prepare', 'supervise', 'backend', 'mock'])
    parser.add_argument('--services', choices=['all', 'autohunt', 'mock'], default='all')
    parser.add_argument('--backend-port', type=int, default=8000)
    parser.add_argument('--frontend-port', type=int, default=3000)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--instance')
    parser.add_argument('--node')
    args = parser.parse_args()
    if os.name != 'nt':
        raise RuntimeError('此启动器面向 Windows。其他系统请使用 README 中的手动命令。')
    if args.action in ('backend', 'mock'):
        suffix = '.mock.stop' if args.action == 'mock' else '.stop'
        port = MOCK_PORT if args.action == 'mock' else args.backend_port
        asyncio.run(backend_worker(LOGS / (args.instance + suffix), port, args.action))
        return
    if args.action == 'supervise':
        supervise(args)
        return
    with command_lock() as lock:
        state = read_state()
        if args.action == 'status':
            say(f"状态：{state.get('phase', 'not_started')}；管理进程在线：{owned_process(state.get('supervisor', {}))}")
            for name, record in state.get('children', {}).items():
                say(f'{name}：端口 {state.get(name + "_port", "未知")}；在线：{owned_process(record)}')
            say(f'日志目录：{LOGS}')
            return
        if args.action == 'stop':
            stop_current(state)
            return
        if owned_process(state.get('supervisor', {})):
            if state.get('phase') != 'ready':
                raise RuntimeError('服务仍在启动或停止中，请稍后重试。')
            check_existing(state, args)
            if not all(owned_process(record) for record in state.get('children', {}).values()):
                raise RuntimeError('实例中有服务退出，请先运行 stop-local.cmd，再重新启动。')
            say(f"服务已启动：{state['url']}")
            if not args.no_browser:
                webbrowser.open(state['url'])
            return
        if any(owned_process(r) for r in state.get('children', {}).values()):
            raise RuntimeError('检测到上次启动遗留的服务，请先运行 stop-local.cmd，再重新启动。')
        ports = service_ports(args)
        if args.action != 'prepare':
            for port in ports.values():
                available_port(port)
        node = prepare(args.services)
        # Re-enter in the project venv after bootstrap; no dependency on global dotenv.
        if Path(sys.executable).resolve() != PYTHON.resolve():
            lock.close()
            run_checked([PYTHON, SCRIPT, *sys.argv[1:]])
            return
        if args.services != 'mock':
            configure()
        if args.action == 'prepare':
            say('环境已准备完成。')
            return
        instance = secrets.token_hex(12)
        command = [str(PYTHON), str(SCRIPT), 'supervise', '--instance', instance, '--node', node, '--services', args.services,
                   '--backend-port', str(args.backend_port), '--frontend-port', str(args.frontend_port)]
        with (LOGS / 'supervisor.log').open('ab') as log:
            process = subprocess.Popen(command, cwd=PROJECT, stdout=log, stderr=log,
                                       stdin=subprocess.DEVNULL, creationflags=HIDDEN)
        say('正在启动服务并检查就绪状态，首次初始化可能需要几分钟…')
        deadline = time.monotonic() + 390
        while time.monotonic() < deadline:
            state = read_state()
            if state.get('instance') == instance:
                if state.get('phase') == 'ready':
                    say(f"服务已就绪：{state['url']}")
                    if state.get('mock_url') and args.services != 'mock':
                        say(f"模拟招聘网站：{state['mock_url']}")
                    say(f'日志：{LOGS}\n关闭服务：双击仓库根目录 stop-local.cmd')
                    if not args.no_browser:
                        webbrowser.open(state['url'])
                    return
                if state.get('phase') == 'failed':
                    raise RuntimeError(state.get('error') or '启动失败')
            if process.poll() is not None:
                raise RuntimeError(f'启动失败，请查看 {LOGS}')
            time.sleep(.5)
        stop_current(read_state())
        raise RuntimeError(f'启动超时，请查看 {LOGS}')


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        say('命令已中断；如服务已启动，可运行 stop-local.cmd 关闭。')
        sys.exit(130)
    except Exception as exc:
        say(f'AutoHunt：{exc}\n日志目录：{LOGS}')
        sys.exit(1)
