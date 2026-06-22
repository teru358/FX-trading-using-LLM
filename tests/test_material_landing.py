from datetime import datetime, timezone, timedelta

from src.orchestrator.material_landing import MaterialLandingDetector


class _FakeTechSnap:
    """実 _TechnicalSnapshot の field 名に合わせる (direction_bias / collect_status)。"""
    def __init__(self, direction_bias, bias_score, collect_status="ok"):
        self.direction_bias = direction_bias
        self.bias_score = bias_score
        self.collect_status = collect_status


def _t(s):  # 秒オフセットのヘルパ
    return datetime(2026, 6, 20, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=s)


# ── technical material (Codex High#1: 実 field 名で検証) ─────────────

def test_direction_change_is_material():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    # 初回は基準が無いので material（初観測）
    assert det.technical_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")
    # 同じ → not material
    assert det.technical_material("USDJPY=X") is False
    # direction_bias 反転 → material
    snaps["USDJPY=X"] = _FakeTechSnap("short", 0.3)
    assert det.technical_material("USDJPY=X") is True


def test_bias_delta_below_threshold_not_material():
    snaps = {"EURUSD=X": _FakeTechSnap("long", 0.30)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    det.technical_material("EURUSD=X"); det.commit_seen("EURUSD=X")
    snaps["EURUSD=X"] = _FakeTechSnap("long", 0.40)  # delta 0.10 < 0.20
    assert det.technical_material("EURUSD=X") is False


def test_stale_to_ok_recovery_is_material():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3, collect_status="stale")}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
    )
    det.technical_material("USDJPY=X"); det.commit_seen("USDJPY=X")
    snaps["USDJPY=X"] = _FakeTechSnap("long", 0.3, collect_status="ok")  # stale→ok
    assert det.technical_material("USDJPY=X") is True


# ── news / event material (Codex High#2: consumed-key で再発火しない) ──

def test_news_impact_above_min_is_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.7,        # pair 関連通貨の impact
        material_news_impact_min=0.5,
        get_news_key=lambda pair: "news-1",
    )
    assert det.news_material("USDJPY=X") is True


def test_news_impact_below_min_not_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.3,
        material_news_impact_min=0.5,
        get_news_key=lambda pair: "news-1",
    )
    assert det.news_material("USDJPY=X") is False


def test_same_news_not_material_after_consume():
    """同じ高 impact news (同 key) は consume 後 material でなくなる。"""
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.7,
        material_news_impact_min=0.5,
        get_news_key=lambda pair: "news-1",  # 同じ key を返し続ける
    )
    assert det.news_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")            # この news を消費
    assert det.news_material("USDJPY=X") is False  # 同 key なので再発火しない


def test_new_news_key_is_material_again():
    """新しい news (別 key) なら再び material。"""
    state = {"key": "news-1"}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        get_news_impact=lambda pair: 0.7,
        material_news_impact_min=0.5,
        get_news_key=lambda pair: state["key"],
    )
    assert det.news_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")
    assert det.news_material("USDJPY=X") is False
    state["key"] = "news-2"                # 新しい news が来た
    assert det.news_material("USDJPY=X") is True


def test_in_high_importance_event_window_is_material():
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        in_event_window=lambda pair: True,
        get_event_key=lambda pair: "evt-1",
    )
    assert det.event_window_material("USDJPY=X") is True


def test_same_event_window_not_material_after_consume():
    """同じ event window (同 key) は consume 後 material でなくなる。"""
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,
        material_bias_delta_min=0.20,
        in_event_window=lambda pair: True,
        get_event_key=lambda pair: "evt-1",
    )
    assert det.event_window_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")
    assert det.event_window_material("USDJPY=X") is False


# ── debounce + periodic floor ───────────────────────────────────────

def test_debounce_coalesces_rapid_landings():
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["USDJPY=X"],
    )
    # t=0: material（初観測）→ debounce 窓開始、まだ起動しない
    assert det.pairs_to_plan(_t(0)) == []
    # t=60: 窓内 → まだ
    assert det.pairs_to_plan(_t(60)) == []
    # t=200: 窓（180s）を抜けた → 起動
    assert det.pairs_to_plan(_t(200)) == ["USDJPY=X"]


def test_periodic_floor_fires_without_material():
    snaps = {"EURUSD=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["EURUSD=X"],
    )
    det.pairs_to_plan(_t(0)); det.mark_committed("EURUSD=X", _t(0))
    # material 無し・floor 未超過 → 起動しない
    snaps["EURUSD=X"] = _FakeTechSnap("long", 0.3)  # 変化なし
    assert det.pairs_to_plan(_t(900)) == []
    # floor（1800s）超過 → 起動
    assert det.pairs_to_plan(_t(1801)) == ["EURUSD=X"]


def test_floor_bootstraps_first_planning_for_idle_pair():
    """Codex Medium#3: technical/news/event が無い idle pair も初回は floor で起動する。"""
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: None,   # material 一切なし
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["GBPUSD=X"],
    )
    # last_planned が無い (None) pair も初回は due 扱いで起動する
    assert det.pairs_to_plan(_t(0)) == ["GBPUSD=X"]
    det.mark_committed("GBPUSD=X", _t(0))
    # 直後は floor 未超過 → 起動しない
    assert det.pairs_to_plan(_t(900)) == []
    # floor 超過 → 再起動
    assert det.pairs_to_plan(_t(1801)) == ["GBPUSD=X"]


