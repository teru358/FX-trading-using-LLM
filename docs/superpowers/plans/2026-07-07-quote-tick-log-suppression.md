# quote tick ログ抑制 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** quote-stream producer の毎秒ポーリングが出す成功 HTTP ログを app 側 (httpx/httpcore INFO 行) と bridge 側 (uvicorn.access の GET /quote 2xx 行) の両方で抑制する。

**Architecture:** app 側は `setup_logging()` で httpx/httpcore ロガーをグローバルに WARNING へ降格 (既存の yfinance 抑制と同じ発想の一元設定)。bridge 側は `_PollingAccessFilter` を uvicorn.access ハンドラに配線し、`GET /quote/* 2xx` のみ drop する。log_config 生成は `_build_log_config()` に関数化して配線をテスト可能にする。

**Tech Stack:** Python logging (Filter / dictConfig), pytest, uvicorn access log。

**Spec:** `docs/superpowers/specs/2026-07-07-quote-tick-log-suppression-design.md`

**Branch:** `feat/planner-watch-loop` (finance repo)

**前提知識 (このリポジトリ固有):**
- app 側テストは finance repo root から `uv run pytest tests/...` で実行する。
- bridge は独立パッケージ。テストは `mt5_bridge/` ディレクトリから `uv run pytest tests/...`
  で実行する (pythonpath="." のため `import server` がそのまま通る)。
- bridge テストが `import server` するのは既存パターン (`test_order_preflight_lock.py` 参照)。
  server.py はモジュールロード時に `mt5_bridge/logs/` を mkdir し logging.basicConfig を
  実行するが、既存テストで問題になっていない。
- docs/ は .gitignore されているため、docs 配下のコミットは `git add -f` が必要。
- uvicorn.access の LogRecord は
  `record.args == (client_addr, method, full_path, http_version, status_code)` の 5-tuple。
  filter は `record.args` だけ読む (getMessage() は呼ばない)。

---

### Task 1: app 側 — httpx/httpcore ロガーの WARNING 降格

**Files:**
- Create: `tests/test_logging_setup.py`
- Modify: `src/logging_setup.py` (setup_logging 内、uvicorn.access フィルタ行の直後)

- [ ] **Step 1: Write the failing test**

`tests/test_logging_setup.py` を新規作成:

```python
"""setup_logging() のサードパーティロガー抑制の検証。

quote-stream producer が毎秒 /quote を polling するため、httpx の
`HTTP Request: GET ... 200 OK` INFO 行と httpcore の DEBUG 行がターミナル・
main log を汚染する。setup_logging() がこれらを WARNING に降格することを確認する。
(spec: docs/superpowers/specs/2026-07-07-quote-tick-log-suppression-design.md)
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.logging_setup import setup_logging


@pytest.fixture
def restore_logging():
    """setup_logging() が root logger をグローバルに書き換えるため、テスト後に復元する。"""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_third_party = {
        name: logging.getLogger(name).level for name in ("httpx", "httpcore")
    }
    yield
    for h in root.handlers:
        if h not in saved_handlers:
            h.close()  # tmp_path 内のログファイルハンドルを解放
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    for name, lvl in saved_third_party.items():
        logging.getLogger(name).setLevel(lvl)


def _logging_cfg():
    """setup_logging() が参照する属性だけ持つ最小 config。"""
    return SimpleNamespace(
        file="logs/main.log",
        activity_log_file="logs/activity.log",
        level="INFO",
        rotate_timing="10MB",
        backup_count=1,
    )


def test_httpx_and_httpcore_demoted_to_warning(restore_logging, tmp_path):
    setup_logging(_logging_cfg(), tmp_path)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
```

- [ ] **Step 2: Run test to verify it fails**

Run (finance repo root): `uv run pytest tests/test_logging_setup.py -v`

Expected: FAIL — `assert 0 == 30` (未設定ロガーの level は NOTSET=0 のため WARNING=30 と一致しない)

- [ ] **Step 3: Write minimal implementation**

`src/logging_setup.py` の `setup_logging()` 内、以下の既存行:

```python
    # uvicorn アクセスログに [API] プレフィックスを付与 → activity.log に流す
    logging.getLogger("uvicorn.access").addFilter(_ApiAccessPrefixFilter())
```

の直後に追加:

