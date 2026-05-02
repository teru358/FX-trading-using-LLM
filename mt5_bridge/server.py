"""MT5 ブリッジ FastAPI サーバー (Phase 1+2: read-only)。

起動方法 (Windows main PC または WSL でテスト):
    cd mt5_bridge
    cp .env.example .env  # 値を埋める
    uv sync
    uv run python server.py
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from admin_models import AdminStatus, HaltRequest
from config import BridgeSettings, load_settings
from mt5_client import Mt5Client
from order_models import ClosePositionResponse, OrderRequest, OrderResponse
from runtime_state import RuntimeState

# ── ログ設定: stdout (色付き) + ローテーション付きファイル (色なし) ──
import sys

_LOG_DIR = Path(__file__).parent / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# levelname 専用 ANSI 色 (cmd.exe / Windows Terminal / Linux 共通)
_LEVEL_COLORS = {
    logging.DEBUG:    "\033[36m",     # cyan
    logging.INFO:     "\033[32m",     # green
    logging.WARNING:  "\033[33m",     # yellow
    logging.ERROR:    "\033[31m",     # red
    logging.CRITICAL: "\033[1;31m",   # bold red
}
_ANSI_RESET = "\033[0m"


def _colorize_levelname(record: logging.LogRecord, use_colors: bool) -> str | None:
    """record.levelname を色付き文字列に書換え、元の値を返す (None=色なし)。

    呼出側で finally に元値を戻すこと (record の汚染回避)。
    """
    if not use_colors:
        return None
    color = _LEVEL_COLORS.get(record.levelno)
    if not color:
        return None
    original = record.levelname
    record.levelname = f"{color}{original}{_ANSI_RESET}"
    return original


class _ColoredLevelFormatter(logging.Formatter):
    """ターミナル用フォーマッタ。`%(levelname)s` を ANSI で色付けする。

    アプリ標準ログ + uvicorn 起動メッセージ用。
    """

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        *,
        use_colors: bool | None = None,
    ) -> None:
        super().__init__(fmt=fmt, datefmt=datefmt)
        if use_colors is None:
            use_colors = sys.stderr.isatty()
        self._use_colors = bool(use_colors)

    def format(self, record: logging.LogRecord) -> str:
        original = _colorize_levelname(record, self._use_colors)
        try:
            return super().format(record)
        finally:
            if original is not None:
                record.levelname = original


def _make_colored_access_formatter(*args, **kwargs):
    """uvicorn.AccessFormatter を継承して levelname も色付けするクラスを返す。

    AccessFormatter は client_addr/request_line/status_code を scope から
    展開する特殊機能を持つため、置換ではなく継承する必要がある。
    関数で動的生成しているのは uvicorn のインポートをモジュールロード時に
    遅延させるため (mt5_bridge ローカル実行で uvicorn 未インストール環境を許容)。
    """
    from uvicorn.logging import AccessFormatter

    class _ColoredAccessFormatter(AccessFormatter):
        def __init__(self, *a, **kw) -> None:
            super().__init__(*a, **kw)
            if self.use_colors is None:
                self.use_colors = sys.stderr.isatty()

        def formatMessage(self, record: logging.LogRecord) -> str:
            original = _colorize_levelname(record, self.use_colors)
            try:
                return super().formatMessage(record)
            finally:
                if original is not None:
                    record.levelname = original

    return _ColoredAccessFormatter(*args, **kwargs)


# ファイル出力 (色なし、ANSI 制御文字を埋め込まない)
_file_handler = RotatingFileHandler(
    _LOG_DIR / "bridge.log",
    maxBytes=5_000_000,    # 5MB ごとに rotate
    backupCount=5,         # bridge.log + bridge.log.1 .. .5 を保持
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

# ターミナル出力 (色付き、tty 検出で自動切替)
_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(
    _ColoredLevelFormatter(_LOG_FORMAT, datefmt=_DATE_FORMAT),
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _file_handler],
)
logger = logging.getLogger(__name__)


# ── 起動時にロードする singleton ────────────────────────────────────

_settings: BridgeSettings | None = None
_client: Mt5Client | None = None
_runtime: RuntimeState | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _client, _runtime
    _settings = load_settings()
    flag_path = Path(__file__).parent / _settings.halt_state_path
    _runtime = RuntimeState(
        initial_dry_run=_settings.dry_run,
        env_file=Path(__file__).parent / ".env",
        hard_halt_flag=flag_path,
    )
    if _runtime.is_hard_halted:
        logger.error(
            "[ADMIN] starting in HARD HALT state (flag file present). "
            f"Remove {flag_path} and set DRY_RUN=false to recover."
        )
    _client = Mt5Client(
        login=_settings.mt5_login,
        password=_settings.mt5_password,
        server=_settings.mt5_server,
    )
    logger.warning(
        f"DRY_RUN={_runtime.dry_run} | api_key={'set' if _settings.auth_required else 'NOT SET (LAN trust mode)'}"
    )
    try:
        _client.connect()
        logger.info("MT5 connection established")
    except Exception as e:  # noqa: BLE001
        logger.error(f"MT5 connect failed at startup: {e}")
        # ブリッジ自体は起動を続行 (heartbeat に "connected=false" を返すため)
    yield
    if _client is not None:
        _client.disconnect()


app = FastAPI(
    title="MT5 Bridge",
    version="0.1.0",
    description="Read-only bridge over MetaTrader5 Python package. Phase 1+2 (no order placement).",
    lifespan=lifespan,
)


# ── 認証 (api_key 設定時のみ強制) ────────────────────────────────────

def require_api_key(x_bridge_api_key: str | None = Header(default=None)) -> None:
    if _settings is None or not _settings.auth_required:
        return
    if x_bridge_api_key != _settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-Bridge-Api-Key",
        )


# ── レスポンスモデル ────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str
    mt5_connected: bool
    dry_run: bool
    server: str | None = None
    login: int | None = None


# ── endpoints ────────────────────────────────────────────────────

@app.get("/health", response_model=HealthResponse)
def health():
    """ブリッジ生存確認 (heartbeat 用、無認証)。"""
    connected = _client is not None and _client.ping()
    return HealthResponse(
        status="ok",
        mt5_connected=connected,
        dry_run=_runtime.dry_run if _runtime else True,
        server=_settings.mt5_server if _settings else None,
        login=_settings.mt5_login if _settings else None,
    )


@app.get("/account", dependencies=[Depends(require_api_key)])
def account():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    return asdict(_client.get_account())


@app.get("/quote/{symbol}", dependencies=[Depends(require_api_key)])
def quote(symbol: str):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    try:
        return asdict(_client.get_quote(symbol))
    except RuntimeError as e:
        raise HTTPException(404, str(e))


@app.get("/positions", dependencies=[Depends(require_api_key)])
def positions():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    return [asdict(p) for p in _client.get_positions()]


@app.get("/symbols", dependencies=[Depends(require_api_key)])
def symbols():
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    return _client.get_symbols()


# ── 発注系 endpoint (Phase 3a: DRY_RUN のみ) ────────────────────────
# 実発注 (DRY_RUN=false) は Phase 3b で資金チェック・slippage 制御等とセットで実装。

# MT5 retcode → 処理分岐 (Phase 3b タスク 5)
_RETCODE_DONE = 10009         # TRADE_RETCODE_DONE
_RETCODE_PARTIAL = 10010      # TRADE_RETCODE_DONE_PARTIAL
_RETCODE_RETRYABLE = {10004, 10020}  # REQUOTE / PRICE_CHANGED → 1 回再試行
_RETCODE_RATE_LIMIT = 10024   # TOO_MANY_REQUESTS → 100ms backoff + 1 回再試行


@app.post("/order", response_model=OrderResponse,
          dependencies=[Depends(require_api_key)])
def place_order(req: OrderRequest):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized")

    # 経路 1: hard halt or 通常 DRY_RUN モード → DRY_RUN パス
    if _runtime.dry_run:
        try:
            result = _client.place_order_dry_run(
                symbol=req.symbol, side=req.side, volume_lots=req.volume_lots,
                sl=req.sl, tp=req.tp, magic=req.magic, comment=req.comment,
            )
            return OrderResponse(**result)
        except RuntimeError as e:
            raise HTTPException(404, str(e))

    # 経路 2: soft halt 中 → 423 Locked
    if _runtime.soft_halted:
        raise HTTPException(
            423,
            "bridge is soft-halted, new orders rejected. "
            "POST /admin/resume to clear halt.",
        )

    # 経路 3: live mode
    # ── 事前証拠金チェック (5% バッファ) ──
    try:
        if not _client._mt5.symbol_select(req.symbol, True):
            raise HTTPException(404, f"symbol {req.symbol} not available")
        tick = _client._mt5.symbol_info_tick(req.symbol)
        if tick is None:
            raise HTTPException(404, f"no tick for {req.symbol}")
        price = float(tick.ask if req.side == "buy" else tick.bid)
        required = _client.calc_required_margin(
            req.symbol, req.side, req.volume_lots, price,
        )
        required_with_buffer = required * 1.05
        account = _client.get_account()
        if account.free_margin < required_with_buffer:
            raise HTTPException(
                422,
                f"insufficient margin: required={required:.2f} "
                f"(with 5% buffer={required_with_buffer:.2f}) "
                f"free={account.free_margin:.2f}",
            )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"pre-flight check failed: {e}")

    # ── 発注 + retry (REQUOTE/PRICE_CHANGED 1 回、TOO_MANY_REQUESTS は 100ms backoff) ──
    last_result: dict | None = None
    for attempt in range(2):
        try:
            last_result = _client.place_order_live(
                symbol=req.symbol, side=req.side, volume_lots=req.volume_lots,
                sl=req.sl, tp=req.tp, magic=req.magic, comment=req.comment,
                filling_mode=_settings.filling_mode,
                deviation_points=_settings.deviation_points,
            )
        except RuntimeError as e:
            raise HTTPException(500, str(e))

        retcode = last_result["retcode"]
        if retcode in (_RETCODE_DONE, _RETCODE_PARTIAL):
            return OrderResponse(
                ticket=last_result["ticket"],
                symbol=last_result["symbol"],
                side=last_result["side"],
                volume_lots=last_result["volume_lots"],
                fill_price=last_result["fill_price"],
                sl=last_result["sl"], tp=last_result["tp"],
                time=last_result["time"],
                dry_run=False,
                magic=last_result["magic"],
            )
        if retcode in _RETCODE_RETRYABLE and attempt == 0:
            continue
        if retcode == _RETCODE_RATE_LIMIT and attempt == 0:
            import time as _t
            _t.sleep(0.1)
            continue
        break

    raise HTTPException(
        409,
        f"order rejected: retcode={last_result['retcode']} "
        f"comment={last_result.get('comment_response', '')}",
    )


@app.post("/positions/{ticket}/close", response_model=ClosePositionResponse,
          dependencies=[Depends(require_api_key)])
def close_position(ticket: int, symbol: str | None = None):
    if _client is None or not _client.is_connected:
        raise HTTPException(503, "MT5 not connected")
    if not _runtime.dry_run:
        raise HTTPException(
            501, "live close not implemented in Phase 3a — set DRY_RUN=true",
        )
    try:
        result = _client.close_position_dry_run(ticket, symbol=symbol)
        return ClosePositionResponse(**result)
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ── 管理エンドポイント (Phase 3b: 2-tier halt) ────────────────────────

def _build_admin_status() -> AdminStatus:
    return AdminStatus(
        dry_run=_runtime.dry_run,
        soft_halted=_runtime.soft_halted,
        is_hard_halted=_runtime.is_hard_halted,
        accepts_new_orders=_runtime.accepts_new_orders,
        mt5_connected=_client is not None and _client.ping(),
    )


@app.post("/admin/halt", response_model=AdminStatus,
          dependencies=[Depends(require_api_key)])
def admin_halt(req: HaltRequest):
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized")
    if req.mode == "hard":
        _runtime.hard_halt(reason=req.reason)
    else:
        _runtime.soft_halt(reason=req.reason)
    return _build_admin_status()


@app.post("/admin/resume", response_model=AdminStatus,
          dependencies=[Depends(require_api_key)])
def admin_resume():
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized")
    ok, msg = _runtime.resume()
    if not ok:
        raise HTTPException(403, msg)
    return _build_admin_status()


@app.get("/admin/status", response_model=AdminStatus,
         dependencies=[Depends(require_api_key)])
def admin_status():
    if _runtime is None:
        raise HTTPException(503, "runtime not initialized")
    return _build_admin_status()


class _UvicornNameRewriter(logging.Filter):
    """uvicorn.error logger name を 'uvicorn' に書き換えて表示する。

    uvicorn は起動・停止・通常 INFO ログをすべて `uvicorn.error` logger に流す
    仕様 (logger 名に "error" が入っていても誤りではない) のため、ログ表示が
    `[INFO] uvicorn.error:` となって誤解を招く。本フィルタで表示名のみ書換える。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "uvicorn.error":
            record.name = "uvicorn"
        return True


def main() -> None:
    """`python server.py` で uvicorn を直接起動する。"""
    import uvicorn

    cfg = load_settings()

    # uvicorn のデフォルトログは `INFO: ...` でタイムスタンプ無し
    # → アプリと同じフォーマット (時刻付き) に統一、`uvicorn.error` 表記も整理
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "uvicorn_name_fix": {
                "()": _UvicornNameRewriter,
            },
        },
        "formatters": {
            "default": {
                "()": _ColoredLevelFormatter,    # levelname に色
                "format": _LOG_FORMAT,
                "datefmt": _DATE_FORMAT,
            },
            "access": {
                # AccessFormatter を継承して levelname も色付け
                # status_code/request_line の色付けは AccessFormatter 標準のまま
                "()": _make_colored_access_formatter,
                "format": "%(asctime)s [%(levelname)s] uvicorn.access: %(client_addr)s - \"%(request_line)s\" %(status_code)s",
                "datefmt": _DATE_FORMAT,
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
                "filters": ["uvicorn_name_fix"],
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"],  "level": "INFO", "propagate": False},
        },
    }

    # app オブジェクト直渡し (reload 不要、import 文字列のパス問題を回避)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info", log_config=log_config)


if __name__ == "__main__":
    main()
