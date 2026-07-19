"""REST API サーバー (FastAPI + uvicorn)。

メインプロセスのバックグラウンドスレッドとして起動し、死活確認・ポジション照会・
ニュース状況・緊急決済を提供する。

このファイル自身は **ルーティング登録 + 起動エントリー** のみを担当する。
個々のエンドポイント実装は ``src/api/routes/`` 配下に分割されている:

- ``routes.health``  — /status (プロセス + halt + サブシステム健全性 統合), /logs, /usage, /schedule
- ``routes.account`` — /account (残高 + ポジション一覧、halt は /status へ集約)
- ``routes.admin``   — /admin/halt, /admin/resume (MT5 bridge へのプロキシ)
- ``routes.data``    — /news, /tech, /analyze, /feeds
- ``routes.trading`` — /run/trade, /close/{pair}
- ``routes.ask``     — /ask

共有ステート (config, store, llm_slot 等) は ``src/api/_state.py`` の
``state`` シングルトンに保持する。
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from fastapi import FastAPI

from src.api._state import state
from src.api.routes import account, admin, ask, data, health, orchestrator, trading
from src.concurrency.priority_job_slot import PriorityJobSlot
from src.config import AppConfig
from src.data.analysis_store import AnalysisStore
from src.data.price_store import PriceStore
from src.rag.vector_store import VectorStore
from src.utils.clock import db_now

logger = logging.getLogger(__name__)


app = FastAPI(title="FX Trading Bot API", docs_url=None, redoc_url=None)
app.include_router(health.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(data.router)
app.include_router(trading.router)
app.include_router(ask.router)
app.include_router(orchestrator.router)


# uvicorn のログ設定:
#   uvicorn / uvicorn.error は WARNING のみ (起動ノイズを抑制)
#   uvicorn.access は INFO + propagate=True → 既存の finance.log に流す
_UVICORN_LOG_CONFIG: dict = {
    "version": 1,
    "disable_existing_loggers": False,
    "loggers": {
        "uvicorn":        {"level": "WARNING", "propagate": True},
        "uvicorn.error":  {"level": "WARNING", "propagate": False},
        "uvicorn.access": {"level": "INFO",    "propagate": True},
    },
}


# uvicorn の startup 完了 (bind + lifespan) を待つ上限。bind 自体は呼び出し
# スレッドで同期実行するのでここで待つのは lifespan / event loop 立ち上げのみ。
# 本 app は lifespan handler を持たずミリ秒で終わるため、遅いマシンでも 10 秒
# あれば十分に余裕がある一方、ハングは確実に切れる。
_STARTUP_TIMEOUT_SEC = 10.0


def _bind_listen_socket(host: str, port: int) -> socket.socket:
    """呼び出しスレッドで bind + listen し、失敗は元の OSError のまま送出する。

    uvicorn 内部の bind (Server.startup) は OSError を握り潰して ``sys.exit(1)``
    に変換する上、別スレッドで起きるため呼び出し側が捕捉できない。先に自前で
    bind することで EADDRINUSE 等を **同期的に・元の例外のまま** 呼び出し元へ
    返す (外部レビュー High)。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # uvicorn の既定と揃える。再起動直後の TIME_WAIT で起動できないのを防ぐ。
    # Linux では listen 中の socket がいる限り EADDRINUSE のままなので、
    # 「旧プロセスがまだ生きている」検出は損なわれない。
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
        sock.listen(2048)
    except BaseException:
        sock.close()
        raise
    return sock


def start_api_server(
    config: AppConfig,
    store: VectorStore,
    analysis_store: AnalysisStore,
    llm_slot: PriorityJobSlot,
    price_store: PriceStore,
    orchestrator_store=None,  # OrchestratorStore (gate spec F-5)
    *,
    startup_timeout: float = _STARTUP_TIMEOUT_SEC,
) -> threading.Thread:
    """uvicorn をバックグラウンドスレッドで起動し、**起動完了まで同期で待つ**。

    ``run_startup_sequence`` は「API が上がらなければ発注ループを開始しない」
    fail-fast を前提にしている (spec §1.5)。以前の実装は thread を起動して即
    return していたため、bind 失敗が後から thread 内で起きるだけで start_api()
    は成功扱いになり、この安全保証が成立していなかった (外部レビュー High)。

    保証:

    - bind 失敗 (EADDRINUSE 等) は **呼び出しスレッドへ元の OSError のまま**
      送出される。
    - return した時点で uvicorn は accept 可能 (``Server.started``)。
    - startup が ``startup_timeout`` 秒で完了しなければ RuntimeError。
      fail-fast の趣旨を優先し、警告して継続はしない — 「API が上がったか
      不明のまま発注ループを開始する」のが最も避けたい状態のため。

    戻り値の Thread には停止用に ``server`` (uvicorn.Server) を属性として
    載せてある。``thread.server.should_exit = True`` で graceful に畳める。
    """
    state.config = config
    state.store = store
    state.analysis_store = analysis_store
    state.llm_slot = llm_slot
    state.price_store = price_store
    state.orchestrator_store = orchestrator_store
    state.started_at = db_now()

    import uvicorn

    host = "0.0.0.0"
    port = config.api.port

    # ① bind は呼び出しスレッドで (失敗をそのまま送出)
    sock = _bind_listen_socket(host, port)

    server = uvicorn.Server(uvicorn.Config(
        app, host=host, port=port, log_config=_UVICORN_LOG_CONFIG,
    ))

    exited = threading.Event()
    failure: list[BaseException] = []

    def _run() -> None:
        try:
            # bind 済み socket を渡す。uvicorn は shutdown 時にこれを close する。
            server.run(sockets=[sock])
        except BaseException as exc:  # SystemExit も含めて拾う
            failure.append(exc)
        finally:
            exited.set()

    thread = threading.Thread(target=_run, daemon=True, name="api-server")
    thread.server = server  # type: ignore[attr-defined]  # 停止用ハンドル
    thread.start()

    # ② startup 完了 (or 異常終了) を同期で待つ
    deadline = time.monotonic() + startup_timeout
    while not server.started and not exited.is_set():
        if time.monotonic() >= deadline:
            server.should_exit = True
            thread.join(timeout=5)
            raise RuntimeError(
                f"REST API server did not start within {startup_timeout}s "
                f"(0.0.0.0:{port}) — 起動を中止します"
            )
        time.sleep(0.01)

    if failure:
        # bind 以外の起動失敗 (lifespan 例外等) も握り潰さず再送出する。
        raise failure[0]
    if not server.started:
        raise RuntimeError(
            f"REST API server exited during startup (0.0.0.0:{port})"
        )

    logger.info(f"REST API server started on {host}:{port}")
    return thread
