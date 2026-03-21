from __future__ import annotations

import logging
import logging.handlers
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


class _PrefixRichHandler:
    """RichHandler に差し込む Mixin — プレフィックスを着色する。"""

    def render_message(self, record: logging.LogRecord, message: str):  # type: ignore[override]
        from rich.text import Text

        for prefix, style in _PREFIX_STYLES.items():
            if message.startswith(prefix):
                text = Text()
                text.append(prefix, style=style)
                text.append(message[len(prefix):])
                return text
        return super().render_message(record, message)  # type: ignore[misc]


class _ActivityLogFilter(logging.Filter):
    """構造化ログプレフィックスのみを通すフィルタ。"""

    _PREFIXES = ("[COLLECT]", "[NEWS]", "[SIGNAL]", "[CLOSE]", "[TRADE]", "[ORDER]", "[REFLECT]")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return any(msg.startswith(p) for p in self._PREFIXES)


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
