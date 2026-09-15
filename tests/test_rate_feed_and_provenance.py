"""Verrous hors-ligne — mission 15/09/2026.

P0-3 : voie « consensus Forex Factory » (macro_engine [N1]–[N5]) ;
P0-2 : provenance réelle des taux CB (external_sources fetch_central_bank_rates_with_meta) ;
P1   : bornes dures de parsing (_parse_feed_rate / _parse_rate_pct) ;
secrets : relecture FRED à chaque appel + chargement .env idempotent ;
P0-1 : fingerprint de module (version + chemin + sha256).

Aucun appel réseau : TOUTE sortie réseau est remplacée par monkeypatch — ces
tests sont déterministes et peuvent tourner en CI sans clé ni connectivité.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bluestar import external_sources as es
from bluestar import fingerprint as fp
from bluestar import macro_engine as me
from bluestar.models import Reliability

NOW_UTC = datetime(2026, 9, 14, 22, 53, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _no_fred_network(monkeypatch):
    """Filet de sécurité CI : AUCUN test de ce module ne sort sur le réseau
    FRED par accident. Les tests qui veulent un faux FRED re-patchent
    eux-mêmes _fred_series_dated (monkeypatch est le même objet par test)."""
    monkeypatch.setattr(es, "_fred_series_dated", lambda series_id: None)


# ---------------------------------------------------------------------------
# fabrique d'événements façon flux Forex Factory (champs lus par getattr)
# ---------------------------------------------------------------------------
def _ev(ccy: str, title: str, hours_until: float, previous: str, forecast: str,
        iso_date: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        currency=ccy, event_name=title, hours_until=hours_until,
        previous=previous, forecast=forecast,
        datetime_utc=iso_date or None, date_display="", date="",
        is_upcoming=hours_until >= 0,
    )


def _week_events() -> list[SimpleNamespace]:
    """Semaine du 14/09/2026, forme réelle du flux (vérifiée 200 ce jour)."""
    return [
        _ev("USD", "Federal Funds Rate", 63.0, "3.75%", "4.00%",
            "2026-09-16"),
        _ev("GBP", "MPC Official Bank Rate Votes", 10.0, "3-0-6", "3-0-6",
            "2026-09-17"),
        _ev("GBP", "Official Bank Rate", 68.0, "3.75%", "3.75%",
            "2026-09-17"),
        _ev("JPY", "BOJ Policy Rate", 96.5, "<1.00%", "<1.25%",
            "2026-09-18"),
    ]


# ===========================================================================
# P1 — bornes dures de parsing (exigées par le brief)
# ===========================================================================
@pytest.mark.parametrize("raw", ["3-0-6", "480B", "120K", "", "—", "-", None])
def test_parse_feed_rate_rejette_non_taux(raw):
    """Sans « % », TOUT est rejeté : votes MPC « 3-0-6 », loans « 480B »."""
    assert me._parse_feed_rate(raw) is None


def test_parse_feed_rate_valeurs_valides():
    p = me._parse_feed_rate("3.75%")
    assert p is not None and p.value == 3.75 and not p.is_bounded
    b = me._parse_feed_rate("<1.00%")
    assert b is not None and b.value == 1.00 and b.is_bounded
    assert b.display == "<1,00%"
    v = me._parse_feed_rate("<1.25%")
    assert v is not None and v.value == 1.25 and v.is_bounded
    fr = me._parse_feed_rate("3,75 %")
    assert fr is not None and fr.value == 3.75


@pytest.mark.parametrize("display,expected", [
    ("-0,10%", -0.10),          # signe négatif préservé (régression passée)
    ("<1,00%", 1.00),           # borne tolérée, plus de ValueError
    ("3,50–3,75%", 3.625),      # fourchette → point milieu
    ("~2,50%", 2.50),
    ("[N/A]", None),
    ("", None),
])
def test_parse_rate_pct_parity_avant(display, expected):
    got = me._parse_rate_pct(display)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# ===========================================================================
# P0-3 — biais consensus : écart en pb sur les VALEURS PARSÉES
# ===========================================================================
def test_bias_consensus_statu_quo_ne_declenche_jamais():
    """BoE 3,75/3,75 : écart == 0 → None → mot d'ouverture inchangé."""
    prev = me._parse_feed_rate("3.75%")
    fcst = me._parse_feed_rate("3.75%")
    assert me._derive_bias_from_consensus(prev, fcst, _ev("GBP", "Official Bank Rate", 1, "", "")) is None


