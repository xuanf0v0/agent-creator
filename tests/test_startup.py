from __future__ import annotations

from types import SimpleNamespace

import openagent_studio.app as studio_app


def test_windows_listener_pids_only_returns_matching_listeners():
    output = """
      TCP    127.0.0.1:8787       0.0.0.0:0       LISTENING       123
      TCP    127.0.0.1:8788       0.0.0.0:0       LISTENING       456
      TCP    127.0.0.1:8787       127.0.0.1:50000 ESTABLISHED     789
      TCP    [::]:8787            [::]:0          LISTENING       321
    """

    assert studio_app._windows_listener_pids(output, 8787) == {123, 321}


def test_stop_port_listeners_terminates_windows_processes(monkeypatch):
    calls: list[list[str]] = []
    availability = iter((False, True))

    monkeypatch.setattr(studio_app, "_listener_pids", lambda _port: {123, 456})
    monkeypatch.setattr(studio_app.os, "getpid", lambda: 456)
    monkeypatch.setattr(studio_app.os, "name", "nt")
    monkeypatch.setattr(studio_app, "_port_is_available", lambda _host, _port: next(availability))
    monkeypatch.setattr(studio_app.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        studio_app.subprocess,
        "run",
        lambda command, **_kwargs: calls.append(command) or SimpleNamespace(returncode=0),
    )

    assert studio_app._stop_port_listeners("127.0.0.1", 8787) == {123}
    assert calls == [["taskkill", "/PID", "123", "/T", "/F"]]


def test_stop_port_listeners_can_be_disabled(monkeypatch):
    monkeypatch.setenv("OPENAGENT_KILL_PORT", "0")
    monkeypatch.setattr(
        studio_app,
        "_listener_pids",
        lambda _port: (_ for _ in ()).throw(AssertionError("listener lookup should be skipped")),
    )

    assert studio_app._stop_port_listeners("127.0.0.1", 8787) == set()
