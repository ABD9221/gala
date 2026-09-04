"""Bilingual (Arabic/Latin) text normalization for place search.

Arabic is the reason a generic full-text index performs badly on Gulf POI data.
The same shop name is written half a dozen ways -- ``مقهى`` vs ``مقهي``,
``القهوة`` vs ``القهوه``, with or without diacritics, with tatweel padding, with
Arabic-Indic digits -- and a byte-level index treats every variant as a
different token. Folding those variants to a single canonical form is what makes
recall usable.

The folding here is deliberately aggressive: it is applied symmetrically to both
the indexed text and the query, so a fold that loses an orthographic distinction
costs nothing as long as both sides lose it identically.
"""
from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Character classes
# ---------------------------------------------------------------------------

TATWEEL = "ـ"

# Harakat and Quranic annotation marks. Combining marks are stripped outright:
# they are optional in modern writing, so they are noise for matching.
_DIACRITICS = re.compile(
    "["
    "ً-ٟ"  # fathatan..hamza below / combining marks
    "ٰ"          # superscript alef
    "ۖ-ۭ"  # Quranic annotation
    "]"
)

# Alef variants all fold to bare alef. This is the single highest-yield rule:
# writers omit hamza on alef constantly.
_ALEF = str.maketrans({c: "ا" for c in "آأإٱ"})

# Remaining letter folds. Alef maqsura -> yeh and teh marbuta -> heh mirror what
# Saudi users actually type; hamza carriers fold to their base letter.
_LETTERS = str.maketrans(
    {
        "ى": "ي",  # alef maqsura -> yeh
        "ة": "ه",  # teh marbuta  -> heh
        "ؤ": "و",  # waw with hamza -> waw
        "ئ": "ي",  # yeh with hamza -> yeh
        "ـ": "",         # tatweel
        "ء": "",         # bare hamza
    }
)

# Arabic-Indic (U+0660..) and Extended Arabic-Indic (U+06F0..) digits.
_DIGITS = str.maketrans({chr(0x0660 + i): str(i) for i in range(10)} | {chr(0x06F0 + i): str(i) for i in range(10)})

# Apostrophes are deleted rather than spaced. Turning them into a break splits
# "Hardee's" into "hardee s", which then matches neither the brand lexicon's
# "hardees" nor a user typing "hardees" -- a chain silently reads as an
# independent business.
_APOSTROPHE = re.compile(r"['\u2019\u02bc\u02bb`\u00b4]")

# Remaining punctuation, including the Arabic comma/semicolon/question mark,
# collapses to a space so "Al-Nakheel Mall" and "Al Nakheel Mall" tokenize alike.
_PUNCT = re.compile(r"[^\w\s؀-ۿ]+", re.UNICODE)
_WS = re.compile(r"\s+")

# The Arabic definite article is a prefix, not a separate token, so a user
# searching "نخيل مول" would miss "النخيل مول". We index both the full form and
# an article-stripped form rather than always stripping, because for some names
# the article is part of the identity ("الرياض").
_ARTICLE = re.compile(r"\b(?:ال|أل|إل)(\w{3,})", re.UNICODE)

# Cross-script synonyms. Gulf POI names mix scripts freely; folding the handful
# of category words that dominate the corpus buys a large recall win cheaply.
SYNONYMS: dict[str, str] = {
    "كافيه": "قهوه",
    "كافي": "قهوه",
    "كوفي": "قهوه",
    "مقهي": "قهوه",
    "coffee": "قهوه",
    "cafe": "قهوه",
    "cafeteria": "قهوه",
    "مطعم": "مطعم",
    "restaurant": "مطعم",
    "resturant": "مطعم",  # frequent misspelling in OSM/Overture data
    "صيدليه": "صيدليه",
    "pharmacy": "صيدليه",
    "مستشفي": "مستشفي",
    "hospital": "مستشفي",
    "مسجد": "مسجد",
    "جامع": "مسجد",
    "mosque": "مسجد",
    "masjid": "مسجد",
    "سوبرماركت": "بقاله",
    "بقاله": "بقاله",
    "supermarket": "بقاله",
    "grocery": "بقاله",
    "صاله": "نادي",
    "جيم": "نادي",
    "gym": "نادي",
    "fitness": "نادي",
    "نادي": "نادي",
    "مول": "مول",
    "mall": "مول",
    "بنك": "بنك",
    "bank": "بنك",
    "فندق": "فندق",
    "hotel": "فندق",
    "صالون": "صالون",
    "salon": "صالون",
    "حلاق": "صالون",
    "barber": "صالون",
}


def strip_diacritics(text: str) -> str:
    """Remove Arabic harakat and Quranic annotation marks."""
    return _DIACRITICS.sub("", text)


def fold_arabic(text: str) -> str:
    """Fold Arabic orthographic variants to a canonical skeleton."""
    text = strip_diacritics(text)
    return text.translate(_ALEF).translate(_LETTERS)


