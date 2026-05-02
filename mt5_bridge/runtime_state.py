"""runtime flag — 2-tier halt (soft / hard) と .env / フラグファイル永続化。

設計:
- bridge 起動時に .env の DRY_RUN とフラグファイルから初期化
- /admin/halt mode=soft → runtime flag のみ、再開可能
- /admin/halt mode=hard → .env 書換 + フラグファイル、再開には main PC 操作必要
- /admin/resume → soft halt のみ解除、hard 中は 403
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class RuntimeState:
    def __init__(
        self,
        *,
        initial_dry_run: bool,
        env_file: Path,
        hard_halt_flag: Path,
    ) -> None:
        self._dry_run = initial_dry_run
        self._soft_halted = False
        self._env_file = env_file
        self._flag = hard_halt_flag
        self._lock = threading.Lock()

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def soft_halted(self) -> bool:
        return self._soft_halted

    @property
    def is_hard_halted(self) -> bool:
        """hard halt 状態 = フラグファイル存在 + dry_run=True。"""
        return self._dry_run and self._flag.exists()

    @property
    def accepts_new_orders(self) -> bool:
        """新規 entry を受け付けるか。soft/hard halt 中はどちらも false。"""
        return not self._dry_run and not self._soft_halted

    def soft_halt(self, *, reason: str = "") -> None:
        with self._lock:
            self._soft_halted = True
            logger.warning(f"[ADMIN] SOFT HALT: {reason!r}")

    def hard_halt(self, *, reason: str = "") -> None:
        with self._lock:
            self._dry_run = True
            self._update_env_dry_run("true")
            self._flag.parent.mkdir(parents=True, exist_ok=True)
            self._flag.write_text(
                f"reason: {reason}\n"
                f"halted_at: {datetime.now(tz=timezone.utc).isoformat()}\n",
                encoding="utf-8",
            )
            logger.error(f"[ADMIN] HARD HALT: {reason!r} — manual recovery required")

    def resume(self) -> tuple[bool, str]:
        """soft halt のみ解除。hard halt 中は失敗。"""
        with self._lock:
            if self.is_hard_halted:
                return False, (
                    "hard halt cannot be resumed remotely; edit .env "
                    "and remove flag file on host"
                )
            self._soft_halted = False
            logger.warning("[ADMIN] RESUME — soft halt cleared")
            return True, "resumed from soft halt"

    def _update_env_dry_run(self, value: str) -> None:
        if not self._env_file.exists():
            logger.error(f"env file not found: {self._env_file}")
            return
        lines = self._env_file.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("DRY_RUN=") or stripped.startswith("DRY_RUN ="):
                lines[i] = f"DRY_RUN={value}"
                break
        else:
            lines.append(f"DRY_RUN={value}")
        self._env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
