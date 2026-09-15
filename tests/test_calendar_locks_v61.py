"""Verrous v6.1 — correctifs post-audit externe (calendar_layer).

Hors réseau : toute donnée FF est synthétique ou injectée via raw_data /
monkeypatch de fetch_raw. Chaque test nomme la mesure qui l'a motivé
(« audit Opus » 15/09/2026, cf. en-tête de calendar_layer.py [M1]-[M5]).
"""
import datetime as dt

import pytest

from bluestar import calendar_layer as cl
from bluestar import models

UTC = dt.timezone.utc
BASE = dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=UTC)


def _row(date_iso, ccy, title, impact="High", forecast=None, previous=None):
    r = {"date": date_iso, "country": ccy, "title": title, "impact": impact}
    if forecast is not None:
        r["forecast"] = forecast
    if previous is not None:
        r["previous"] = previous
    return r


def _week_rows(end_hours=96.0):
    """Micro-semaine thisweek-only se terminant à ~+end_hours h (format ET -04:00)."""
    end_utc = BASE + dt.timedelta(hours=end_hours)
    end_local = end_utc.astimezone(dt.timezone(dt.timedelta(hours=-4)))
    return [
        _row("2026-09-15T08:30:00-04:00", "GBP", "Claimant Count Change",
             forecast="118.0K", previous="120.4K"),
        _row("2026-09-15T08:30:00-04:00", "USD", "Export Price Index m/m",
             forecast="0.2%", previous="0.4%"),
        _row(end_local.strftime("%Y-%m-%dT%H:%M:00") + "-04:00", "EUR",
             "ECB Main Refinancing Rate", forecast="2.15%", previous="2.15%"),
    ]


def _build(rows, now=BASE):
    return cl.build_calendar(now_utc=now, raw_data=list(rows))


# ---------------------------------------------------------------- [M1] replica
def test_refinancing_is_tier_s_replica_v10():
    # v10 l.251 contient "refinancing" ; l'absence ici classait la décision
    # BCE en NONE (mesuré 15/09/2026). Rétabli à l'identique.
    assert cl.classify_tier("Main Refinancing Rate") == "S"
    blocked, tier = cl.is_blackout("Main Refinancing Rate", -6.0)
    assert (blocked, tier) == (True, "S")
    assert cl.is_blackout("Main Refinancing Rate", -60.0)[0] is False


def test_high_none_promoted_to_A_label_only():
    # v10 CalendarEvent._derive l.335-336 : HIGH + NONE → A. Fenêtre A ==
    # défaut (2,24) : le VERDICT booléen est inchangé, seul le label suit.
    blocked, tier = cl.is_blackout("Claimant Count Change", -3.9)
    assert tier == "A" and blocked is True
    # bornes identiques à l'ancien comportement défaut :
    assert cl.is_blackout("Toute Chose Inconnue", 1.5)[0] is True
    assert cl.is_blackout("Toute Chose Inconnue", 2.5)[0] is False
    assert cl.is_blackout("Toute Chose Inconnue", -23.9)[0] is True
    assert cl.is_blackout("Toute Chose Inconnue", -24.1)[0] is False


def test_non_high_impact_not_promoted():
    # contrat v10 : pas de promotion si l'impact n'est pas HIGH.
    assert cl.is_blackout("Toute Chose Inconnue", 1.0, impact="medium")[1] == "NONE"
    assert cl.is_blackout("Claimant Count Change", 1.0)[1] == "A"  # défaut high


def test_tier_label_flows_into_build_calendar_rows():
    cal = _build(_week_rows())
    row = next(r for r in cal["events_engine"] if r["event_name"] == "Claimant Count Change")
    assert row["tier"] == "A"


# ------------------------------------------------------------- [M2] bandeau
def test_no_dead_nextweek_request_by_default(monkeypatch):
    # Pas de reload : la dérivation est une fonction pure + l'état d'import
    # courant de SOURCE_URLS doit rester cohérent avec l'absence d'override.
    monkeypatch.delenv("BLUESTAR_SOURCE_URL_NEXT", raising=False)
    assert cl._derive_nextweek_url("https://x/ff_calendar_thisweek.json") is None
    assert cl._derive_nextweek_url("https://x/ff_calendar_thisweek.xml") is None
    assert cl.SOURCE_URLS == (cl.SOURCE_URL,)  # un seul GET par cycle (live)


def test_second_feed_capability_preserved_via_env(monkeypatch):
    monkeypatch.setenv("BLUESTAR_SOURCE_URL_NEXT", "https://x/ff_calendar_nextweek.json")
    assert cl._derive_nextweek_url("https://x/ff_calendar_thisweek.json") == \
        "https://x/ff_calendar_nextweek.json"


def test_horizon_diagnosis_table():
    d = cl._horizon_diagnosis
    assert d(None, {}) == ("unreachable", False)
    assert d(96.0, {"thisweek": "ok"}) == ("nominal_weekly", False)
    assert d(96.0, {}) == ("nominal_weekly", False)          # raw injectée = confiance
    assert d(96.0, {"thisweek": "error:500", "nextweek": "ok"}) == ("degraded", True)
    assert d(170.0, {"thisweek": "ok"}) == ("full_watch_horizon", False)