def test_bias_consensus_boj_hausse_25pb_born():
    prev = me._parse_feed_rate("<1.00%")
    fcst = me._parse_feed_rate("<1.25%")
    bias = me._derive_bias_from_consensus(prev, fcst, _ev("JPY", "BOJ Policy Rate", 96.5, "", "", "2026-09-18"))
    assert bias is not None
    assert bias.startswith("Hawkish")
    assert "hausse de 25 pb attendue le 18/09/2026" in bias
    assert "<1,25% contre <1,00% en vigueur" in bias
    assert "comparaison de bornes publiées" in bias
    assert "[consensus Forex Factory, pas un communiqué officiel]" in bias


def test_bias_consensus_calcul_sur_valeurs_parees_non_sur_texte():
    """Écart = (forecast.value − previous.value) × 100 en pb."""
    prev = me._parse_feed_rate("3.75%")
    fcst = me._parse_feed_rate("3.50%")
    bias = me._derive_bias_from_consensus(prev, fcst, _ev("USD", "Federal Funds Rate", 1, "", "", "2026-09-16"))
    assert bias.startswith("Dovish")
    assert "baisse de 25 pb attendue le 16/09/2026" in bias
    assert "comparaison de bornes publiées" not in bias  # valeurs non bornées


def test_rate_event_for_bank_exclut_votes_et_passes():
    evs = _week_events()
    gbp = me._rate_event_for_bank(evs, "BoE")
    # « Votes » (10 h) EST plus proche mais doit être exclu ; la décision
    # (68 h) est retenue.
    assert gbp is not None and gbp.event_name == "Official Bank Rate"
    boj = me._rate_event_for_bank(evs, "BoJ")
    assert boj is not None and boj.event_name == "BOJ Policy Rate"
    assert me._rate_event_for_bank(None, "BoJ") is None
    assert me._rate_event_for_bank(evs, "BCE") is None  # aucune décision EUR


# ===========================================================================
# P0-3 / P0-2 — intégration build_central_bank_context (zéro réseau)
# ===========================================================================
@pytest.fixture()
def stubbed_engine(monkeypatch):
    """Neutralise les deux seules sources live de build_central_bank_context."""
    def _apply(meta):
        monkeypatch.setattr(me, "fetch_central_bank_rates_with_meta", lambda: dict(meta))
        monkeypatch.setattr(me, "fetch_fedwatch_probabilities", lambda: None)
    return _apply


def _by_name(cards):
    return {c.name: c for c in cards}


def test_feed_stamp_est_fallback_jamais_primary(stubbed_engine):
    stubbed_engine({})
    cards = _by_name(me.build_central_bank_context({}, NOW_UTC, events=_week_events()))
    for name in ("FED", "BoJ", "BoE"):
        assert cards[name].stamp.reliability is Reliability.FALLBACK, name
        assert cards[name].stamp.source_name.startswith("Forex Factory · "), name
    # BCE : ni live ni feed → N/A honnête, JAMAIS une valeur inventée.
    assert cards["BCE"].stamp.reliability is Reliability.UNAVAILABLE
    assert cards["BCE"].rate_display == "[N/A]"


def test_boj_flux_prime_sur_live_et_biais_hawkish_25pb(stubbed_engine):
    # La série FRED BoJ répond (0,20 %) : le flux DOIT passer devant [N1].
    stubbed_engine({"BoJ": (0.20, "FRED")})
    cards = _by_name(me.build_central_bank_context({}, NOW_UTC, events=_week_events()))
    boj = cards["BoJ"]
    assert boj.rate_display == "<1,00%"          # borne affichée, jamais « 1,00% » nu
    assert boj.stamp.reliability is Reliability.FALLBACK
    assert "valeur bornée telle que publiée" in (boj.stamp.note or "")
    bias = boj.bias_interpretation
    assert bias.startswith("Hawkish — hausse de 25 pb attendue le 18/09/2026")
    assert "Lecture de niveau :" in bias          # niveau conservé, jamais perdu
    # « 18/09 » est cité dans le BIAIS (consensus daté), et « 25 pb » aussi.
    assert "25 pb" in bias and "18/09/2026" in bias


def test_boe_statu_quo_mot_ouverture_inchange_parcours_band(stubbed_engine):
    stubbed_engine({})
    cards = _by_name(me.build_central_bank_context({}, NOW_UTC, events=_week_events()))
    boe = cards["BoE"]
    bias = boe.bias_interpretation
    # Consensus == (3,75/3,75) → PAS de « Hausse/baisse de X pb » :
    assert "pb attendue" not in bias
    # dérive par fourchette (band (2.50, 3.50) → au-dessus → Hawkish) + note
    # textuelle de statu quo [N5].
    assert bias.startswith("Hawkish — taux directeur (3,75%)")
    assert "Consensus Forex Factory : statu quo attendu le 17/09/2026" in bias


