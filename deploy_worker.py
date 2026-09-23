"""Pin the Operator worker to a release; activate the first handoff only at idle."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from urllib.request import urlopen


def unit_text(release: Path, python: Path, home: Path, sock: Path) -> str:
    return f'''[Unit]
Description=Operator runner and browser API
After=network.target
[Service]
Type=simple
WorkingDirectory={release}/modules/operator
EnvironmentFile={home}/.config/host-app/env
EnvironmentFile={home}/.config/host-app/mcp-internal.env
Environment=SQUAD_STORE_HOST=127.0.0.1
Environment=SQUAD_STORE_PORT=5005
Environment=SQUAD_STORE_ENV=production
Environment=SQUAD_STORE_URL_PREFIX=/squad
Environment=OPERATOR_WORKER=1
Environment=OPERATOR_WORKER_SOCKET={sock}
UMask=0077
ExecStartPre=/usr/bin/rm -f {home}/.cache/computer-use/operator-deploy-drain
ExecStart={python} {release}/modules/operator/operator_worker.py
ExecStop={python} {release}/modules/operator/operator_worker.py --drain
TimeoutStopSec=infinity
KillMode=mixed
Restart=on-failure
RestartSec=3
[Install]
WantedBy=default.target
'''


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('release', type=Path)
    parser.add_argument('--python', type=Path, required=True)
    parser.add_argument('--activate', action='store_true')
    args = parser.parse_args()
    # Keep the virtualenv entry point: resolving its symlink selects system Python.
    release, python = args.release.resolve(), args.python.absolute()
    if not (release / 'modules/operator/operator_worker.py').is_file() or not python.is_file():
        raise SystemExit('release or Python executable is missing')
    home = Path.home()
    runtime = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
    sock = runtime / f'operator-{os.getuid()}.sock'
    units = home / '.config/systemd/user'
    unit = units / 'squad-operator-worker.service'
    proxy = units / 'host-app-server.service.d/operator-worker.conf'
    def systemctl(*argv):
        subprocess.run(['systemctl', '--user', *argv], check=True, timeout=180)
    def configure():
        unit.write_text(unit_text(release, python, home, sock))
        proxy.parent.mkdir(parents=True, exist_ok=True)
        proxy.write_text(f'[Service]\nEnvironment=OPERATOR_WORKER_SOCKET={sock}\n'
                         'ExecStop=\nExecStartPre=\nTimeoutStopSec=30\n')
    if not args.activate:
        configure()
        return

    from operator_restart_guard import admission_lock, drain_path
    with admission_lock(exclusive=True):
        marker = drain_path()
        marker.write_text(json.dumps({'pid': os.getpid(), 'started': time.time()}))
        try:
            with urlopen('http://127.0.0.1:5005/squad/operator/agent?conversation_id=worker-handoff', timeout=5) as response:
                active = json.load(response).get('admission', {}).get('active', 0)
            if active:
                raise SystemExit(f'Initial worker handoff needs idle Operator; {active} run(s) still active')
            configure()
            systemctl('daemon-reload')
            systemctl('enable', '--now', 'squad-operator-worker')
            from operator_worker import UnixConnection
            deadline = time.monotonic() + 30
            while True:
                connection = UnixConnection(str(sock), timeout=2)
                try:
                    connection.request('GET', '/squad/operator/agent?conversation_id=worker-health', headers={'Host': '127.0.0.1:5005'})
                    response = connection.getresponse()
                    if response.status == 200 and isinstance(json.loads(response.read()), dict):
                        break
                except (OSError, ValueError):
                    pass
                finally:
                    connection.close()
                if time.monotonic() >= deadline:
                    raise SystemExit('Worker did not become healthy; main process was not restarted')
                time.sleep(.25)
            systemctl('restart', 'host-app-server')
        finally:
            marker.unlink(missing_ok=True)
    print(f'Operator worker active on {sock}; release {release.name}')


if __name__ == '__main__':
    main()
