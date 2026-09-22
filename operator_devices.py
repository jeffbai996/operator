"""Best-effort viewer names from the local tailnet; never an authorization check.

Browsers cannot read the client OS hostname. Resolve the requesting peer, not
the server or the remotely controlled Chrome. Cache successes and misses so
presence heartbeats do not spawn a command each time.
"""
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

_cache = {}
_lock = threading.Lock()
_TAILNET_V4 = ipaddress.ip_network('100.64.0.0/10')
_TAILNET_V6 = ipaddress.ip_network('fd7a:115c:a1e0::/48')


def device_label(address, fallback=''):
    fallback = ' '.join(str(fallback or '').split())[:40] or 'Another device'
    try:
        ip = ipaddress.ip_address(address or '')
    except ValueError:
        return fallback
    if ip not in _TAILNET_V4 and ip not in _TAILNET_V6:
        return fallback
    address = str(ip)
    with _lock:
        cached = _cache.get(address)
        if cached and time.monotonic() - cached[0] < 300:
            return cached[1] or fallback
        name = ''
        executable = shutil.which('tailscale') or shutil.which('tailscale.exe')
        windows_cli = Path('/mnt/c/Program Files/Tailscale/tailscale.exe')
        if not executable and windows_cli.is_file():
            executable = str(windows_cli)
        if executable:
            try:
                result = subprocess.run([executable, 'whois', '--json', address],
                    capture_output=True, text=True, timeout=2, check=True,
                    cwd='/mnt/c' if executable.endswith('.exe') and os.name != 'nt' else None)
                node = json.loads(result.stdout).get('Node') or {}
                name = (node.get('Hostinfo') or {}).get('Hostname') or node.get('Name', '').split('.')[0]
                name = ' '.join(str(name).split())[:40]
            except (OSError, subprocess.SubprocessError, ValueError, AttributeError, TypeError):
                pass
        if len(_cache) >= 128:
            _cache.clear()
        _cache[address] = (time.monotonic(), name)
        return name or fallback
