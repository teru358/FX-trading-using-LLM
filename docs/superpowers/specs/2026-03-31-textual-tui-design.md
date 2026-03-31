# Textual TUI 設計

## 概要

サーバー側CLI（`src/cli.py`）を Textual ベースの TUI に移行する。
上部にログ・コマンド結果のスクロール領域、下部にコマンド入力欄の2分割レイアウト。
既存の `cli.py` はフォールバック用に維持し、`--no-tui` で切替可能。

## 背景・動機

- 現在のCLIはログ出力とコマンド入力が同一ストリームで混在する
- スケジューラのログが流れている最中にコマンドプロンプトが割り込む
- curses による分割表示は Rich の色表現再現が困難で断念済み
- Textual は Rich 作者製の TUI フレームワークで Rich との親和性が最も高い

## スコープ

### 含む

- `src/tui.py` 新規作成（Textual TUIアプリ）
- `main.py` に `--no-tui` フラグ追加
- カスタム logging ハンドラ（ログを RichLog に転送）
- 既存コマンドの TUI 対応（status, run *, compare, ask, close, feeds, notify, help, quit）
- `pyproject.toml` に textual 依存追加

### 含まない

- `client.py` の TUI化（将来対応）
- WebSocket 移行（将来対応）
- `edit` コマンド（初期バージョンではオミット。案内メッセージのみ表示）
- ダッシュボード・サイドバー等の高度なレイアウト（将来拡張）
- `src/cli.py` の変更（フォールバック用にそのまま残す）

## 画面レイアウト

```
┌─ FX Trading Bot ─────────────────────────────┐
│                                               │
│  RichLog ウィジェット（スクロール可能）         │
│                                               │
│  スケジューラのログ出力                        │
│  コマンド実行結果（Richテーブル等）             │
│  シグナル・取引通知                            │
│                                               │
│                                               │
├───────────────────────────────────────────────┤
│ > コマンド入力（Input ウィジェット）            │
└───────────────────────────────────────────────┘
```

- 上部: Textual `RichLog` — Rich のテーブル・パネル・色付きテキストをそのまま表示。ログもコマンド結果もここに流れる。スクロール可能。
- 下部: Textual `Input` — コマンド入力欄。Enter で実行。1行固定。
- 比率: ログ領域が画面の大部分、入力が1行。

## アーキテクチャ

### ファイル構成

#### 新規ファイル

| ファイル | 役割 |
|---|---|
| `src/tui.py` | Textual TUIアプリ本体（RichLog + Input + TuiLogHandler + コマンドディスパッチ） |

#### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `main.py` | `--no-tui` フラグ追加。デフォルトで `TuiApp.run()` を起動 |
| `pyproject.toml` | `textual` 依存追加 |

#### 変更しないもの

- `src/cli.py` — フォールバック用にそのまま残す
- `src/api/server.py` — 変更なし
- `src/logging_setup.py` — ファイルハンドラは変更なし
- スケジューラ・ジョブ関連 — 変更なし

### スレッド構成

```
main.py
  ├─ スケジューラスレッド（バックグラウンド、変更なし）
  ├─ API サーバースレッド（デーモン、変更なし）
  └─ TuiApp.run()（メインスレッド、Textualイベントループ）
```

- `run_commands()` の代わりに `TuiApp.run()` を呼ぶだけ
- スケジューラ・APIサーバーのスレッド構成は一切変更しない
- コマンド実行時は `_job_lock` を取得してスケジューラと排他制御（現行と同じ）

### ログの流し込み

カスタム logging ハンドラで Python logging の出力を RichLog ウィジェットに転送する。

```python
class TuiLogHandler(logging.Handler):
    """logging の出力を RichLog ウィジェットに転送するハンドラ。"""
    def __init__(self, rich_log_widget):
        super().__init__()
        self.widget = rich_log_widget

    def emit(self, record):
        msg = self.format(record)
        self.widget.write(msg)
```

- TUI 起動時にルートロガーにこのハンドラを追加
- ファイルログ（`finance.log`）は従来どおり維持
- コンソールハンドラ（`StreamHandler`）は TUI 起動時に無効化（画面競合防止）

### コマンド処理

TUI の `Input` ウィジェットに入力 → Textual のワーカースレッドでコマンド実行 → 結果を `RichLog` に出力。

ワーカースレッドで実行する理由: 重い処理（`run trade`, `compare` 等）が TUI のイベントループをブロックしないため。

コマンド処理は `tui.py` 内にディスパッチャを実装する。各コマンドの実処理は既存モジュールの公開関数（`run_news_view`, `run_tech_view`, `run_trading_cycle` 等）を直接呼び出す。
`_cmd_status` のような `cli.py` のプライベート関数は再利用せず、TUI用に同等のロジックを `tui.py` 内に実装する（Rich Console 出力を RichLog に書く必要があるため）。
Rich の出力先を RichLog にリダイレクトするため、`Console(file=...)` で StringIO にキャプチャし、結果を RichLog に流す方式を採る。

### edit コマンドの扱い

初期バージョンではオミット。`edit` 入力時に以下を RichLog に表示:

```
TUIモードでは edit は使用できません。別ターミナルで実行してください:
  vim config/user_notes.md
```

## 起動フロー

```
python main.py              → TUI（デフォルト）
python main.py --no-tui     → 従来CLI（src/cli.py の run_commands()）
python main.py --daemon     → デーモンモード（変更なし）
```

`main.py` の分岐:

```python
if args.daemon:
    # デーモンモード（変更なし）
elif args.no_tui:
    run_commands(...)  # 従来CLI
else:
    from src.tui import TuiApp
    app = TuiApp(...)
    app.run()
```

## エラーハンドリング

- Textual インストール失敗 / インポートエラー → 自動で `run_commands()` にフォールバック
- コマンド実行中のエラー → RichLog にエラーメッセージ表示（TUI は継続）
- `Ctrl+C` → TUI を終了し `_stop` イベントをセット（スケジューラも停止）