def test_nominal_weekly_no_red_banner(monkeypatch, caplog):
    cal = _build(_week_rows(end_hours=96.0))
    md = cal["metadata"]
    assert md["feed_horizon_truncated"] is False
    assert md["feed_horizon_state"] == "nominal_weekly"
    assert md["feed_horizon_h"] == pytest.approx(96.0, abs=0.05)
    # mention neutre en INFO, jamais WARNING :
    assert not [r for r in caplog.records if r.levelname == "WARNING"
                and "FF" in r.getMessage()]


def test_degraded_feed_still_raises_red_flag(monkeypatch):
    rows = _week_rows()
    meta = {"feed_status": {"thisweek": "error:500", "nextweek": "ok"},
            "feeds_ok": 1, "feeds_total": 2, "urls": ("u1", "u2"),
            "fetched_at_utc": BASE, "payload_bytes": 10,
            "payload_sha256": "sha256:test"}
    monkeypatch.setattr(cl, "fetch_raw", lambda urls=None: (list(rows), dict(meta)))
    cal = cl.build_calendar(now_utc=BASE)
    assert cal["metadata"]["feed_horizon_truncated"] is True
    assert cal["metadata"]["feed_horizon_state"] == "degraded"


def test_unreachable_is_not_truncated():
    cal = cl.build_calendar(now_utc=BASE, raw_data=[])
    md = cal["metadata"]
    assert md["feed_horizon_truncated"] is False
    assert md["feed_horizon_state"] == "unreachable"
    assert md["reachable"] is False


# --------------------------------------------------------- [M3] parsing / doc
def test_num_re_qualifier_vocabulary_aligned_with_macro_parser():
    from bluestar import macro_engine as me
    for txt, val in [("≥2.5%", 2.5), ("≤3,75%", 3.75), ("±0.1%", 0.1),
                     ("<1.00%", 1.0)]:
        nv = cl.normalize_numeric(txt)
        pr = me._parse_feed_rate(txt)
        assert nv.parse_status == "APPROXIMATE", txt
        assert nv.value == pytest.approx(val), txt
        assert pr is not None and pr.value == pytest.approx(val), txt
    # ≈ reste propre à calendar_layer (le parseur macro exige un % : c'est un
    # parseur de TAUX, pas un normaliseur général) — parité non attendue ici.
    nv = cl.normalize_numeric("≈2.5")
    assert (nv.value, nv.parse_status) == (2.5, "APPROXIMATE")
    # écart résiduel DOCUMENTÉ (en-tête [M3]) : la plage reste non dévinée ici
    assert cl.normalize_numeric("3,50–3,75%").parse_status == "UNPARSEABLE"


def test_forecast_previous_status_exported_and_tolerated():
    cal = _build(_week_rows())
    for r in cal["events_engine"]:
        assert r["forecast_status"] in {"PARSED", "APPROXIMATE", "COMPOSITE",
                                        "ABSENT", "UNPARSEABLE", "PLACEHOLDER"}
        assert r["previous_status"] in {"PARSED", "APPROXIMATE", "COMPOSITE",
                                        "ABSENT", "UNPARSEABLE", "PLACEHOLDER"}
        models.MacroEvent.from_enriched(r)  # additif toléré par le contrat


def test_view_hash_alias_and_lists_decoupled():
    payload = cl.build_payload(_week_rows(), source=cl.SourceInfo(
        provider="t", url="u", urls=("u",), feeds_ok=1, feeds_total=1,
        fetched_at_utc=BASE, payload_sha256="sha256:t", supports_actual=False,
        from_last_known_good=False, feed_status={}),
        now_utc=BASE, policy=cl.DEFAULT_POLICY)
    legacy = cl.to_legacy_payload(payload, BASE)
    assert legacy["metadata"]["view_hash"] == legacy["metadata"]["content_hash"]
    assert legacy["events"] is not legacy["events_engine"]
    assert len(legacy["events"]) == len(legacy["events_engine"])


# ------------------------------------------------------------ [M4] presse BoJ
def test_presser_anchor_windows():
    rows = [
        {"currency": "JPY", "name": "BOJ Policy Rate",
         "scheduled_at_utc": dt.datetime(2026, 9, 17, 2, 30, tzinfo=UTC)},
        {"currency": "JPY", "name": "BOJ Press Conference",
         "scheduled_at_utc": dt.datetime(2026, 9, 17, 5, 30, tzinfo=UTC)},
        {"currency": "EUR", "name": "ECB Interest Rate Decision",
         "scheduled_at_utc": dt.datetime(2026, 9, 17, 12, 0, tzinfo=UTC)},
        {"currency": "EUR", "name": "ECB Press Conference",
         "scheduled_at_utc": dt.datetime(2026, 9, 17, 15, 0, tzinfo=UTC)},
        {"currency": "USD", "name": "FOMC Statement",
         "scheduled_at_utc": dt.datetime(2026, 9, 16, 18, 0, tzinfo=UTC)},
        {"currency": "USD", "name": "FOMC Press Conference",
         "scheduled_at_utc": dt.datetime(2026, 9, 16, 18, 30, tzinfo=UTC)},
    ]
    cl.assign_release_groups(rows)
    g = {r["name"]: r["release_group_id"] for r in rows}
    assert g["BOJ Press Conference"] == g["BOJ Policy Rate"] is not None   # 3,0 h ≤ 240 min
    assert g["FOMC Press Conference"] == g["FOMC Statement"] is not None   # 30 min
    assert g["ECB Press Conference"] is None                              # 180 min > 120 (défaut)
