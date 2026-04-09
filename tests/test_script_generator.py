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

    def test_with_technical_data(self):
        """テクニカルデータ付きでサポレジ・パターンが反映される。"""
        script = generate_signal_pine(
            pair="USD/JPY",
            direction="long",
            entry_price=158.250,
            stop_loss=157.500,
            take_profit=160.500,
            confidence=0.85,
            reason="Bullish",
            bias_score=0.42,
            trend_direction="uptrend",
            key_support=157.200,
            key_resistance=159.800,
            swing_highs=[159.500, 159.100],
            swing_lows=[157.800, 157.300],
            patterns="hammer, bullish_engulfing",
        )
        assert "Support" in script
        assert "157.2" in script
        assert "Resist" in script
        assert "159.8" in script
        assert "159.5" in script  # swing high
        assert "157.8" in script  # swing low
        assert "hammer" in script
        assert "0.42" in script   # bias_score
        assert "uptrend" in script

    def test_without_optional_data(self):
        """オプションデータなしでもエラーなく生成される。"""
        script = generate_signal_pine(
            pair="EUR/USD",
            direction="short",
            entry_price=1.17000,
            stop_loss=1.17500,
            take_profit=1.16000,
            confidence=0.70,
            reason="Bearish",
        )
        # key_support/resistance未指定時はライン描画ブロックが出力されない
        assert "keySupport" not in script
        assert "keyResist" not in script
