"""config パッケージ共通の例外。"""
from __future__ import annotations


class ConfigError(ValueError):
    """設定の起動時バリデーションで検出された致命的エラー。

    ValueError を継承しているのは既存互換のため (loader.ConfigError が
    元から ValueError 派生であり、ValueError で捕捉しているコードがある)。
    """
