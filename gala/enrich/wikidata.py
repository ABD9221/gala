"""Photos and a notability signal from Wikidata / Wikimedia Commons.

This closes the third gap against Google Places. Two things come out of a
Wikidata id:

* **P18 (image)** -> a Commons file, resolvable to a stable image URL via
  ``Special:FilePath``. Coverage is limited to notable places -- malls, towers,
  museums, mosques -- but those are exactly the places a user expects a photo
  for, and the licence permits redisplay, which Google's photo API does not.
* **Sitelink count** -> how many Wikipedia language editions carry an article.
  It is a decent, entirely free proxy for real-world prominence, and it feeds
  the ranking model in ``gala.rank``.

Requests are batched 50 ids at a time (the API's documented ceiling); enriching
a city one entity at a time would be needlessly slow and rude.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass
from typing import Any, Iterable, Iterator

import requests

from ..config import COMMONS_FILEPATH, DEFAULT_HTTP_TIMEOUT, WIKIDATA_URL
from ..http import make_session

BATCH_SIZE = 50
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


@dataclass
class WikidataInfo:
    qid: str
    image_file: str | None
    image_url: str | None
    sitelinks: int
    attribution: str | None = None


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def commons_url(filename: str) -> str:
    """Build a stable direct URL for a Commons file."""
    return COMMONS_FILEPATH + urllib.parse.quote(filename.replace(" ", "_"))


def fetch_entities(qids: Iterable[str], *, client: requests.Session | None = None) -> dict[str, WikidataInfo]:
    """Fetch image + sitelink count for a set of Wikidata ids."""
    ids = sorted({q.strip() for q in qids if q and q.strip().startswith("Q")})
    if not ids:
        return {}

    owns_client = client is None
    client = client or make_session()
    out: dict[str, WikidataInfo] = {}
    try:
        for batch in _chunks(ids, BATCH_SIZE):
            resp = client.get(
                f"{WIKIDATA_URL}/w/api.php",
                timeout=DEFAULT_HTTP_TIMEOUT,
                params={
                    "action": "wbgetentities",
                    "ids": "|".join(batch),
                    # Only the two properties we use -- entities are large.
                    "props": "claims|sitelinks",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            for qid, entity in (resp.json().get("entities") or {}).items():
                if "missing" in entity:
                    continue
                image_file = _first_image(entity)
                out[qid] = WikidataInfo(
                    qid=qid,
                    image_file=image_file,
                    image_url=commons_url(image_file) if image_file else None,
                    sitelinks=len(entity.get("sitelinks") or {}),
                )
    finally:
        if owns_client:
            client.close()
    return out


def _first_image(entity: dict[str, Any]) -> str | None:
    for claim in (entity.get("claims") or {}).get("P18", []):
        value = (claim.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(value, str):
            return value
    return None


def fetch_attribution(filenames: Iterable[str], *, client: requests.Session | None = None) -> dict[str, str]:
    """Fetch artist + licence for Commons files.

    Commons content is free to reuse but almost always requires credit, so a
    UI that shows these photos needs this string. Skipping it would make the
    feature legally unusable rather than merely incomplete.
    """
    names = sorted({f for f in filenames if f})
    if not names:
        return {}

    owns_client = client is None
    client = client or make_session()
    out: dict[str, str] = {}
    try:
        for batch in _chunks(names, BATCH_SIZE):
            resp = client.get(
                COMMONS_API,
                timeout=DEFAULT_HTTP_TIMEOUT,
                params={
                    "action": "query",
                    "titles": "|".join(f"File:{n}" for n in batch),
                    "prop": "imageinfo",
                    "iiprop": "extmetadata",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            for page in (resp.json().get("query", {}).get("pages") or {}).values():
                info = (page.get("imageinfo") or [{}])[0]
                meta = info.get("extmetadata") or {}
                artist = _plain(meta.get("Artist", {}).get("value"))
                licence = _plain(meta.get("LicenseShortName", {}).get("value"))
                title = page.get("title", "").removeprefix("File:")
                if title and (artist or licence):
                    out[title] = " / ".join(p for p in (artist, licence) if p)
    finally:
        if owns_client:
            client.close()
    return out


def _plain(html: str | None) -> str | None:
    """Strip the HTML Commons wraps its metadata in."""
    if not html:
        return None
    import re

    text = re.sub(r"<[^>]+>", "", html)
    return " ".join(text.split()) or None
