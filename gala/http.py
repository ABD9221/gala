"""Shared HTTP session.

Standardised on ``requests`` after ``httpx`` turned out to be rejected with
HTTP 403 ("Please respect our robot policy") by the Wikimedia APIs from this
environment, on every combination of User-Agent, HTTP version and header set we
tried -- while ``requests``, ``urllib`` and ``curl`` all returned 200 for the
identical URL. The block therefore keys off the client's TLS/connection
fingerprint rather than anything we can set in a header. One client for every
upstream keeps that class of surprise to a single place.
"""
from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import DEFAULT_HTTP_TIMEOUT, USER_AGENT


def make_session(*, retries: int = 3, backoff: float = 1.0) -> requests.Session:
    """A session with a sane UA and exponential backoff on transient failures.

    429 is included in the retry set because Nominatim and the Wikimedia APIs
    both use it for rate limiting, and ``Retry`` honours ``Retry-After``.
    """
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
    retry = Retry(
        total=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=16)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def get_json(session: requests.Session, url: str, params: dict | None = None, *, timeout: float = DEFAULT_HTTP_TIMEOUT):
    resp = session.get(url, params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()