```python
    # httpx は 1 リクエストごとに INFO で `HTTP Request: ...` を出す。quote-stream の
    # 毎秒 polling でターミナル・main log が汚染されるため WARNING に降格する。
    # (プロセス内全 HTTP 成功行が消える意図的なグローバル抑制 — spec 2026-07-07 参照。
    #  transport エラーは httpx 自体がログせず例外伝播、非 2xx は caller が捕捉ログする)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_logging_setup.py -v`

Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/test_logging_setup.py src/logging_setup.py
git commit -m "fix: httpx/httpcore ロガーを WARNING に降格し毎秒 quote polling のログ汚染を抑制"
```

---

### Task 2: bridge 側 — _PollingAccessFilter (drop/keep 判定)

**Files:**
- Create: `mt5_bridge/tests/test_access_log_filter.py`
- Modify: `mt5_bridge/server.py` (`_UvicornNameRewriter` クラスの直後に追加)

- [ ] **Step 1: Write the failing test**

`mt5_bridge/tests/test_access_log_filter.py` を新規作成:

```python
"""_PollingAccessFilter の drop/keep 判定テスト。

uvicorn.access のレコードは
`record.args == (client_addr, method, full_path, http_version, status_code)`。
GET /quote/* の 2xx (毎秒 polling の成功) だけ drop し、それ以外は keep する。
(spec: docs/superpowers/specs/2026-07-07-quote-tick-log-suppression-design.md)
"""
from __future__ import annotations

import logging

import server


def _access_record(args) -> logging.LogRecord:
    """uvicorn.access が出す形の LogRecord を組み立てる。"""
    return logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname=__file__, lineno=0,
        msg='%s - "%s %s HTTP/%s" %d', args=args, exc_info=None,
    )


def _filter() -> logging.Filter:
    return server._PollingAccessFilter()


def test_quote_2xx_dropped():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/USDJPY", "1.1", 200))
    assert _filter().filter(rec) is False


def test_quote_204_dropped():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/EURUSD", "1.1", 204))
    assert _filter().filter(rec) is False


def test_quote_5xx_kept():
    rec = _access_record(("192.168.1.10:50000", "GET", "/quote/USDJPY", "1.1", 500))
    assert _filter().filter(rec) is True


def test_health_2xx_kept():
    """/health は低頻度 (preflight / halt resume / app proxy) のため抑制しない。"""
    rec = _access_record(("192.168.1.10:50000", "GET", "/health", "1.1", 200))
    assert _filter().filter(rec) is True


def test_post_order_kept():
    rec = _access_record(("192.168.1.10:50000", "POST", "/order", "1.1", 200))
    assert _filter().filter(rec) is True


def test_ohlcv_2xx_kept():
    rec = _access_record(("192.168.1.10:50000", "GET", "/ohlcv/USDJPY", "1.1", 200))
    assert _filter().filter(rec) is True


def test_malformed_args_none_kept():
    """args が想定形状でない場合は fail-open (落とすより出す)。"""
    assert _filter().filter(_access_record(None)) is True


def test_malformed_args_short_tuple_kept():
    assert _filter().filter(_access_record(("client", "GET", "/quote/USDJPY"))) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run (mt5_bridge ディレクトリで): `uv run pytest tests/test_access_log_filter.py -v`

Expected: FAIL — `AttributeError: module 'server' has no attribute '_PollingAccessFilter'`

- [ ] **Step 3: Write minimal implementation**

`mt5_bridge/server.py` の `_UvicornNameRewriter` クラス定義 (filter メソッドの
`return True` まで) の直後に追加:

```python
class _PollingAccessFilter(logging.Filter):
    """quote polling の成功 access log を落とすフィルタ。

    quote-stream producer (app 側) が全 trade pair を短周期 polling するため、
    `GET /quote/{symbol} 2xx` の access log が毎秒出てターミナルを汚染する。
    成功した定常ポーリングのみ drop し、エラー (非 2xx)・他 endpoint は keep する。
    /health は低頻度 (発注 preflight / halt resume / app proxy) のため対象外。
    uvicorn.access の record.args は
    (client_addr, method, full_path, http_version, status_code) の 5-tuple。
    想定外の形状は keep (fail-open — 落とすより出す方が安全)。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != 5:
            return True
        _client, method, path, _ver, status = args
        if method != "GET" or not isinstance(path, str) or not path.startswith("/quote/"):
            return True
        return not (isinstance(status, int) and 200 <= status < 300)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_access_log_filter.py -v`

Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add tests/test_access_log_filter.py server.py
git commit -m "feat: bridge access log から GET /quote 2xx を drop する _PollingAccessFilter を追加"
```

(mt5_bridge ディレクトリからの相対パス。finance root からなら
`git add mt5_bridge/tests/test_access_log_filter.py mt5_bridge/server.py`)

---

### Task 3: bridge 側 — _build_log_config() 関数化 + フィルタ配線

**Files:**
- Modify: `mt5_bridge/tests/test_access_log_filter.py` (配線テスト追記)
- Modify: `mt5_bridge/server.py` (`main()` の log_config を関数抽出)

- [ ] **Step 1: Write the failing test**

`mt5_bridge/tests/test_access_log_filter.py` の末尾に追記:

```python
def test_log_config_wires_polling_filter_into_access_handler():
    """main() 内の配線漏れを検出する: access ハンドラに polling filter が入っている。"""
    cfg = server._build_log_config()
    assert "polling_access" in cfg["handlers"]["access"]["filters"]
    assert cfg["filters"]["polling_access"]["()"] is server._PollingAccessFilter


