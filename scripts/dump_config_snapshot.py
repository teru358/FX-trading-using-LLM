"""AppConfig を JSON スナップショットへ書き出す。

config 移行の preflight 用。移行前の git revision でも動くよう、
移行後に導入する API (embedding 等) に依存しない書き方にしている。

使い方:
    uv run python scripts/dump_config_snapshot.py <出力先.json> [config/settings.yaml]
"""
from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config


def flatten(obj, prefix: str = "") -> dict[str, str]:
    """dataclass を {ドット区切りパス: repr(値)} に平坦化する。

    値を repr 文字列にするのは、Path や enum を JSON 化するためと、
    型が変わった場合も差分として見えるようにするため。
    """
    out: dict[str, str] = {}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for f in dataclasses.fields(obj):
            out.update(flatten(getattr(obj, f.name), f"{prefix}{f.name}."))
    elif isinstance(obj, (list, tuple)):
        for i, item in enumerate(obj):
            out.update(flatten(item, f"{prefix}{i}."))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = repr(obj)
    return out


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        return 2

    out_path = Path(sys.argv[1])
    config_path = Path(sys.argv[2]) if len(sys.argv) == 3 else None

    config = load_config(config_path) if config_path else load_config()
    snapshot = flatten(config)

    out_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {len(snapshot)} keys to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