def test_live_stamp_consomme_provenance_reelle_meta(stubbed_engine):
    """P0-2 : le stamp « live » reprend le libellé MÉTA (source réelle)."""
    stubbed_engine({"FED": (3.75, "FRED"),
                    "BCE": (2.25, "ECB Data Portal · DFR"),
                    "BoE": (3.75, "Bank of England · Bank-Rate.asp")})
    cards = _by_name(me.build_central_bank_context({}, NOW_UTC, events=[]))
    assert cards["BCE"].rate_display == "2,25%"
    assert cards["BCE"].stamp.reliability is Reliability.PRIMARY
    assert cards["BCE"].stamp.source_name == "ECB Data Portal · DFR"
    assert cards["BoE"].stamp.source_name == "Bank of England · Bank-Rate.asp"
    assert cards["FED"].stamp.source_name == "FRED"


def test_pas_de_troisieme_chemin_de_rate_kind(stubbed_engine):
    """Toute valeur affichée vient de live / feed / override / [N/A] — et
    l'entrée dans le différentiel est gated par le stamp, pas par un chemin
    parallèle (règle P0-3 du brief)."""
    stubbed_engine({"FED": (3.75, "FRED")})
    cards = me.build_central_bank_context(
        {"central_banks": {"BoJ": {"rate": "0,75%"}}},  # override BoJ
        NOW_UTC, events=_week_events())
    by = _by_name(cards)
    assert by["BoJ"].rate_display == "<1,00%"     # feed bat override (feed d'abord)
    assert by["BoJ"].stamp.reliability is Reliability.FALLBACK
    dominant, implication = me._build_rate_differential(list(cards))
    # FED live (PRIMARY) vs BoJ feed (FALLBACK) → tag MIXTE documenté, pas un
    # troisième régime.
    assert "MIXTE" in dominant, dominant
    assert "<1,00%" in dominant  # la borne reste bornée à l'écran


def test_diff_differentiel_two_live_primary_pas_mixed(stubbed_engine):
    stubbed_engine({"FED": (3.75, "FRED"), "BCE": (2.25, "ECB Data Portal · DFR")})
    cards = me.build_central_bank_context({}, NOW_UTC, events=[])
    dominant, _impl = me._build_rate_differential(list(cards))
    assert "PRIMARY" in dominant and "MIXTE" not in dominant
    assert "ECB Data Portal · DFR" in dominant
    assert "EUR (2,25%)" in dominant and "USD (3,75%)" in dominant


# ===========================================================================
# P0-2 — external_sources : ordre BoE inversé + meta + wrapper
# ===========================================================================
def test_boe_scrape_premier_iadb_repli(monkeypatch):
    calls = []

    def fake_scrape():
        calls.append("scrape")
        return (3.75, "2025-12-18")

    def fake_iadb():
        calls.append("iadb")
        return (3.75, "2026-09-11")

    monkeypatch.setattr(es, "_boe_bank_rate_scrape", fake_scrape)
    monkeypatch.setattr(es, "_boe_bank_rate", fake_iadb)
    out = es.fetch_central_bank_rates_with_meta()
    assert calls == ["scrape"], "le scrape Bank-Rate.asp doit être tenté EN PREMIER"
    assert out["BoE"] == (3.75, "Bank of England · Bank-Rate.asp")


def test_boe_repli_iadb_label_honnete(monkeypatch):
    calls = []
    monkeypatch.setattr(es, "_boe_bank_rate_scrape", lambda: calls.append("scrape") or None)
    monkeypatch.setattr(es, "_boe_bank_rate", lambda: calls.append("iadb") or (3.75, "2026-09-11"))
    out = es.fetch_central_bank_rates_with_meta()
    assert calls == ["scrape", "iadb"]
    assert out["BoE"] == (3.75, "Bank of England · IADB"), \
        "IADB n'est revendiqué QUE s'il a réellement servi"


def test_boe_les_deux_echouent_absent_de_meta(monkeypatch):
    monkeypatch.setattr(es, "_boe_bank_rate_scrape", lambda: None)
    monkeypatch.setattr(es, "_boe_bank_rate", lambda: None)
    out = es.fetch_central_bank_rates_with_meta()
    assert "BoE" not in out


def test_boe_valeur_hors_bornes_ecartee(monkeypatch):
    monkeypatch.setattr(es, "_boe_bank_rate_scrape", lambda: (99.0, "2026-09-01"))
    monkeypatch.setattr(es, "_boe_bank_rate", lambda: (3.75, "2026-09-11"))
    out = es.fetch_central_bank_rates_with_meta()
    # le scrape a RÉPONDU mais sa valeur est rejetée : pas de bascule
    # silencieuse vers IADB (comportement symétrique à l'ancien code) → absent.
    assert "BoE" not in out


