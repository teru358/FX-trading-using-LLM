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
- **app 側の抑制は quote 限定ではない**: httpx/httpcore ロガーのグローバル降格のため、
  プロセス内の全 HTTP 成功ログ (LLM / Discord / Feedly / bridge /ohlcv 等) が消える。
  これは意図的な選択 (ブレスト時に「LLM への httpx リクエスト行も消える」前提で承認済み)。
  quote 限定のメッセージ内容フィルタは URL 文字列への結合が brittle なため不採用。
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
- **エラー可視性への影響** (codex review 反映で厳密化):
  - 降格で失われるのは httpx の成功/ステータス行 (非 2xx 含む) のみ。
    transport エラー (timeout / 接続不能) は httpx 自体が元々ログせず例外で伝播する。
  - 非 2xx は `raise_for_status()` で例外化され caller が捕捉してログする。
    確認済みサイト: `mt5_ohlcv_fetcher.py:170,283` / `mt5_bridge_broker.py:144,308,326` /
    `bridge_health_gate.py:132,175` / `twelvedata_fetcher.py:97,102` /
    `llm_queue.py:115` (`logger.exception`) / `reflector.py:180`。
  - **留意**: LLM client 自体は DEBUG 開始行のみ (`openai_client.py:64` 等) で
    自前のエラーログを持たない。LLM のエラー可視性は queue / pipeline 層の
    既存 exception ログに依存する (上記 llm_queue で担保)。
- 副作用: LLM / Discord / Feedly 等への httpx 成功行も消える (§決定事項の通り意図的)。

### 変更 2 — bridge 側: uvicorn.access にポーリング除外フィルタ

`mt5_bridge/server.py` に `_PollingAccessFilter(logging.Filter)` を追加し、
`log_config` の `access` ハンドラに配線する。配線漏れをテストで検出できるよう、
`main()` 内にインラインで書かれている log_config 生成を **`_build_log_config()`
として関数化**する (codex review 反映)。

判定仕様:

- uvicorn.access のレコードは
  `record.args == (client_addr, method, full_path, http_version, status_code)`。
- **drop 条件**: `method == "GET"` かつ `full_path.startswith("/quote/")` かつ
  `200 <= status_code < 300`。
- それ以外 (POST /order、/close、4xx/5xx、他エンドポイント) は keep。
- **`/health` は抑制しない** (codex review 反映): bridge /health へのアクセスは
  発注プリフライト (`bridge_health_gate`) / halt resume / app `/api/health` プロキシ
  経由の低頻度のみで毎秒ノイズに寄与しておらず、bridge 復旧確認・heartbeat 診断の
  到達成功ログとして残す価値の方が大きい。
- `record.args` が想定形状でない場合は **True を返す (fail-open)** —
  落とすより出す方が安全。

## テスト

- **app 側** (`tests/`): `setup_logging()` 実行後に `logging.getLogger("httpx")` /
  `logging.getLogger("httpcore")` の level が WARNING であることを検証。
- **bridge 側** (`mt5_bridge/tests/`):
  - フィルタ単体: uvicorn 形式の LogRecord を組み立てて drop/keep を検証:
    - `GET /quote/USDJPY 200` → drop
    - `GET /quote/USDJPY 500` → keep
    - `GET /health 200` → keep (抑制対象外)
    - `POST /order 200` → keep
    - `GET /ohlcv/USDJPY 200` → keep
    - args 形状不正 (None / 要素数不足) → keep (fail-open)
  - 配線: `_build_log_config()` の戻り値で `handlers["access"]["filters"]` に
    `_PollingAccessFilter` が入っていることを検証 (main() 内配線漏れの検出)。

## デプロイ

- 両プロセスの再起動が必要: bridge (192.168.1.16) と app (stick=Live / Fiosracht=paper)。
- stick への rsync は既知の必須除外セットを厳守 (data/ 等、2026-04-25 事故の再発防止)。

## スコープ外

- 定期サマリ行 ([QUOTE-STREAM] N pairs polled 等) の追加。
- WebSocket / push 型 quote 配信への移行。
- MagicMock パスリーク (repo 直下のゴミファイル) の掃除 — 別課題。
