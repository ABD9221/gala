"""Lead scoring: the ranking that has to be the opposite of prominence."""
import pytest

from gala.leads import (
    category_fit, chain_names, classify_website, find_leads, is_chain, score_lead,
)


@pytest.mark.parametrize("url,expected", [
    ("https://joebarrel.com/", "real site"),
    ("https://mysalon.com.sa", "real site"),
    # Slash-prefixed domains: the bug that made link aggregators read as real
    # sites when the anchor bound to only the first alternative.
    ("https://linktr.ee/y", "link aggregator"),
    ("https://taplink.cc/shop", "link aggregator"),
    ("https://instagram.com/x", "instagram"),
    ("https://www.instagram.com/x", "instagram"),
    ("https://x.com/shop", "twitter/x"),
    ("http://wa.me/9665", "whatsapp"),
    ("https://shop.business.site/?m=true", "google business stub"),
    ("https://mycafe.wixsite.com/home", "free site builder"),
    ("https://sites.google.com/view/shop", "free site builder"),
    ("https://easymenu.site/r/1", "delivery aggregator"),
    (None, "none"),
    ("", "none"),
])
def test_website_classification(url, expected):
    assert classify_website(url) == expected


def test_disqualifiers_score_zero():
    base = dict(category="restaurant", has_phone=True, chain=False, confidence=0.9)
    assert score_lead(**{**base, "web_presence": "real site"})[0] == 0.0
    assert score_lead(**{**base, "has_phone": False, "web_presence": "none"})[0] == 0.0
    assert score_lead(**{**base, "chain": True, "web_presence": "none"})[0] == 0.0
    assert score_lead(**{**base, "category": "atm", "web_presence": "none"})[0] == 0.0


def test_social_only_beats_no_presence():
    """Someone on Instagram has already shown they want to be findable."""
    base = dict(category="restaurant", has_phone=True, chain=False, confidence=0.8)
    social = score_lead(**base, web_presence="instagram")[0]
    nothing = score_lead(**base, web_presence="none")[0]
    assert social > nothing > 0


def test_reviews_raise_a_lead_above_an_unverified_one():
    base = dict(web_presence="instagram", category="restaurant", has_phone=True,
                chain=False, confidence=0.8)
    busy = score_lead(**base, review_count=240, rating=4.7)[0]
    assert busy > score_lead(**base)[0]


def test_trade_fit_orders_sensibly():
    assert category_fit("beauty_salon") > category_fit("grocery_store") > category_fit("atm")
    assert category_fit("atm") == 0.0
    assert category_fit(None) > 0  # unknown is plausible, not promoted


def test_apostrophe_brands_are_recognised_as_chains():
    """normalize("Hardee's") must be "hardees" or the lexicon check misses."""
    assert is_chain("Hardee's", None, None, set())
    assert is_chain("McDonald's", None, None, set())


def test_any_brand_tag_means_chain():
    """Overture tags a brand only for real brands, even when it equals the name."""
    assert is_chain("Hardee's", "Hardee's", None, set())
    assert is_chain("Whatever", None, "Q123", set())


def test_franchise_marker_inside_a_longer_name():
    assert is_chain("Rosh Rayhaan by Rotana", None, None, set())
    assert not is_chain("Bayt Karam", None, None, set())


def test_repeated_names_are_detected_as_a_chain(con):
    for i in range(3):
        con.execute(
            """INSERT INTO places (id, name_primary, category, lon, lat, confidence)
               VALUES (?, 'Local Bakery', 'bakery', ?, 24.71, 0.8)""",
            [f"lb{i}", 46.67 + i * 0.001],
        )
    chains = chain_names(con)
    assert "local bakery" in chains
    assert is_chain("Local Bakery", None, None, chains)


def test_find_leads_excludes_places_with_real_sites(con):
    leads = find_leads(con, min_score=0.0)
    assert "Kingdom Centre" not in [l.name for l in leads]  # has a website


def test_find_leads_returns_contactable_prospects(con):
    for lead in find_leads(con, min_score=0.0):
        assert lead.phone
        assert lead.web_presence != "real site"
        assert lead.reasons


def test_leads_are_sorted_best_first(con):
    scores = [l.score for l in find_leads(con, min_score=0.0)]
    assert scores == sorted(scores, reverse=True)
