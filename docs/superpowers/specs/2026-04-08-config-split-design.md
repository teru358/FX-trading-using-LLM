# 設定ファイル分割設計

## 目的

運用パラメータとデータ定義を分離し、誤編集リスクを減らす。

## 分割構成

```
config/
  settings.yaml        — 運用パラメータ (~210行)
  instruments.yaml     — 銘柄定義 + price_provider設定 (~80行)
  news_sources.yaml    — キーワード + RSSフィード + Feedly (~130行)
```

## 各ファイルの内容

### settings.yaml (残留)

- logging, api, llm, gemini, openai, claude
- trading (全パラメータ)
- price_monitor
- schedule
- analysis (indicators, chart_patterns, forecast)
- news_collection
- rag
- notification

### instruments.yaml (新規)

- `price_provider` セクション (realtime_provider, twelvedata設定含む)
- `instruments` リスト (全銘柄定義)

### news_sources.yaml (新規)

- `keywords` セクション (global, japan)
- `news_sources` セクション (feeds_fx, feeds_global, feeds_japan, feedly)

## 読み込みロジック

`src/config.py` の `load_config()` を変更:

1. `settings.yaml` を読み込み → `raw` dict
2. `instruments.yaml`, `news_sources.yaml` が存在すれば読み込み
3. 分割ファイルのキーを `raw` にマージ（**分割ファイル優先**: 同一キーが settings.yaml にもあれば分割ファイルで上書き）
4. 分割ファイルが存在しない場合はスキップ（config.py のデフォルト値を使用、エラーにしない）

### マージ関数

```python
def _merge_split_configs(base: dict, config_dir: Path) -> dict:
    for fname in ["instruments.yaml", "news_sources.yaml"]:
        fpath = config_dir / fname
        if fpath.exists():
            with open(fpath, encoding="utf-8") as f:
                extra = yaml.safe_load(f)
            if extra and isinstance(extra, dict):
                for key, value in extra.items():
                    base[key] = value
    return base
```

## 影響範囲

- **変更**: `src/config.py` (load_config にマージ処理追加)
- **新規**: `config/instruments.yaml`, `config/news_sources.yaml`
- **変更**: `config/settings.yaml` (分割したセクションを削除)
- **変更**: `config/settings.yaml.example` (同期)
- **変更なし**: 他の全コード (AppConfig経由のアクセスは不変)

## 移行

- settings.yaml から instruments, price_provider, keywords, news_sources セクションを削除
- 分割ファイルに移動
- settings.yaml.example も同様に同期
