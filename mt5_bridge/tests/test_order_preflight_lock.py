"""server.py が _client._mt5 を直接触らず public method 経由になっている配線テスト。

codex Medium#2: place_order だけでなく server.py 全体をスキャンし、将来別 endpoint に
直アクセスが混入しても回帰検出する。
"""
from __future__ import annotations

import inspect

import server


def test_server_never_accesses_client_mt5_directly():
    """server.py 全体のソースに `_client._mt5` 直アクセスが1件も無い。"""
    src = inspect.getsource(server)   # モジュール全体
    assert "_client._mt5" not in src, (
        "server.py accesses _client._mt5 directly somewhere; route all MT5 "
        "access through Mt5Client public methods so it stays under the lock."
    )


def test_place_order_uses_preflight_public_method():
    """/order の pre-flight は preflight_order_margin 経由になっている。"""
    src = inspect.getsource(server.place_order)
    assert "preflight_order_margin" in src
