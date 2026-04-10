"""Pine Script生成のテスト。"""
import pytest
from src.tradingview.script_generator import (
    PositionData,
    SignalData,
    generate_signal_pine,
    generate_multi_signal_pine,
)


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


class TestGenerateMultiSignalPine:
    def test_signals_only_backward_compatible(self):
        """positions 未指定時は従来通りシグナルのみ描画される。"""
        signals = [
            SignalData(
                pair="USD/JPY",
                tv_ticker="USDJPY",
                direction="long",
                entry_price=158.25,
                stop_loss=157.50,
                take_profit=160.50,
                confidence=0.80,
                reason="Test",
            )
        ]
        script = generate_multi_signal_pine(signals)
        assert "//@version=6" in script
        assert "158.25" in script
        assert "157.5" in script
        assert "160.5" in script

    def test_positions_block_rendered(self):
        """positions が渡されるとポジション線ブロックが描画される。"""
        positions = [
            PositionData(
                pair="USD/JPY",
                tv_ticker="USDJPY",
                direction="buy",
                entry_price=158.85,
                stop_loss=157.26,
                take_profit=162.03,
                position_size=10000.0,
                opened_at_ms=1712739600000,
            )
        ]
        script = generate_multi_signal_pine(signals=[], positions=positions)
        assert "// ─ POSITIONS ─" in script
        assert "158.85" in script
        assert "157.26" in script
        assert "162.03" in script
        assert "1712739600000" in script  # opened_at_ms
        assert "xloc.bar_time" in script
        assert "▲" in script  # buy → ▲

    def test_position_sell_uses_down_arrow(self):
        positions = [
            PositionData(
                pair="EUR/USD",
                tv_ticker="EURUSD",
                direction="sell",
                entry_price=1.1700,
                stop_loss=1.1750,
                take_profit=1.1600,
                position_size=10000.0,
                opened_at_ms=1712739600000,
            )
        ]
        script = generate_multi_signal_pine(signals=[], positions=positions)
        assert "▼" in script

    def test_signals_and_positions_coexist(self):
        """シグナルとポジション両方を同時描画できる。"""
        signals = [
            SignalData(
                pair="USD/JPY",
                tv_ticker="USDJPY",
                direction="long",
                entry_price=159.0,
                stop_loss=158.0,
                take_profit=161.0,
                confidence=0.8,
                reason="New signal",
            )
        ]
        positions = [
            PositionData(
                pair="USD/JPY",
                tv_ticker="USDJPY",
                direction="buy",
                entry_price=158.85,
                stop_loss=157.26,
                take_profit=162.03,
                position_size=10000.0,
                opened_at_ms=1712739600000,
            )
        ]
        script = generate_multi_signal_pine(signals=signals, positions=positions)
        # シグナル側
        assert "159" in script
        assert "158" in script
        assert "161" in script
        # ポジション側
        assert "158.85" in script
        assert "157.26" in script
        assert "162.03" in script