def normalize(text: str | None) -> str:
    """Canonical form used for both indexing and querying.

    NFKC first, so Arabic presentation forms (U+FB50.. / U+FE70..) and full-width
    Latin collapse onto their canonical code points before any of our own rules
    run. Then: casefold Latin, fold Arabic, unify digits, drop punctuation.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = fold_arabic(text)
    text = text.translate(_DIGITS)
    text = _APOSTROPHE.sub("", text)
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def strip_article(text: str) -> str:
    """Drop the Arabic definite article from words long enough to survive it."""
    return _ARTICLE.sub(r"\1", text)


def apply_synonyms(tokens: list[str]) -> list[str]:
    """Map known category words onto a shared canonical token."""
    return [SYNONYMS.get(t, t) for t in tokens]


def tokenize(text: str | None, *, expand: bool = True) -> list[str]:
    """Normalize then split into search tokens.

    With ``expand`` the article-stripped variant of each token is appended, so
    "النخيل" indexes as both ``النخيل`` and ``نخيل`` and matches either query.
    """
    norm = normalize(text)
    if not norm:
        return []
    tokens = norm.split()
    if expand:
        tokens = apply_synonyms(tokens)
        extra = [s for t in tokens if (s := strip_article(t)) != t]
        tokens = tokens + extra
    # Preserve order while removing duplicates -- BM25 term frequency should not
    # be inflated by our own expansion.
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def search_text(*parts: str | None) -> str:
    """Build the indexable blob for a place from its name/category/address."""
    tokens: list[str] = []
    for p in parts:
        tokens.extend(tokenize(p))
    seen: set[str] = set()
    return " ".join(t for t in tokens if not (t in seen or seen.add(t)))


def is_arabic(text: str | None) -> bool:
    """True when the string contains at least one Arabic letter."""
    return bool(text) and any("؀" <= ch <= "ۿ" for ch in text)


# ---------------------------------------------------------------------------
# Cross-script matching
# ---------------------------------------------------------------------------

# Overture and OSM name the same place in different alphabets: in our Riyadh
# sample Overture is 62% Latin while OSM is 91% Arabic. Token similarity across
# scripts is ~0 by construction, so a place called "Coffee Hill" in one source
# and "كوفي هيل" in the other never matches on the raw strings, no matter how
# close the coordinates are. Romanising the Arabic side gives the comparison
# something to bite on.
#
# This is a matching aid, not a display transliteration -- it targets the
# consonant skeleton, because Arabic script omits short vowels and any vowel we
# invented would be noise.
_ROMAN = {
    "ا": "a", "ب": "b", "ت": "t", "ث": "th", "ج": "j", "ح": "h", "خ": "kh",
    "د": "d", "ذ": "dh", "ر": "r", "ز": "z", "س": "s", "ش": "sh", "ص": "s",
    "ض": "d", "ط": "t", "ظ": "z", "ع": "a", "غ": "gh", "ف": "f", "ق": "q",
    "ك": "k", "ل": "l", "م": "m", "ن": "n", "ه": "h", "و": "w", "ي": "y",
    "پ": "p", "چ": "ch", "ژ": "zh", "گ": "g", "ة": "h", "ى": "a",
}

# Latin spellings that recur in Gulf place names and do not fall out of the
# letter map: the definite article, and English words written phonetically.
_ROMAN_FIXUPS = [
    (re.compile(r"\bal\s+"), "al"),
    (re.compile(r"\bkwfy\b"), "coffee"),
    (re.compile(r"\bkafyh\b"), "cafe"),
    (re.compile(r"\bkfyh\b"), "cafe"),
    (re.compile(r"\bmwl\b"), "mall"),
    (re.compile(r"\brstwrant\b"), "restaurant"),
    (re.compile(r"\bmtam\b"), "restaurant"),
    (re.compile(r"\bfndq\b"), "hotel"),
    (re.compile(r"\bmstshfy\b"), "hospital"),
    (re.compile(r"\bsydlyh\b"), "pharmacy"),
    (re.compile(r"\bbnk\b"), "bank"),
    (re.compile(r"\bmsjd\b"), "mosque"),
    (re.compile(r"\bjam\b"), "mosque"),
    (re.compile(r"\bswbrmarkt\b"), "supermarket"),
    (re.compile(r"\bmrkz\b"), "center"),
    (re.compile(r"\bshark\b"), "sharq"),
]


def romanize(text: str | None) -> str:
    """Romanise Arabic text into a comparable Latin skeleton.

    Returns '' for input with no Arabic, so callers can cheaply skip it.
    """
    if not text or not is_arabic(text):
        return ""
    folded = normalize(text)
    out = "".join(_ROMAN.get(ch, ch) for ch in folded)
    for pattern, repl in _ROMAN_FIXUPS:
        out = pattern.sub(repl, out)
    return _WS.sub(" ", out).strip()


def match_variants(*names: str | None) -> set[str]:
    """All comparable forms of a place's names, for conflation.

    Includes the normalised original of every name plus a romanisation of any
    Arabic one, so an Arabic name on one side can meet a Latin name on the other.
    """
    out: set[str] = set()
    for name in names:
        if not name:
            continue
        if norm := normalize(name):
            out.add(norm)
        if roman := romanize(name):
            out.add(roman)
    return out
