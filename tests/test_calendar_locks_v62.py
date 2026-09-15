"""Verrous v6.2 — sessions ANZ (B8). Zéro consommateur dans le briefing :
ces tests épinglent la véridicité de la donnée ET l'invariance du
content_hash à la table de sessions (le hash ne projette pas « session »).
"""
import datetime as dt

from bluestar import calendar_layer as cl
from bluestar.calendar_layer import Session

UTC = dt.timezone.utc


def _cs(y, mo, d, h, mi=0):
    return cl.classify_session(dt.datetime(y, mo, d, h, mi, tzinfo=UTC))


# ------------------------- mesure source de l'arbitrage B8 -----------------
def test_nzd_gdp_wednesday_2245z_is_asian_not_off():
    # Le cas live mesuré 15/09/2026 : « GDP q/q » NZD 2026-09-16T22:45Z
    # = 10:45 Wellington / 08:45 Sydney → avant : OFF, après : ASIAN.
    session, centers = _cs(2026, 9, 16, 22, 45)
    assert session is Session.ASIAN
    assert set(centers) >= {"WELLINGTON", "SYDNEY"}


def test_aud_release_sydney_daytime_is_asian():
    session, centers = _cs(2026, 9, 15, 3, 0)      # 13:00 Sydney (AEST)
    assert session is Session.ASIAN and "SYDNEY" in centers


# ------------------------------ non-régression ------------------------------
def test_tokyo_london_overlap_and_plain_sessions_unchanged():
    assert _cs(2026, 9, 15, 6, 30)[0] is Session.ASIAN            # Tokyo 15:30 JST
    assert _cs(2026, 9, 15, 8, 30)[0] is Session.LONDON           # Tokyo = 23:00Z-08:00Z, donc 08:30Z Tokyo fermé : LONDON pur
    assert _cs(2026, 9, 15, 13, 30)[0] is Session.OVERLAP_LONDON_NY
    assert _cs(2026, 9, 15, 7, 0)[0] is Session.OVERLAP_ASIA_LONDON  # Lon 08:00 + Tokyo 16:00


def test_anz_hours_now_asian_instead_of_off():
    # Conséquence assumée (aucun consommateur de session dans le briefing) :
    # avec 5 centres, la couverture est continue 24h en jours ouvrés — des
    # créneaux jadis « OFF » deviennent « ASIAN ». Épingle les cas mesurés :
    # mardi 22:30Z = mercredi 08:30 Sydney → ASIAN (avant v6.2 : OFF) ;
    # lundi 21:30Z = NY fermé (17:30 EDT), Wellington 09:30 / Sydney 07:30 → ASIAN.
    assert _cs(2026, 9, 15, 22, 30)[0] is Session.ASIAN
    assert _cs(2026, 9, 14, 21, 30)[0] is Session.ASIAN


def test_weekend_still_off():
    assert _cs(2026, 9, 19, 22, 45)[0] is Session.OFF   # samedi 19/09 → dimanche local : ignoré
    assert _cs(2026, 9, 20, 10, 0)[0] is Session.OFF    # dimanche


def test_get_session_legacy_api_untouched():
    # API de compat v4 (UTC-only) : volontairement NON touchée par B8.
    assert cl.get_session(dt.datetime(2026, 6, 28, 10, tzinfo=UTC)) == "LONDON"


# --------------------------- hash ⊥ sessions -------------------------------
def test_content_hash_invariant_to_session_table(monkeypatch):
    rows = [{"date": "2026-09-16T18:45:00-04:00", "country": "NZD",
             "title": "GDP q/q", "impact": "High",
             "forecast": "0.5%", "previous": "0.2%"}]
    now = dt.datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    cal_full = cl.build_calendar(now_utc=now, raw_data=list(rows))
    monkeypatch.setattr(cl, "SESSION_HOURS", {
        "LONDON": cl.SESSION_HOURS["LONDON"],
        "NEW_YORK": cl.SESSION_HOURS["NEW_YORK"],
        "TOKYO": cl.SESSION_HOURS["TOKYO"],
    })
    cal_3 = cl.build_calendar(now_utc=now, raw_data=list(rows))
    assert cal_full["metadata"]["content_hash"] == cal_3["metadata"]["content_hash"]
    # …et la donnée, elle, devient vraie :
    assert cal_full["events_engine"][0]["session_v2"] == "ASIAN"
    assert cal_3["events_engine"][0]["session_v2"] == "OFF"


def test_session_policy_version_bumped():
    assert cl.SESSION_POLICY_VERSION.endswith("_v2")