def test_log_config_keeps_existing_uvicorn_name_fix():
    """既存の uvicorn.error 表示名書き換えが関数化後も残っている (回帰防止)。"""
    cfg = server._build_log_config()
    assert "uvicorn_name_fix" in cfg["handlers"]["default"]["filters"]
    assert cfg["loggers"]["uvicorn.access"]["handlers"] == ["access"]
```

- [ ] **Step 2: Run test to verify it fails**

Run (mt5_bridge ディレクトリで): `uv run pytest tests/test_access_log_filter.py -v`

Expected: 新 2 件が FAIL — `AttributeError: module 'server' has no attribute '_build_log_config'`。既存 8 件は PASS のまま。

- [ ] **Step 3: Write minimal implementation**

`mt5_bridge/server.py` の `main()` を次の 2 関数に書き換える
(現在 `main()` 内にある log_config dict をほぼそのまま移動し、
`filters` に `polling_access` を追加、`access` ハンドラに配線する):

```python
def _build_log_config() -> dict:
    """uvicorn 用 log_config を生成する。

    main() インラインだと access ハンドラへのフィルタ配線漏れをテストで
    検出できないため関数化している (tests/test_access_log_filter.py が検証)。

    - uvicorn のデフォルトログは `INFO: ...` でタイムスタンプ無し
      → アプリと同じフォーマット (時刻付き) に統一、`uvicorn.error` 表記も整理
    - access ハンドラには _PollingAccessFilter を配線し、quote polling の
      成功行 (GET /quote/* 2xx) を落とす
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "uvicorn_name_fix": {
                "()": _UvicornNameRewriter,
            },
            "polling_access": {
                "()": _PollingAccessFilter,
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
                "filters": ["polling_access"],
            },
        },
        "loggers": {
            "uvicorn":        {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error":  {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.access": {"handlers": ["access"],  "level": "INFO", "propagate": False},
        },
    }


def main() -> None:
    """`python server.py` で uvicorn を直接起動する。"""
    import uvicorn

    cfg = load_settings()

    # app オブジェクト直渡し (reload 不要、import 文字列のパス問題を回避)
    uvicorn.run(
        app, host=cfg.host, port=cfg.port,
        log_level="info", log_config=_build_log_config(),
    )
```

注意: 既存 `main()` 冒頭のコメント 2 行 (`# uvicorn のデフォルトログは ...`) は
`_build_log_config()` の docstring に移動済みなので削除する。

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/ -v`

Expected: bridge 全テスト PASS (test_access_log_filter.py の 10 件を含む)

- [ ] **Step 5: Commit**

```bash
git add tests/test_access_log_filter.py server.py
git commit -m "refactor: log_config 生成を _build_log_config() に関数化し polling filter を access ハンドラに配線"
```

---

### Task 4: 全 suite 検証

**Files:** なし (検証のみ)

- [ ] **Step 1: app 側 full suite**

Run (finance repo root): `uv run pytest`

Expected: 全件 PASS (直近の green 基準は 1388 passed。新規 1 件が加わり 1389+ passed / 0 failed)

- [ ] **Step 2: bridge 側 full suite**

Run (mt5_bridge ディレクトリで): `uv run pytest`

Expected: 全件 PASS (既存 4 ファイル + 新規 test_access_log_filter.py)

- [ ] **Step 3: 手動スモーク (任意、環境があれば)**

bridge をローカル起動し `/quote` と `/health` を叩いて access log の出方を確認:

```bash
# terminal 1 (mt5_bridge ディレクトリ)
uv run python server.py
# terminal 2
curl -s -H "X-API-Key: <bridge_api_key>" http://127.0.0.1:8001/health   # → access log に出る
curl -s -H "X-API-Key: <bridge_api_key>" http://127.0.0.1:8001/quote/USDJPY  # 2xx なら access log に出ない
```

Expected: /health 行は表示、/quote 2xx 行は非表示。MT5 未接続で /quote が 5xx を返す場合はその行が表示される (エラーは keep の確認になる)。

---

## デプロイ (実装完了後の運用作業 — コード変更なし)

1. **bridge (192.168.1.16 Windows)**: `mt5_bridge/server.py` を配布して bridge プロセス再起動。
2. **app (stick=Live / Fiosracht=paper)**: `src/logging_setup.py` を含む変更を rsync してプロセス再起動。
   **stick への rsync は既知の必須除外セット厳守** (data/ 等 — 2026-04-25 の data 消失事故の再発防止手順に従う)。
3. 再起動後の確認: app ターミナルに `HTTP Request:` 行が出ないこと、bridge ターミナルに
   `GET /quote/* 200` が出ないこと、`[SIGNAL]`/`[ORDER]` 等の既存イベントログは出ること。