def test_meta_labels_bce_et_wrappers(monkeypatch):
    today = datetime.now(timezone.utc).date().isoformat()

    def fake_dated(series_id):
        return {"DFEDTARU": (3.75, today),
                "ECBDFR": (2.25, today),
                "IRSTCI01JPM156N": (0.20, today)}[series_id]

    monkeypatch.setattr(es, "_fred_series_dated", fake_dated)
    monkeypatch.setattr(es, "_boe_bank_rate_scrape", lambda: (3.75, today))
    meta = es.fetch_central_bank_rates_with_meta()
    assert meta["BCE"] == (2.25, "ECB Data Portal · DFR")
    assert meta["FED"] == (3.75, "FRED")
    # Wrapper historique : contrat dict[str, float] strict + valeurs identiques.
    flat = es.fetch_central_bank_rates()
    assert set(flat) == set(meta)
    assert all(isinstance(v, float) for v in flat.values())
    assert flat == {k: v for k, (v, _s) in meta.items()}


def test_central_bank_rate_source_compat_label():
    """Fonction historique conservée (signature str->str), étiquette primaire."""
    assert es.central_bank_rate_source("BoE") == "Bank of England · Bank-Rate.asp"
    assert es.central_bank_rate_source("BCE") == "ECB Data Portal · DFR"
    assert es.central_bank_rate_source("FED") == "FRED"


# ===========================================================================
# secrets — relecture FRED à chaque appel + .env (idempotence, non-override)
# ===========================================================================
def test_fred_key_relue_a_chaque_appel(monkeypatch):
    """Bug du 23/07 : un cache module-level gelait la clé. Interdit.

    On capture les params des GET successifs : 1re appel avec clé → passe ;
    2e appel après suppression de la clé → AUCUNE requête (relecture réelle).
    """
    seen = []

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"observations": [{"date": "2026-09-14", "value": "3.75"}]}

    def fake_get(url, extra_headers=None, **kw):
        seen.append(kw.get("params", {}))
        return FakeResp()

    monkeypatch.setattr(es, "_get", fake_get)
    monkeypatch.setenv("FRED_API_KEY", "CLE-TEST")
    assert es._fred_series("DFEDTARU") == 3.75
    assert seen and seen[-1]["api_key"] == "CLE-TEST"
    monkeypatch.delenv("FRED_API_KEY")
    assert es._fred_series("DFEDTARU") is None
    assert len(seen) == 1, "la clé doit être relue À CHAQUE appel, pas cachée"


def test_dotenv_charge_non_override_et_idempotent(tmp_path, monkeypatch):
    envf = tmp_path / "test.env"
    envf.write_text("BLUESTAR_TEST_SECRET = \"from-dotenv\"\n", encoding="utf-8")
    monkeypatch.setenv("BLUESTAR_DOTENV_PATH", str(envf))
    monkeypatch.delenv("_BLUESTAR_DOTENV_PATH", raising=False)
    monkeypatch.delenv("BLUESTAR_TEST_SECRET", raising=False)
    es._ensure_dotenv_loaded()
    assert os.environ.get("BLUESTAR_TEST_SECRET") == "from-dotenv"
    # Idempotence : marqueur posé → second appel ne relit rien ; une valeur
    # déjà présente dans l'environnement n'est JAMAIS écrasée (override=False).
    monkeypatch.setenv("BLUESTAR_TEST_SECRET", "prioritaire")
    monkeypatch.setenv("_BLUESTAR_DOTENV_PATH", str(envf))
    es._ensure_dotenv_loaded()
    assert os.environ.get("BLUESTAR_TEST_SECRET") == "prioritaire"
    monkeypatch.delenv("_BLUESTAR_DOTENV_PATH")
    es._ensure_dotenv_loaded()
    assert os.environ.get("BLUESTAR_TEST_SECRET") == "prioritaire", \
        "override=False : l'environnement système garde la main sur le .env"


# ===========================================================================
# P0-1 — fingerprint : version + chemin + sha (démasque toute copie fantôme)
# ===========================================================================
def test_fingerprint_external_sources_complet():
    rec = fp.module_fingerprint("external_sources")
    assert rec["error"] is None
    assert rec["file"].endswith("external_sources.py")
    assert rec["version"] == es.__version__
    assert len(rec["sha256"]) == 12
    assert rec["size"] > 0


def test_fingerprint_sha_distinctif_entre_copies(tmp_path):
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    a.write_bytes(b"v1")
    b.write_bytes(b"v2")
    sa, sb = fp.file_sha256(str(a)), fp.file_sha256(str(b))
    assert sa != sb, "deux copies divergentes ne peuvent pas partager un fingerprint"


def test_log_boot_fingerprints_14_modules():
    recs = fp.log_boot_fingerprints()
    assert len(recs) == len(fp.CORE_MODULES)
    assert all(r["error"] is None for r in recs), [r for r in recs if r["error"]]
    assert "external_sources" in fp.summary_for_ui(recs)
