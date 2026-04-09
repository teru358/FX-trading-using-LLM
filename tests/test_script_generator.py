"""Pine Script生成のテスト。"""
from src.tradingview.script_generator import generate_signal_pine


class TestGenerateSignalPine:
    def test_long_signal(self):
        script = generate_signal_pine(
            pair="USD/JPY",
            direction="long",
            entry_price=158.250,
            stop_loss=157.500,
            take_profit=160.500,
            confidence=0.85,
            reason="Strong bullish momentum",
        )
        assert "//@version=6" in script
        assert "158.25" in script
        assert "157.5" in script
        assert "160.5" in script
        assert "long" in script

    def test_short_signal(self):
        script = generate_signal_pine(
            pair="EUR/USD",
            direction="short",
            entry_price=1.17000,
            stop_loss=1.17500,
            take_profit=1.16000,
            confidence=0.70,
            reason="Bearish reversal",
        )
        assert "short" in script
        assert "1.175" in script
        assert "1.16" in script

    def test_hold_signal(self):
        """HOLD時はエントリーラインなし。"""
        script = generate_signal_pine(
            pair="USD/JPY",
            direction="hold",
            entry_price=0,
            stop_loss=0,
            take_profit=0,
            confidence=0.45,
            reason="No clear signal",
        )
        assert "hold" in script
        # hold時はline.newブロックが出力されない（Jinja2 ifで除外）
        assert "line.new" not in script
