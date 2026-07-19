from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

_SIZE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*(B|KB|MB|GB)$", re.IGNORECASE)
_INTERVAL_RE = re.compile(r"^([0-9]+)\s*(H|D)$", re.IGNORECASE)
_SIZE_UNITS = {"b": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


def _make_rotating_handler(path: Path, cfg) -> logging.Handler:
    """rotate_timing を解析してサイズ or 時間ベースのローテーションハンドラを返す。"""
    timing = cfg.rotate_timing.strip()

    m = _SIZE_RE.match(timing)
    if m:
        num, unit = float(m.group(1)), m.group(2).lower()
        max_bytes = int(num * _SIZE_UNITS[unit])
        return logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=cfg.backup_count, encoding="utf-8",
        )

    m = _INTERVAL_RE.match(timing)
    if m:
        interval, unit = int(m.group(1)), m.group(2).lower()
        when = "h" if unit == "h" else "d"
        return logging.handlers.TimedRotatingFileHandler(
            path, when=when, interval=interval, backupCount=cfg.backup_count, encoding="utf-8",
        )

    # midnight / W0〜W6 はそのまま when に渡す
    return logging.handlers.TimedRotatingFileHandler(
        path, when=timing.lower(), backupCount=cfg.backup_count, encoding="utf-8",
    )


# ログプレフィックスの一元レジストリ。
#
# 各エントリは (prefix, rich_style_or_None, goes_to_activity_log) のタプル。
# 着色と activity.log フィルタの両方がここから派生するため、新しい構造化
# プレフィックスを追加する際はこの 1 箇所に追記する。
#
# goes_to_activity_log=True のものは logs/activity.log に流れる
# (INFO 以上 + プレフィックス合致時のみ)。
_PREFIX_REGISTRY: tuple[tuple[str, str | None, bool], ...] = (
    ("[COLLECT]",      "cyan",          True),
    ("[NEWS]",         "blue",          True),
    ("[PRICE]",        "cyan",          True),
    ("[SIGNAL]",       "bold yellow",   True),
    ("[CLOSE]",        "dark_orange",   True),
    ("[TRADE]",        "bold green",    True),
    ("[ORDER]",        "bright_green",  True),
    ("[REFLECT]",      "magenta",       True),
    ("[AGGREGATE]",    "dim cyan",      True),
    ("[MONITOR]",      "bold red",      True),
    ("[EXIT]",         "bold red",      True),
    ("[API]",          None,            True),
    # 取引判定・ポジション運用に関わるが従来 activity に載っていなかった
    # 主要イベント (今回追加):
    ("[REVIEW]",       "yellow",        True),   # Layer 1-3 position_review 結果
    ("[TRAIL]",        "bright_green",  True),   # 段階トレーリングストップ更新
    ("[RAG ADJ]",      "dim magenta",   True),   # RAG スコア補正
    ("[ADAPTIVE]",     "magenta",       True),   # adaptive params 更新
    # Phase 3c: bridge 連携 / halt 系イベント (本番運用で activity.log に必須)
    ("[MT5_BRIDGE]",   "red",           True),   # 発注経路の bridge 通信
    ("[PROVIDER]",     "yellow",        True),   # price provider degradation / fallback
    ("[ADMIN]",        "bold red",      True),   # /admin/halt / /admin/resume 操作ログ
    ("[HALT]",         "bold red",      True),   # halt_state I/O (corruption など)
    ("[BRIDGE_GATE]",  "bright_red",    True),   # bridge プリフライト + halt 判定
    ("[ORCH]",         "bold cyan",     True),   # orchestrator planning / watch / trigger イベント
)

_PREFIX_STYLES: dict[str, str] = {
    prefix: style for prefix, style, _ in _PREFIX_REGISTRY if style is not None
}

_ACTIVITY_PREFIXES: tuple[str, ...] = tuple(
    prefix for prefix, _, activity in _PREFIX_REGISTRY if activity
)

_MODEL_RE = re.compile(r"(\w+Client\()([^)]+)(\))")


class _PrefixRichHandler:
    """RichHandler に差し込む Mixin — プレフィックス着色 + モデル名着色。"""

    def render_message(self, record: logging.LogRecord, message: str):  # type: ignore[override]
        from rich.text import Text

        prefix_match = None
        for prefix, style in _PREFIX_STYLES.items():
            if message.startswith(prefix):
                prefix_match = (prefix, style)
                break

        has_model = _MODEL_RE.search(message)
        if not prefix_match and not has_model:
            return super().render_message(record, message)  # type: ignore[misc]

        text = Text()
        rest = message

        if prefix_match:
            pfx, style = prefix_match
            text.append(pfx, style=style)
            rest = message[len(pfx):]

        last = 0
        for m in _MODEL_RE.finditer(rest):
            text.append(rest[last : m.start()])
            text.append(m.group(1))                       # "OllamaClient("
            text.append(m.group(2), style="green")        # "phi4:14b"
            text.append(m.group(3))                       # ")"
            last = m.end()
        text.append(rest[last:])

        return text


class _ActivityLogFilter(logging.Filter):
    """構造化ログプレフィックスのみを通すフィルタ。対象は _PREFIX_REGISTRY 由来。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return any(msg.startswith(p) for p in _ACTIVITY_PREFIXES)


class _ApiAccessPrefixFilter(logging.Filter):
    """uvicorn.access のログメッセージに [API] プレフィックスを付与する。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if not msg.startswith("[API]"):
            record.msg = "[API] " + msg
            record.args = ()
        return True


class _ApiAccessTerminalFilter(logging.Filter):
    """ターミナル出力から uvicorn アクセスログを除外する。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name != "uvicorn.access"


def setup_logging(cfg, base_dir: Path) -> None:
    from rich.console import Console
    from rich.logging import RichHandler

    log_file = base_dir / cfg.file
    log_file.parent.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, cfg.level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # メインログ（全ログ・DEBUG以上）
    fh = _make_rotating_handler(log_file, cfg)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # アクティビティログ（構造化プレフィックスのみ）
    activity_file = base_dir / cfg.activity_log_file
    activity_file.parent.mkdir(parents=True, exist_ok=True)
    af = _make_rotating_handler(activity_file, cfg)
    af.setLevel(logging.INFO)
    af.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    af.addFilter(_ActivityLogFilter())
    root.addHandler(af)

    # uvicorn アクセスログに [API] プレフィックスを付与 → activity.log に流す
    logging.getLogger("uvicorn.access").addFilter(_ApiAccessPrefixFilter())

    # httpx は 1 リクエストごとに INFO で `HTTP Request: ...` を出し、quote-stream の
    # 毎秒 polling でターミナル・main log が汚染されるため WARNING に降格する。
    # プロセス内全 HTTP 成功行が消える意図的なグローバル抑制。エラー可視性への
    # 影響評価は spec 2026-07-07-quote-tick-log-suppression-design.md を参照。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # ターミナル（RichHandler — プレフィックス着色付き）
    _ColorRichHandler = type("_ColorRichHandler", (_PrefixRichHandler, RichHandler), {})
    rh = _ColorRichHandler(
        level=level,
        console=Console(stderr=False),
        show_time=True,
        show_level=True,
        show_path=False,
        rich_tracebacks=True,
        tracebacks_show_locals=False,
        markup=True,
        log_time_format="[%H:%M:%S]",
    )
    rh.setLevel(level)
    rh.addFilter(_ApiAccessTerminalFilter())
    root.addHandler(rh)
