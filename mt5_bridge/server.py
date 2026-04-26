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

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from config import BridgeSettings, load_settings
from mt5_client import Mt5Client

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ── 起動時にロードする singleton ────────────────────────────────────

_settings: BridgeSettings | None = None
_client: Mt5Client | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _client
    _settings = load_settings()
    _client = Mt5Client(
        login=_settings.mt5_login,
        password=_settings.mt5_password,
        server=_settings.mt5_server,
    )
    logger.warning(
        f"DRY_RUN={_settings.dry_run} | api_key={'set' if _settings.auth_required else 'NOT SET (LAN trust mode)'}"
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
        dry_run=_settings.dry_run if _settings else True,
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


# 注意: 発注 endpoint (/order, /modify, /close) は Phase 1+2 では実装しない。
# 資金 0 の本番口座保護のため、Phase 3 で BrokerAdapter 抽象とセットで導入予定。


def main() -> None:
    """`python server.py` で uvicorn を直接起動する。"""
    import uvicorn
    cfg = load_settings()
    # app オブジェクト直渡し (reload 不要、import 文字列のパス問題を回避)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")


if __name__ == "__main__":
    main()
