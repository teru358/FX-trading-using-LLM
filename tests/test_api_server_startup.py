"""start_api_server の同期 fail-fast テスト (外部レビュー High)。

`run_startup_sequence` は「start_api() が失敗したら initialize / runtime.start()
に進まない」設計だが、start_api_server が daemon thread を起動して即 return して
いた間はこの保証が成立していなかった (bind 失敗が後から thread 内で起きるだけ)。

このファイルは **Mock を使わず実物の start_api_server** を叩き、bind 失敗が
呼び出しスレッドへ同期的に伝播することを検証する。
"""
from __future__ import annotations

import errno
import socket

import pytest

from src.api import server as api_server
from src.api._state import state
from src.config.schema import AppConfig


@pytest.fixture(autouse=True)
def _restore_api_state():
    """state は module singleton。テストの注入が後続テストに漏れないよう復元する。"""
    saved = dict(state.__dict__)
    yield
    state.__dict__.clear()
    state.__dict__.update(saved)


def _config(port: int) -> AppConfig:
    cfg = AppConfig()
    cfg.api.enabled = True
    cfg.api.port = port
    return cfg


def _start(port: int):
    return api_server.start_api_server(
        _config(port), None, None, None, None,
    )


def _stop(handle) -> None:
    """テストがポートを掴んだまま終わらないよう確実に停止する。"""
    server = getattr(handle, "server", None)
    if server is not None:
        server.should_exit = True
    handle.join(timeout=15)
    assert not handle.is_alive(), "api-server thread が停止しなかった"


def _occupy_port():
    """空きポートを動的取得し、listen 状態で占有した socket を返す。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", 0))
    sock.listen(5)
    return sock, sock.getsockname()[1]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def test_port_in_use_raises_in_calling_thread():
    """EADDRINUSE は呼び出しスレッドへ再送出される (握り潰さない)。"""
    occupied, port = _occupy_port()
    try:
        with pytest.raises(OSError) as exc:
            _start(port)
        assert exc.value.errno == errno.EADDRINUSE
    finally:
        occupied.close()


def test_returns_only_after_server_is_accepting():
    """return 時点で bind + startup 完了している (ready 待ちを消すと落ちる)。"""
    port = _free_port()
    handle = _start(port)
    try:
        assert handle.server.started is True
        # 実際に接続できる = listen 済み
        with socket.create_connection(("127.0.0.1", port), timeout=5):
            pass
    finally:
        _stop(handle)


def test_port_released_after_stop():
    """停止後にポートが解放される (full suite で後続テストを塞がない)。"""
    port = _free_port()
    handle = _start(port)
    _stop(handle)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", port))
    finally:
        probe.close()
