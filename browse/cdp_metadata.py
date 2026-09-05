"""Validate the browser identity returned by a DevTools version endpoint."""
import argparse
import json
import re
import sys
from urllib.parse import urlsplit
from urllib.request import urlopen


def valid_metadata(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    browser = value.get('Browser')
    websocket = value.get('webSocketDebuggerUrl')
    if not isinstance(browser, str) or not re.match(r'^(?:HeadlessChrome|Chrome|Chromium)/\d', browser):
        return False
    if not isinstance(websocket, str) or any(char.isspace() for char in websocket):
        return False
    try:
        url = urlsplit(websocket)
        port = url.port
        return (url.scheme in ('ws', 'wss') and bool(url.hostname)
                and (port is None or 0 < port <= 65535)
                and not url.username and not url.password
                and not url.query and not url.fragment
                and url.path.startswith('/devtools/browser/')
                and bool(url.path.removeprefix('/devtools/browser/')))
    except ValueError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--url')
    args = parser.parse_args()
    try:
        if args.url:
            with urlopen(args.url, timeout=2) as response:
                payload = json.load(response)
        else:
            payload = json.load(sys.stdin)
        return 0 if valid_metadata(payload) else 1
    except (OSError, ValueError):
        return 1


if __name__ == '__main__':
    sys.exit(main())
