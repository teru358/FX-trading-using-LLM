from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path


_PREFIX_STYLES: dict[str, str] = {
    "[COLLECT]":   "cyan",
    "[NEWS]":      "blue",
    "[PRICE]":     "cyan",
    "[SIGNAL]":    "bold yellow",
    "[CLOSE]":     "dark_orange",
    "[TRADE]":     "bold green",
    "[ORDER]":     "bright_green",
    "[REFLECT]":   "magenta",
    "[AGGREGATE]": "dim cyan",
}

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
    """構造化ログプレフィックスのみを通すフィルタ。"""

    _PREFIXES = ("[COLLECT]", "[AGGREGATE]", "[SIGNAL]", "[CLOSE]", "[TRADE]", "[ORDER]", "[REFLECT]", "[API]")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return any(msg.startswith(p) for p in self._PREFIXES)


class _ApiAccessPrefixFilter(logging.Filter):
    """uvicorn.access のログメッセージに [API] プレフィックスを付与する。"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if not msg.startswith("[API]"):
            record.msg = "[API] " + msg
            record.args = ()
        return True


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
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # アクティビティログ（構造化プレフィックスのみ）
    activity_file = base_dir / cfg.activity_log_file
    activity_file.parent.mkdir(parents=True, exist_ok=True)
    af = logging.handlers.RotatingFileHandler(
        activity_file, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8",
    )
    af.setLevel(logging.INFO)
    af.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    af.addFilter(_ActivityLogFilter())
    root.addHandler(af)

    # uvicorn アクセスログに [API] プレフィックスを付与 → activity.log に流す
    logging.getLogger("uvicorn.access").addFilter(_ApiAccessPrefixFilter())

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
    root.addHandler(rh)
