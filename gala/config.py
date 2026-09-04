"""Central configuration for the Gala places stack."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Overture Maps is published as public Parquet on S3. We read it over plain
# HTTPS rather than the s3:// scheme: many managed environments inject AWS
# credentials into the proxy, which makes anonymous SigV4 requests fail with
# InvalidAccessKeyId even though the bucket is world-readable.
OVERTURE_BUCKET_URL = "https://overturemaps-us-west-2.s3.amazonaws.com/"
OVERTURE_RELEASE = os.getenv("GALA_OVERTURE_RELEASE", "2026-08-19.0")
OVERTURE_PLACES_PREFIX = f"release/{OVERTURE_RELEASE}/theme=places/type=place/"

NOMINATIM_URL = os.getenv("GALA_NOMINATIM_URL", "https://nominatim.openstreetmap.org")
WIKIDATA_URL = os.getenv("GALA_WIKIDATA_URL", "https://www.wikidata.org")
COMMONS_FILEPATH = "https://commons.wikimedia.org/wiki/Special:FilePath/"

# Nominatim's usage policy caps anonymous clients at 1 request/second and
# requires an identifying User-Agent. Both are enforced in enrich/osm.py.
NOMINATIM_MIN_INTERVAL = float(os.getenv("GALA_NOMINATIM_INTERVAL", "1.1"))
USER_AGENT = os.getenv("GALA_USER_AGENT", "gala-places/0.1 (+https://github.com/abd9221/gala)")

DB_PATH = os.getenv("GALA_DB", "data/gala.duckdb")

DEFAULT_HTTP_TIMEOUT = float(os.getenv("GALA_HTTP_TIMEOUT", "30"))


@dataclass(frozen=True)
class BBox:
    """Geographic bounding box in WGS84 degrees."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def __post_init__(self) -> None:
        if self.min_lon >= self.max_lon:
            raise ValueError(f"min_lon must be < max_lon, got {self.min_lon} >= {self.max_lon}")
        if self.min_lat >= self.max_lat:
            raise ValueError(f"min_lat must be < max_lat, got {self.min_lat} >= {self.max_lat}")

    @property
    def center(self) -> tuple[float, float]:
        return ((self.min_lon + self.max_lon) / 2, (self.min_lat + self.max_lat) / 2)


# Handy presets so the demo scripts stay readable.
PRESETS: dict[str, BBox] = {
    "olaya": BBox(46.665, 24.700, 46.690, 24.725),
    "riyadh": BBox(46.50, 24.55, 46.90, 24.90),
    "jeddah": BBox(39.10, 21.40, 39.30, 21.75),
    "dammam": BBox(49.95, 26.35, 50.20, 26.50),
}
