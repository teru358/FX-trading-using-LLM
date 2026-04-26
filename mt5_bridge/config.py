"""ブリッジサーバー設定 (.env からロード)。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name, "").strip().lower()
    if v in ("true", "1", "yes", "on"):
        return True
    if v in ("false", "0", "no", "off"):
        return False
    return default


@dataclass(frozen=True)
class BridgeSettings:
    mt5_login: int
    mt5_password: str
    mt5_server: str
    host: str
    port: int
    api_key: str
    dry_run: bool

    @property
    def auth_required(self) -> bool:
        return bool(self.api_key)


def load_settings(env_file: Path | None = None) -> BridgeSettings:
    """.env をロードして BridgeSettings を返す。"""
    if env_file is None:
        env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        load_dotenv(env_file)

    login_raw = _env("MT5_LOGIN")
    if not login_raw:
        raise ValueError("MT5_LOGIN is required (set in mt5_bridge/.env)")
    try:
        login = int(login_raw)
    except ValueError as e:
        raise ValueError(f"MT5_LOGIN must be integer, got {login_raw!r}") from e

    password = _env("MT5_PASSWORD")
    if not password:
        raise ValueError("MT5_PASSWORD is required")
    server = _env("MT5_SERVER")
    if not server:
        raise ValueError("MT5_SERVER is required")

    return BridgeSettings(
        mt5_login=login,
        mt5_password=password,
        mt5_server=server,
        host=_env("BRIDGE_HOST", "0.0.0.0"),
        port=int(_env("BRIDGE_PORT", "8812")),
        api_key=_env("BRIDGE_API_KEY"),
        dry_run=_env_bool("DRY_RUN", True),
    )
