# MT5 Bridge

`finance` プロジェクト (Linux/stick PC) から **MetaTrader5 ターミナル**へ HTTP 経由でアクセスするための薄い FastAPI サービス。

`MetaTrader5` Python パッケージは Windows 専用のため、main PC (Windows) でこのブリッジを動かし、stick PC の `finance` から `http://<main-pc-ip>:8812/...` を叩く構成。

## Phase 1+2 のスコープ (現在)

- **read-only のみ**: `/health` `/account` `/quote/{symbol}` `/positions` `/symbols`
- **発注 endpoint は意図的に未実装** (資金 0 の本番口座保護)
- 認証は API キー (任意)、無設定なら LAN trust モードで起動

## Phase 3 以降の予定

- `BrokerAdapter` 抽象 (`finance` 側) の導入
- 発注 endpoint (`/order`) の実装 — `DRY_RUN=true` で約定シミュレーション、`DRY_RUN=false` + 別フラグで実発注
- ポジション変更 (modify/close)

---

## セットアップ (Windows main PC で実行)

### 前提

- Windows 10/11
- MetaTrader5 ターミナルがインストール・ログイン済み (口座は有効化されていればよい、デモ/本番問わず)
- Python 3.11+
- [uv](https://docs.astral.sh/uv/) がインストール済み

### 手順

```powershell
# 1. リポジトリを clone (既に main PC にあれば skip)
git clone <finance-repo-url>
cd finance

# 2. ブランチを切替
git checkout feature/mt5-integration

# 3. ブリッジディレクトリへ
cd mt5_bridge

# 4. 設定ファイルを用意
copy .env.example .env
notepad .env   # MT5_LOGIN / MT5_PASSWORD / MT5_SERVER を埋める

# 5. 依存をインストール (Windows なので MetaTrader5 が入る)
uv sync

# 6. 起動 (フォアグラウンド)
uv run python server.py
```

成功すると以下のようなログ:

```
[INFO] mt5_bridge.server: DRY_RUN=true | api_key=NOT SET (LAN trust mode)
[INFO] mt5_bridge.mt5_client: MT5 connected: login=12345678 server=OANDA-Live
INFO:     Uvicorn running on http://0.0.0.0:8812
```

ブラウザで `http://localhost:8812/health` にアクセスして JSON が返れば OK:

```json
{"status":"ok","mt5_connected":true,"dry_run":true,"server":"OANDA-Live","login":12345678}
```

### stick PC (Linux) からの疎通確認

```bash
curl http://<main-pc-ip>:8812/health
```

`mt5_connected: true` が返れば連携準備 OK。stick PC の `config/settings.yaml` で:

```yaml
mt5_bridge:
  enabled: true
  bridge_url: "http://192.168.1.10:8812"   # 実際の main PC IP
  heartbeat_interval_minutes: 60
```

を設定して finance を再起動すると、毎時 `data/state/mt5_heartbeat.jsonl` に稼働率が追記され始める。

---

## 常駐化 (任意、Phase 1+2 では手動起動でも可)

Windows でブリッジを常駐させる方法:

### 方法 A: タスクスケジューラ (簡易)

1. `schtasks` でログオン時起動を登録
2. 「最も高い特権で実行」「ユーザーがログオンしているかどうかにかかわらず実行」をオン

### 方法 B: NSSM で Windows サービス化 (推奨)

```powershell
# NSSM をダウンロード後
nssm install Mt5Bridge "C:\path\to\uv.exe" "run python server.py"
nssm set Mt5Bridge AppDirectory "C:\path\to\finance\mt5_bridge"
nssm start Mt5Bridge
```

---

## トラブルシュート

| 症状 | 原因 / 対処 |
|---|---|
| `ImportError: MetaTrader5 package is Windows-only` | Linux/Mac で動かそうとしている。Windows で実行すること |
| `MT5 initialize() failed: (-10004, ...)` | MT5 ターミナルが起動していない、または別ユーザーで起動している |
| `MT5 login failed: (-6, ...)` | 認証情報誤り。MT5 ターミナルで手動ログインが通ることを先に確認 |
| `mt5_connected: false` が続く | terminal_info() が None。MT5 ターミナルの自動売買ボタンをオンに、AlgoTrading 許可を確認 |
| stick PC から接続できない | Windows ファイアウォールで TCP 8812 を許可、または `BRIDGE_HOST=0.0.0.0` を確認 |

---

## セキュリティ注意

- `.env` は git で無視 (gitignore 済み) — 認証情報を絶対にコミットしない
- LAN 外公開は禁止 (このブリッジに認可・rate limit はない簡易設計)
- 公開する場合は `BRIDGE_API_KEY` を必ず設定
