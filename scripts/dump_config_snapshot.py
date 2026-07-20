"""AppConfig を JSON スナップショットへ書き出す。

config 移行の preflight 用。移行前の git revision でも動くよう、
移行後に導入する API (embedding 等) に依存しない書き方にしている。

使い方:
    uv run python scripts/dump_config_snapshot.py <出力先.json> [config/settings.yaml]

移行前スナップショットの取り方:
    このスクリプト自体は移行コミット群の一部として追加されたため、移行前の
    revision には存在しない。旧 worktree へコピーしてから実行する:

        git worktree add /tmp/finance-before <移行前のcommit>
        cp -r config/ /tmp/finance-before/          # config は gitignore のため要コピー
        cp scripts/dump_config_snapshot.py /tmp/finance-before/scripts/
        cd /tmp/finance-before && uv run python scripts/dump_config_snapshot.py /tmp/before.json

    旧 config が手元のバックアップ (tar/コピー) しかない場合は、それを
    /tmp/finance-before/config/ に展開してから実行する。

秘密値の扱い:
    キー名が token/secret/password/api_key 等を含むフィールドはハッシュに
    置換される (AppConfig には .env 由来の認証情報が取り込まれるため)。
    出力ファイルは 0600 に制限される。
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import load_config

# 値を平文で残してはいけないキー名のパターン。
# AppConfig には .env 由来の認証情報が取り込まれる
# (例: news_sources.feedly.access_token ← FEEDLY_ACCESS_TOKEN、MT5 の API キー)。
# 差分検出には「変わったかどうか」だけ分かればよいので、ハッシュに置換する。
_SECRET_KEY_PATTERN = re.compile(
    r"(?:^|[._])(?:"
    r"access_token|auth_token|refresh_token|api_token|"
    r"secret|secret_key|password|passwd|"
    r"api_key|apikey|access_key|credential|credentials"
    r")(?:$|[._])",
    re.IGNORECASE,
)

# 上の判定から除外するキー (名前は似ているが秘密ではない)。
# 例: llm.provider_config.max_tokens は応答トークン数の上限であって認証情報ではない。
_SECRET_EXCEPTIONS = re.compile(r"max_tokens$|_tokens$", re.IGNORECASE)


def _is_secret(key_path: str) -> bool:
    if _SECRET_EXCEPTIONS.search(key_path):
        return False
    return bool(_SECRET_KEY_PATTERN.search(key_path))


def _redact(value: str) -> str:
    """秘密値をハッシュに置換する。空値はそのまま (未設定と設定済みを区別するため)。"""
    if value in ("''", '""', "None"):
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"<redacted:sha256:{digest}>"


def flatten(obj, prefix: str = "") -> dict[str, str]:
    """dataclass を {ドット区切りパス: repr(値)} に平坦化する。

    値を repr 文字列にするのは、Path や enum を JSON 化するためと、
    型が変わった場合も差分として見えるようにするため。

    キー名が秘密情報を示すものはハッシュに置換する。移行検証に必要なのは
    「値が変わっていないこと」だけであり、平文である必要はない。
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
        key = prefix.rstrip(".")
        value = repr(obj)
        out[key] = _redact(value) if _is_secret(key) else value
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
    # 秘密値はハッシュ化済みだが、config 全体の構造が読めるため所有者のみに制限する。
    try:
        os.chmod(out_path, 0o600)
    except OSError:  # Windows 等で chmod が効かない環境は警告に留める
        print(f"warning: could not restrict permissions on {out_path}", file=sys.stderr)

    redacted = sum(1 for v in snapshot.values() if v.startswith("<redacted:"))
    print(f"wrote {len(snapshot)} keys to {out_path} ({redacted} redacted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
