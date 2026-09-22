import json
import subprocess
from types import SimpleNamespace

import pytest
import operator_devices as devices


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    devices._cache.clear()
    monkeypatch.setattr(devices.shutil, 'which', lambda name: '/fixture/tailscale')


def test_peer_name_not_server_hostname_and_cached(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps({'Node': {
            'Name': 'host-b.example.ts.net.', 'Hostinfo': {'Hostname': 'HOST-B'}}}))
    monkeypatch.setattr(devices.subprocess, 'run', run)
    assert devices.device_label('100.100.10.20', 'Windows') == 'HOST-B'
    assert devices.device_label('100.100.10.20', 'Windows') == 'HOST-B'
    assert len(calls) == 1
    assert calls[0][-1] == '100.100.10.20'


@pytest.mark.parametrize('address', ['127.0.0.1', '192.168.1.10', '8.8.8.8', 'bad;command'])
def test_non_tailnet_address_does_not_launch_lookup(monkeypatch, address):
    monkeypatch.setattr(devices.subprocess, 'run', lambda *a, **kw: pytest.fail('unexpected lookup'))
    assert devices.device_label(address, 'iPad') == 'iPad'


def test_timeout_miss_is_cached_and_preserves_device_fallback(monkeypatch):
    calls = []
    def run(*args, **kwargs):
        calls.append(1)
        raise subprocess.TimeoutExpired('tailscale', 2)
    monkeypatch.setattr(devices.subprocess, 'run', run)
    assert devices.device_label('100.100.10.20', 'Windows') == 'Windows'
    assert devices.device_label('100.100.10.20', 'Mac') == 'Mac'
    assert len(calls) == 1
