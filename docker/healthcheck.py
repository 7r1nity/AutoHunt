"""Read-only health probe, with no model calls or recruitment actions."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import ProxyHandler, Request, build_opener

from agent_entrypoint import load_credentials


def main():
    values = load_credentials(Path(os.environ.get('AUTOHUNT_CONFIG_FILE', '/run/autohunt-config/runtime.env')))
    opener = build_opener(ProxyHandler({}))
    for url, headers in (
        ('http://127.0.0.1:8000/api/v1/automation/stats', {'X-AutoHunt-Key': values['AUTOHUNT_API_KEY']}),
        ('http://127.0.0.1:9223/json/version', {}),
        ('http://127.0.0.1:8080/api/health', {}),
        ('http://127.0.0.1:6080/vnc.html', {}),
    ):
        with opener.open(Request(url, headers=headers), timeout=1.5) as response:
            assert response.status == 200
            if url.endswith('/stats'):
                assert 'discovered' in json.load(response)
            elif url.endswith('/json/version'):
                assert json.load(response)['webSocketDebuggerUrl'].startswith('ws://')
            elif url.endswith('/api/health'):
                assert json.load(response)['service'] == 'autohunt-mock-site'


if __name__ == '__main__':
    main()
