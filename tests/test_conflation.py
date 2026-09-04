"""Conflation: the step where a mistake attaches one shop's hours to another."""
from gala.enrich.osm import haversine_m
from gala.enrich.overpass import OsmPoi, build_query
from gala.enrich.pipeline import candidate_pairs, resolve_matches
from gala.config import BBox


def poi(osm_id, name, lon, lat, **tags):
    return OsmPoi("node", osm_id, lon, lat, {"name": name, "amenity": "cafe", **tags})


PLACES = [
    # (id, name_primary, name_ar, name_en, lon, lat)
    ("a", "Coffee Hill", None, None, 46.6750, 24.7120),
    ("b", "Caribou Coffee", None, None, 46.6741, 24.7134),
]


def test_haversine_is_accurate():
    # Kingdom Centre -> Al Faisaliah is about 2.5 km.
    assert 2400 < haversine_m(46.6744, 24.7114, 46.6853, 24.6905) < 2700


def test_cross_script_match_is_accepted():
    """The core case: Overture names in Latin, OSM names the same place in Arabic."""
    pois = [poi(1, "كوفي هيل", 46.67505, 24.71205)]
    matched = resolve_matches(candidate_pairs(PLACES, pois))
    assert matched["a"].osm_id == 1


def test_distant_namesake_is_rejected():
    """Same name, different branch across town -- proximity must veto it."""
    pois = [poi(2, "Coffee Hill", 46.7400, 24.7800)]
    assert resolve_matches(candidate_pairs(PLACES, pois)) == {}


def test_close_stranger_is_rejected():
    """Right next door but a different business -- the name must veto it."""
    pois = [poi(3, "Al Faisaliah Tower", 46.67501, 24.71201)]
    assert resolve_matches(candidate_pairs(PLACES, pois)) == {}


def test_roads_are_never_candidates():
    """A road named "طريق التخصصي الفرعي" once matched a shop called
    "Al-Thabit Doors - Takhassosi Branch". Only POI-tagged objects qualify."""
    road = OsmPoi("way", 4, 46.67501, 24.71201, {"name": "Coffee Hill", "highway": "primary"})
    assert not road.is_poi
    assert resolve_matches(candidate_pairs(PLACES, [road])) == {}


def test_matching_is_one_to_one():
    """Without this, six outlets of a chain bind to one node and share a phone."""
    pois = [poi(5, "Coffee Hill", 46.67505, 24.71205)]
    places = PLACES + [("c", "Coffee Hill", None, None, 46.67506, 24.71206)]
    matched = resolve_matches(candidate_pairs(places, pois))
    assert len(matched) == 1
    assert len({(p.osm_type, p.osm_id) for p in matched.values()}) == 1


def test_query_covers_the_bbox():
    q = build_query(BBox(46.665, 24.700, 46.690, 24.725))
    assert "24.7,46.665,24.725,46.69" in q
    assert "out center tags" in q


def test_is_poi_classification():
    assert poi(6, "x", 0.1, 0.1).is_poi
    assert OsmPoi("way", 7, 0.1, 0.1, {"name": "x", "shop": "mall"}).is_poi
    assert not OsmPoi("way", 8, 0.1, 0.1, {"name": "x"}).is_poi          # bare building
    assert not OsmPoi("way", 9, 0.1, 0.1, {"name": "x", "highway": "residential"}).is_poi
