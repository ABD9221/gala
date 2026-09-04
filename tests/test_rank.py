"""The prominence model, and its SQL/Python parity."""
import pytest

from gala import rank
from gala.rank import NullRatingsProvider, Signals, score


def test_score_is_bounded():
    assert score(Signals()) == 0.0
    full = Signals(confidence=1, source_count=99, has_phone=True, has_website=True,
                   has_address=True, osm_importance=1, wikidata_sitelinks=999, has_brand=True)
    assert score(full) == pytest.approx(1.0, abs=1e-9)


def test_more_evidence_scores_higher():
    stub = Signals(confidence=0.3)
    shop = Signals(confidence=0.8, source_count=2, has_phone=True, has_website=True, has_address=True)
    landmark = Signals(confidence=0.95, source_count=4, has_phone=True, has_website=True,
                       has_address=True, osm_importance=0.48, wikidata_sitelinks=42, has_brand=True)
    assert score(stub) < score(shop) < score(landmark)


def test_sitelinks_saturate():
    """Beyond the saturation point extra Wikipedia editions add ~nothing."""
    a = score(Signals(wikidata_sitelinks=40))
    b = score(Signals(wikidata_sitelinks=400))
    assert b - a < 0.03


def test_sql_matches_python_reference(con):
    """The SQL fast path and the readable implementation must not drift."""
    cur = con.execute("SELECT * FROM places")
    cols = [d[0] for d in cur.description]
    for row in (dict(zip(cols, r)) for r in cur.fetchall()):
        assert score(rank.signals_from_row(row)) == pytest.approx(row["prominence"], abs=1e-9)


def test_empty_strings_count_as_missing(con):
    """'' and NULL must mean the same thing, or coverage and scoring diverge."""
    con.execute("UPDATE places SET phone = '', website = NULL WHERE id = 'p0'")
    rank.recompute(con)
    blank = con.execute("SELECT prominence FROM places WHERE id = 'p0'").fetchone()[0]
    con.execute("UPDATE places SET phone = NULL WHERE id = 'p0'")
    rank.recompute(con)
    assert blank == pytest.approx(con.execute("SELECT prominence FROM places WHERE id = 'p0'").fetchone()[0])


def test_null_provider_never_invents_a_rating():
    assert NullRatingsProvider().fetch("p0", "x", 0.0, 0.0) is None
