"""tui.py の基本テスト。"""


def test_tui_app_import():
    """TuiApp がインポートできること。"""
    from src.tui import TuiApp
    assert TuiApp is not None


def test_tui_app_has_widgets():
    """TuiApp が必要なウィジェットを定義していること。"""
    from src.tui import TuiApp
    app = TuiApp()
    widgets = list(app.compose())
    widget_types = [type(w).__name__ for w in widgets]
    assert "RichLog" in widget_types
    assert "Input" in widget_types


def test_tui_log_handler_import():
    """TuiLogHandler がインポートできること。"""
    from src.tui import TuiLogHandler
    assert TuiLogHandler is not None
