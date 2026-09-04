"""District catalog: deriving workable extents from point-only OSM data."""
import math

import pytest

from gala.districts import (
    MAX_ASSIGN_M, MAX_RADIUS_M, MIN_RADIUS_M, District, _assign_boxes, assign, city_bbox,
)


def raw(name, lon, lat, bounds=None):
    return {"name": name, "name_en": None, "place_type": "neighbourhood",
            "osm_type": "node", "osm_id": 1, "lon": lon, "lat": lat, "bounds": bounds}


def test_real_boundaries_are_kept():
    bounds = {"minlon": 39.1, "minlat": 21.5, "maxlon": 39.2, "maxlat": 21.6}
    d = _assign_boxes([raw("X", 39.15, 21.55, bounds)], "jeddah")[0]
    assert d.bbox_source == "osm"
    assert (d.min_lon, d.max_lat) == (39.1, 21.6)


def test_point_districts_get_a_derived_box():
    """171 of Jeddah's 176 districts are points; without this they are unusable."""
    ds = _assign_boxes([raw("A", 39.10, 21.50), raw("B", 39.13, 21.50)], "jeddah")
    assert all(d.bbox_source == "derived" for d in ds)
    assert all(d.area_km2 > 0 for d in ds)


def test_derived_radius_follows_neighbour_spacing():
    """Tightly packed districts get small boxes, isolated ones get large."""
    dense = _assign_boxes([raw("A", 39.100, 21.5), raw("B", 39.105, 21.5)], "jeddah")[0]
    sparse = _assign_boxes([raw("A", 39.10, 21.5), raw("B", 39.40, 21.5)], "jeddah")[0]
    assert dense.area_km2 < sparse.area_km2


def test_derived_radius_is_clamped():
    touching = _assign_boxes([raw("A", 39.1000, 21.5), raw("B", 39.1001, 21.5)], "jeddah")[0]
    assert touching.area_km2 >= (2 * MIN_RADIUS_M / 1000) ** 2 * 0.9
    lonely = _assign_boxes([raw("A", 39.1, 21.5)], "jeddah")[0]
    assert lonely.area_km2 <= (2 * MAX_RADIUS_M / 1000) ** 2 * 1.1


def test_longitude_is_scaled_by_latitude():
    """A square in metres is not a square in degrees; at Jeddah's latitude
    ignoring the cosine under-covers each district east and west by ~7%."""
    d = _assign_boxes([raw("A", 39.1, 21.5)], "jeddah")[0]
    width_deg = d.max_lon - d.min_lon
    height_deg = d.max_lat - d.min_lat
    assert width_deg > height_deg
    assert width_deg / height_deg == pytest.approx(1 / math.cos(math.radians(21.5)), rel=0.02)


def _district(name, lon, lat, half=0.01):
    return District(name=name, name_en=None, city="jeddah", lon=lon, lat=lat,
                    min_lon=lon - half, min_lat=lat - half,
                    max_lon=lon + half, max_lat=lat + half,
                    place_type="neighbourhood", osm_type="node", osm_id=1,
                    bbox_source="derived")


def test_assign_prefers_a_containing_district():
    a, b = _district("A", 39.10, 21.50), _district("B", 39.30, 21.50)
    assert assign(39.101, 21.501, [a, b]).name == "A"


def test_assign_breaks_overlap_ties_by_distance():
    """Derived boxes overlap, so containment alone is ambiguous."""
    a, b = _district("A", 39.100, 21.50, half=0.02), _district("B", 39.108, 21.50, half=0.02)
    assert assign(39.107, 21.50, [a, b]).name == "B"


def test_assign_refuses_a_far_away_label():
    """A label 50 km away is worse than no label."""
    assert assign(40.0, 22.5, [_district("A", 39.10, 21.50)]) is None


def test_city_bbox_covers_every_district():
    ds = [_district("A", 39.10, 21.50), _district("B", 39.30, 21.70)]
    box = city_bbox(ds)
    assert box.min_lon <= min(d.min_lon for d in ds)
    assert box.max_lat >= max(d.max_lat for d in ds)


def test_shipped_jeddah_catalog_is_usable():
    from gala.districts import find, load

    catalog = load("jeddah")
    assert len(catalog) > 100
    assert all(d.area_km2 > 0 for d in catalog)
    # Arabic lookup must survive spelling variation -- that is what
    # gala.normalize is for.
    assert find("الروضة", "jeddah") is not None
    assert find("الروضه", "jeddah") is not None