# ── attempted vs committed (Codex Medium#4) ─────────────────────────

def test_mark_attempted_does_not_consume_technical_baseline():
    """planning が snapshot 前に失敗したら baseline を消費しない (次回も material)。"""
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["USDJPY=X"],
    )
    # material が debounce を抜けて起動対象になる
    det.pairs_to_plan(_t(0))
    assert det.pairs_to_plan(_t(200)) == ["USDJPY=X"]
    # planning が失敗 → attempted (debounce 窓は閉じるが baseline は未消費)
    det.mark_attempted("USDJPY=X", _t(200))
    # 失敗直後は debounce 窓リセットで即再発火しない (新たな窓を開く)
    assert det.pairs_to_plan(_t(201)) == []   # t=201 に窓を開き直す
    # baseline 未消費なので、新たな debounce 窓を抜ければ再び material として起動できる
    assert det.pairs_to_plan(_t(300)) == []   # 窓内 (300-201=99 < 180)
    assert det.pairs_to_plan(_t(390)) == ["USDJPY=X"]  # 窓を抜けて再起動 (390-201=189 ≥ 180)


def test_mark_committed_consumes_technical_baseline():
    """planning が成功したら baseline を消費する (同じ material では再発火しない)。"""
    snaps = {"USDJPY=X": _FakeTechSnap("long", 0.3)}
    det = MaterialLandingDetector(
        get_latest_technical=lambda pair: snaps.get(pair),
        material_bias_delta_min=0.20,
        debounce_window_seconds=180,
        min_planning_interval_seconds=1800,
        pairs=["USDJPY=X"],
    )
    det.pairs_to_plan(_t(0))
    assert det.pairs_to_plan(_t(200)) == ["USDJPY=X"]
    det.mark_committed("USDJPY=X", _t(200))   # 成功 → baseline 消費
    # 同じ technical のまま → material でない (floor まで起動しない)
    assert det.pairs_to_plan(_t(400)) == []


# ── regime push 経路 (§5.4① / Task C-3) ──────────────────────

def _regime_detector(pairs=("USDJPY=X",), **kw):
    return MaterialLandingDetector(
        get_latest_technical=lambda p: None,  # technical 無し (regime 経路を単離)
        material_bias_delta_min=0.2,
        pairs=list(pairs),
        **kw,
    )


def test_regime_active_is_material():
    det = _regime_detector()
    det.mark_regime("USDJPY=X", "active")
    assert det.regime_material("USDJPY=X") is True
    assert det.is_material("USDJPY=X") is True


def test_regime_calm_normal_not_material():
    det = _regime_detector()
    det.mark_regime("USDJPY=X", "normal")
    assert det.regime_material("USDJPY=X") is False
    det.mark_regime("USDJPY=X", "calm")
    assert det.regime_material("USDJPY=X") is False


def test_regime_consumed_then_not_material_until_higher():
    det = _regime_detector()
    det.mark_regime("USDJPY=X", "active")
    assert det.regime_material("USDJPY=X") is True
    # planning が消費 (commit) すると同じ active では再発火しない。
    det.commit_seen("USDJPY=X")
    assert det.regime_material("USDJPY=X") is False
    # critical へさらに上がったら再び material (1 上昇 = 1 回)。
    det.mark_regime("USDJPY=X", "critical")
    assert det.regime_material("USDJPY=X") is True
    det.commit_seen("USDJPY=X")
    assert det.regime_material("USDJPY=X") is False


def test_regime_downgrade_then_reupgrade_is_material():
    """出戻り上昇 (active→normal→active) は新たな上昇イベントなので再発火する (review High)。"""
    det = _regime_detector()
    det.mark_regime("USDJPY=X", "active")  # seq 1
    det.commit_seen("USDJPY=X")            # consumed seq=1
    assert det.regime_material("USDJPY=X") is False
    det.mark_regime("USDJPY=X", "normal")  # 下降 = seq 据え置き (1)
    assert det.regime_material("USDJPY=X") is False  # 上昇していないので material でない
    # normal → active は新たな上昇イベント (seq 2) → 再 material (§5.4① の意図)。
    det.mark_regime("USDJPY=X", "active")  # seq 2
    assert det.regime_material("USDJPY=X") is True


def test_regime_active_held_does_not_refire():
    """active を維持したまま再 push しても (上昇でないので) 再発火しない (連発防止)。"""
    det = _regime_detector()
    det.mark_regime("USDJPY=X", "active")  # seq 1
    det.commit_seen("USDJPY=X")            # consumed 1
    det.mark_regime("USDJPY=X", "active")  # 同 rank = 上昇でない → seq 1 のまま
    assert det.regime_material("USDJPY=X") is False
    det.mark_regime("USDJPY=X", "critical")  # active→critical = 上昇 (seq 2)
    assert det.regime_material("USDJPY=X") is True


def test_regime_drives_pairs_to_plan_after_debounce():
    det = _regime_detector(debounce_window_seconds=180)
    t0 = datetime(2026, 6, 22, 12, 0, 0, tzinfo=timezone.utc)
    det.mark_regime("USDJPY=X", "active")
    assert det.pairs_to_plan(t0) == []  # debounce 窓開始
    # 窓を抜けたら planning 対象に入る。
    assert det.pairs_to_plan(t0 + timedelta(seconds=181)) == ["USDJPY=X"]
