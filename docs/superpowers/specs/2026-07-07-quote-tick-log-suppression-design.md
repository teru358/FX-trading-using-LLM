# quote tick ログ抑制 design (2026-07-07)

## 背景 / 問題

quote-stream producer が trade pairs を短周期 (既定 2 秒、`quote_stream_poll_seconds`) で
polling するようになって以降、成功した定常ポーリングの記録が毎秒ターミナルとログを
汚染し、本来見たいイベント ([SIGNAL] / [ORDER] / [MT5_BRIDGE] 等) が埋もれる。

ノイズの発生源は producer 本体ではなく、両端の HTTP 層が 1 リクエスト = 1 行を
出力する構造にある:

1. **app 側 (stick / Fiosracht)**
   - httpx が 1 リクエストごとに INFO で
     `HTTP Request: GET http://.../quote/... "HTTP/1.1 200 OK"` を出力する。
   - `src/logging_setup.py` は yfinance / trafilatura を抑制済みだが httpx は未抑制。
   - root logger が DEBUG のため、main log ファイルには httpcore の DEBUG 行
     (接続・送受信で 1 リクエスト複数行) まで流入する。
   - 影響: ターミナル (RichHandler, INFO) + main log (file handler, DEBUG) の両方。

2. **bridge 側 (192.168.1.16 Windows)**
   - `mt5_bridge/server.py` の uvicorn `log_config` で `uvicorn.access` が INFO 有効。
     `GET /quote/{symbol} 200` が 1 リクエストごとに stdout へ出る。
   - bridge.log ファイルへは propagate=False のため流れておらず、汚染はターミナルのみ。

## 決定事項

**成功 tick を完全抑制する** (2026-07-07 ブレストで確定)。

- ターミナル・ログファイルの両方から定常ポーリングの成功行を消す。
- エラー・発注系・その他エンドポイントのログは従来通り残す。
- 検討した代替案:
  - ターミナルのみ抑制 (file には全行残す) — ログ肥大と grep 性の悪さが残るため不採用。
  - 完全抑制 + 定期サマリ行 — 実装増に見合う必要が現状ないため見送り
    (必要になれば後付け可能)。
  - **WebSocket 化** — handshake 1 行のみになりノイズは構造的に消えるが、
    接続管理・再接続・freshness wall 再設計まで必要で、ログ対策としては過剰投資。
    MT5 Python API は push 購読を持たず bridge 内部ポーリングは残るため、
    従来の「メリット少」判断はログ観点を加えても変わらない。不採用。

## 変更内容

### 変更 1 — app 側: httpx / httpcore ロガーの降格

`src/logging_setup.py` の `setup_logging()` に追加 (yfinance 等の既存抑制と同じ流儀):

```python
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
```

- 毎リクエストの INFO 行がターミナル・main log 双方から消える。
  httpcore の DEBUG 行も main log から消える。
- httpx の通信エラーは例外として呼び出し側 (fetcher / broker / LLM client) が
  自前の prefix 付きログで記録する構造のため、エラー可視性は失われない。
- 副作用: LLM (llama.cpp / OpenAI 等) への httpx リクエスト行も消えるが、
  各 client が自前ログを持つため実害なし。

### 変更 2 — bridge 側: uvicorn.access にポーリング除外フィルタ

`mt5_bridge/server.py` に `_PollingAccessFilter(logging.Filter)` を追加し、
`main()` の `log_config` で `access` ハンドラに配線する。

判定仕様:

- uvicorn.access のレコードは
  `record.args == (client_addr, method, full_path, http_version, status_code)`。
- **drop 条件**: `method == "GET"` かつ
  (`full_path.startswith("/quote/")` または `full_path == "/health"`) かつ
  `200 <= status_code < 300`。
- それ以外 (POST /order、/close、4xx/5xx、他エンドポイント) は keep。
- `record.args` が想定形状でない場合は **True を返す (fail-open)** —
  落とすより出す方が安全。

## テスト

- **app 側** (`tests/`): `setup_logging()` 実行後に `logging.getLogger("httpx")` /
  `logging.getLogger("httpcore")` の level が WARNING であることを検証。
- **bridge 側** (`mt5_bridge/tests/`): uvicorn 形式の LogRecord を組み立てて
  フィルタの drop/keep を検証:
  - `GET /quote/USDJPY 200` → drop
  - `GET /health 200` → drop
  - `GET /quote/USDJPY 500` → keep
  - `POST /order 200` → keep
  - `GET /ohlcv/USDJPY 200` → keep
  - args 形状不正 (None / 要素数不足) → keep (fail-open)

## デプロイ

- 両プロセスの再起動が必要: bridge (192.168.1.16) と app (stick=Live / Fiosracht=paper)。
- stick への rsync は既知の必須除外セットを厳守 (data/ 等、2026-04-25 事故の再発防止)。

## スコープ外

- 定期サマリ行 ([QUOTE-STREAM] N pairs polled 等) の追加。
- WebSocket / push 型 quote 配信への移行。
- MagicMock パスリーク (repo 直下のゴミファイル) の掃除 — 別課題。
