"""A small synthetic corpus so search tests never touch the network."""
import pytest

from gala import rank
from gala.store import build_index, connect

# name, name_ar, category, lon, lat, phone, website, address, confidence, sources, hours
FIXTURES = [
    ("Caribou Coffee", None, "coffee_shop", 46.6741, 24.7134, "+966594848893", None, "Olaya St", 0.95, 3, "Mo-Su 06:00-02:00"),
    ("كوفي هيل", "كوفي هيل", "cafe", 46.6750, 24.7120, "+966114604848", None, "Olaya St", 0.81, 2, "Sa-Th 08:00-23:00"),
    ("Kingdom Centre", "مركز المملكة", "shopping_center", 46.6744, 24.7114, None, "https://kingdomcentre.com.sa", "Olaya St", 0.97, 4, "24/7"),
    ("Zara Kingdom Centre", None, "clothing_store", 46.6746, 24.7116, None, None, "Kingdom Centre, Olaya St", 0.88, 2, None),
    ("صيدلية النهدي", "صيدلية النهدي", "pharmacy", 46.6760, 24.7100, "+966920003222", None, "Olaya St", 0.9, 3, "Mo-Su 08:00-24:00"),
    ("كرسبي كريم", None, "pharmacy", 46.6739, 24.7118, None, None, "Olaya St", 0.96, 2, None),
    ("Far Away Cafe", None, "cafe", 46.7400, 24.7800, None, None, "Elsewhere", 0.7, 1, None),
]


@pytest.fixture()
def con(tmp_path):
    connection = connect(tmp_path / "test.duckdb")
    for i, (name, name_ar, cat, lon, lat, phone, site, addr, conf, sources, hours) in enumerate(FIXTURES):
        connection.execute(
            """INSERT INTO places
               (id, name_primary, name_ar, category, lon, lat, phone, website,
                address_freeform, confidence, source_count, opening_hours)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [f"p{i}", name, name_ar, cat, lon, lat, phone, site, addr, conf, sources, hours],
        )
    rank.recompute(connection)
    build_index(connection)
    yield connection
    connection.close()
