#!/usr/bin/env python3
"""Work Radar scanner.

A time-budgeted, manually triggered scan over top-tier work/employment journals
and government + institutional publishers, looking for evidence about the future
of work with a standing focus on hybrid and remote working.

The scan is deliberately bounded. It runs for a fixed wall-clock budget (default
ten minutes), split into per-source-family stage slices. Whatever it reaches in
that time is merged into a cumulative corpus stored in work_radar.json. Rotation
cursors are persisted, so consecutive manual runs walk forward through the query
and source universe instead of re-reading the same first page every time.

Admission and matrix placement are separate decisions:

  * Admission asks whether an item belongs in the corpus at all: is it about
    work, does it say something about where work happens, and does it come from
    a source on the list.
  * Placement asks whether the item can be defensibly located in the 2x2. That
    needs directional evidence on both axes from the same document. Items that
    pass admission but not placement are kept as "unplaced" rather than guessed
    into a quadrant.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import math
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup
from dateutil.relativedelta import relativedelta

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "work_radar_config.json"
DATA_PATH = ROOT / "work_radar.json"

CONFIG: dict[str, Any] = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# Run-scoped state
# ---------------------------------------------------------------------------

SCAN_DEADLINE_MONO: float | None = None
KNOWN_IDENTITIES: set[str] = set()
KNOWN_LINKS: set[str] = set()
SEEN_INSTITUTION_URLS: dict[str, str] = {}
DIAGNOSTICS: Counter = Counter()
DIAG_LOCK = threading.Lock()

DATE_FLOOR = dt.date.today() - relativedelta(months=int(CONFIG.get("lookback_months", 30)))
EXTENDED_FLOOR = dt.date.today() - relativedelta(
    months=int(CONFIG.get("extended_top_quality_lookback_months", 48))
)

UA = "WorkRadar/1.0 (future-of-work evidence scanner)"
SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": UA,
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
)

OPENALEX_MAILTO = os.environ.get("OPENALEX_MAILTO", "").strip()
CROSSREF_MAILTO = os.environ.get("CROSSREF_MAILTO", "").strip()


def log(message: str) -> None:
    """Flush progress so a long scan never looks hung in the Actions log."""
    elapsed = time.monotonic() - log.started
    print(f"[work-radar +{elapsed:6.1f}s] {message}", flush=True)


log.started = time.monotonic()


def diag(key: str, amount: int = 1) -> None:
    with DIAG_LOCK:
        DIAGNOSTICS[key] += amount


# ---------------------------------------------------------------------------
# Time budget
# ---------------------------------------------------------------------------


def budget_remaining() -> float:
    if SCAN_DEADLINE_MONO is None:
        return float("inf")
    return SCAN_DEADLINE_MONO - time.monotonic()


def deadline_reached(reserve_seconds: float = 0) -> bool:
    return budget_remaining() <= reserve_seconds


def stage_deadline_reached(stage_deadline: float | None, reserve_seconds: float = 0) -> bool:
    """Respect both the overall scan budget and the source family's time slice."""
    if deadline_reached(reserve_seconds):
        return True
    return stage_deadline is not None and time.monotonic() >= stage_deadline


def new_stage_deadline(seconds: float, reserve: float) -> float:
    """A stage never outlives the global budget minus the finalise reserve."""
    available = max(0.0, budget_remaining() - reserve)
    return time.monotonic() + min(float(seconds), available)


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).strip()


def norm_title(text: str) -> str:
    return normalized(text)[:220]


def normalized_link(value: Any) -> str:
    raw = clean_text(value).lower().rstrip("/")
    raw = re.sub(r"[?#].*$", "", raw)
    return raw


def url_domain(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower().removeprefix("www.")
    except Exception:
        return ""


def stable_identity(title: str = "", doi_or_link: str = "") -> str:
    """Cheap DOI/title identity, usable before any expensive classification."""
    raw = str(doi_or_link or "").lower()
    match = re.search(r"10\.\d{4,9}/[^\s?#]+", raw)
    if match:
        return "doi:" + match.group(0).rstrip(".,)")
    return "title:" + norm_title(title)


def parse_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    raw = clean_text(value)
    if not raw:
        return None
    raw = raw.replace("Z", "")
    for pattern in ("%Y-%m-%d", "%Y-%m", "%Y", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(raw[: len(dt.datetime.now().strftime(pattern)) + 4], pattern).date()
        except Exception:
            continue
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if match:
        try:
            return dt.date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except Exception:
            return None
    match = re.search(r"\b(19|20)\d{2}\b", raw)
    if match:
        try:
            return dt.date(int(match.group(0)), 1, 1)
        except Exception:
            return None
    return None


def split_sentences(text: str, max_chars: int = 60000) -> list[str]:
    body = clean_text(text)[:max_chars]
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", body)
    return [p.strip() for p in parts if len(p.strip()) > 12]


def distinct_matches(text: str, phrases: Iterable[str]) -> list[str]:
    """Word-boundary phrase matching, de-duplicated and order-stable."""
    haystack = " " + normalized(text) + " "
    found: list[str] = []
    for phrase in phrases:
        needle = normalized(phrase)
        if not needle:
            continue
        if f" {needle} " in haystack and needle not in found:
            found.append(needle)
    return found


def contains_any(text: str, phrases: Iterable[str]) -> bool:
    return bool(distinct_matches(text, phrases))


def probably_english(text: str) -> bool:
    """Cheap English check.

    The script-ratio test works on any length. The stopword test does not: a
    perfectly good English title carries only one or two function words, so
    applying it to short text rejects most of the corpus. It runs only once
    there is enough prose to support it.
    """
    if not bool(CONFIG.get("english_only", True)):
        return True
    body = clean_text(text)
    if len(body) < 40:
        return True
    letters = sum(1 for ch in body if ch.isalpha())
    if not letters:
        return False
    ascii_letters = sum(1 for ch in body if ch.isalpha() and ord(ch) < 128)
    if ascii_letters / letters < 0.85:
        return False
    if len(body.split()) < int(CONFIG.get("english_stopword_test_min_words", 30)):
        return True
    stopwords = (" the ", " and ", " of ", " to ", " in ", " that ", " with ", " for ", " is ", " are ", " on ", " we ")
    lowered = " " + body.lower() + " "
    return sum(1 for w in stopwords if w in lowered) >= 3


def word_count(text: str) -> int:
    return len(clean_text(text).split())


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def rotating_batch(items: list[Any], cursor: int, limit: int) -> tuple[list[Any], int, bool]:
    """Bounded circular slice plus the next cursor.

    The cursor lives in work_radar.json so that the next manual run continues
    through the universe rather than restarting at the first item. The batch
    never wraps inside a single run: the final batch of a cycle is simply
    shorter, which avoids re-requesting the head before the checkpoint is saved.
    """
    seq = list(items)
    if not seq:
        return [], 0, True
    size = len(seq) if limit <= 0 else min(len(seq), max(1, int(limit)))
    start = int(cursor or 0) % len(seq)
    end = min(len(seq), start + size)
    batch = seq[start:end]
    wrapped = end >= len(seq)
    return batch, (0 if wrapped else end), wrapped


def commit_cursor(state: dict[str, Any], key: str, original: int, planned_next: int, executed_count: int) -> None:
    """Only advance a cursor when the batch actually ran.

    A stage that dies on its first request must not silently skip that slice of
    the universe on the next run.
    """
    state[key] = planned_next if executed_count > 0 else int(original or 0)


# ---------------------------------------------------------------------------
# Source quality
# ---------------------------------------------------------------------------


def journal_tier(name: str) -> tuple[int | None, str]:
    n = normalized(name)
    if not n:
        return None, ""
    for candidate in CONFIG.get("tier1_journals", []):
        if n == normalized(candidate):
            return 1, "Tier 1 journal"
    for candidate in CONFIG.get("tier2_journals", []):
        if n == normalized(candidate):
            return 2, "Tier 2 journal"
    return None, ""


def institution_for_domain(domain: str) -> dict[str, Any] | None:
    d = (domain or "").lower().removeprefix("www.")
    if not d:
        return None
    for src in CONFIG.get("institution_sources", []):
        allowed = str(src.get("domain", "")).lower().removeprefix("www.")
        if allowed and (d == allowed or d.endswith("." + allowed)):
            return src
    return None


def trusted_publisher(name: str) -> bool:
    n = normalized(name)
    if not n:
        return False
    return any(normalized(p) in n for p in CONFIG.get("trusted_scholarly_publishers", []))


# ---------------------------------------------------------------------------
# Topic gate
# ---------------------------------------------------------------------------


def exclusion_reason(title: str, text: str = "") -> str:
    blob = f"{title} {text}"
    hits = distinct_matches(blob, CONFIG.get("exclusion_terms", []))
    if hits:
        return f"boilerplate: {hits[0]}"
    if len(clean_text(title)) < 12:
        return "title too short"
    return ""


def topic_evidence(title: str, abstract: str, body: str = "") -> dict[str, Any]:
    """Does this item concern work, and does it say where work happens?"""
    blob = " ".join([title, abstract, body])
    work_hits = distinct_matches(blob, CONFIG.get("work_terms", []))
    location_hits = distinct_matches(blob, CONFIG.get("location_of_work_terms", []))
    effect_hits = distinct_matches(blob, CONFIG.get("effectiveness_terms", []))

    # The location signal has to appear in the title or abstract, not only deep
    # in a page's footer navigation.
    front = " ".join([title, abstract])
    front_location = distinct_matches(front, CONFIG.get("location_of_work_terms", []))

    return {
        "work_hits": work_hits,
        "location_hits": location_hits,
        "front_location_hits": front_location,
        "effect_hits": effect_hits,
        "on_topic": bool(work_hits) and bool(front_location),
    }


# ---------------------------------------------------------------------------
# 2x2 matrix classification
# ---------------------------------------------------------------------------


def _find_all(haystack: str, needle: str) -> list[int]:
    """All start offsets of a whitespace-delimited term."""
    spots: list[int] = []
    target = f" {needle} "
    start = haystack.find(target)
    while start >= 0:
        spots.append(start)
        start = haystack.find(target, start + 1)
    return spots


def _negated_at(haystack: str, index: int) -> bool:
    window = haystack[max(0, index - int(CONFIG.get("negation_window_chars", 55))) : index + 1]
    return any(f" {normalized(t)} " in window for t in CONFIG.get("negation_terms", []) if normalized(t))


def cue_hits_with_polarity(sentence: str, cues: Iterable[str]) -> list[tuple[str, int]]:
    """Find cues in a sentence, with negation scoped to the preceding clause.

    Whole-sentence negation is too blunt: "remote work raised productivity but
    did not reduce collaboration" would flip every cue in the sentence. Looking
    only at the short window before each match keeps the two clauses apart.
    """
    haystack = " " + normalized(sentence) + " "
    hits: list[tuple[str, int]] = []
    seen: set[str] = set()
    for cue in cues:
        needle = normalized(cue)
        if not needle or needle in seen:
            continue
        spots = _find_all(haystack, needle)
        if not spots:
            continue
        seen.add(needle)
        hits.append((needle, -1 if _negated_at(haystack, spots[0]) else 1))
    return hits


def _hedged(sentence: str) -> bool:
    return contains_any(sentence, CONFIG.get("hedge_terms", []))


def effectiveness_reading(sentence: str) -> tuple[float, list[str]]:
    """Read an effectiveness claim out of one sentence, compositionally.

    Research abstracts do not say "increased productivity". They say "positively
    related to organizational performance", "the hidden costs of hybrid working",
    "negatively impacted engagement". So instead of matching fixed phrases, this
    pairs a valence word with an outcome noun inside a short window and lets the
    noun's orientation decide the sign: reduced turnover is a gain, reduced
    productivity is a loss. Null markers such as "unaffected" or "mixed findings"
    cancel a pair instead of voting, and negation is scoped to the clause before
    the valence word.
    """
    haystack = " " + normalized(sentence) + " "
    window = int(CONFIG.get("proximity_window_chars", 70))
    evidence: list[str] = []
    score = 0.0

    nouns: list[tuple[int, str, int]] = []
    for noun in CONFIG.get("outcome_nouns_good_when_high", []):
        for spot in _find_all(haystack, normalized(noun)):
            nouns.append((spot, normalized(noun), 1))
    for noun in CONFIG.get("outcome_nouns_good_when_low", []):
        for spot in _find_all(haystack, normalized(noun)):
            nouns.append((spot, normalized(noun), -1))
    if not nouns:
        return 0.0, []

    null_spots = [s for term in CONFIG.get("valence_null", []) for s in _find_all(haystack, normalized(term))]

    def suppressed(position: int) -> bool:
        return any(abs(position - spot) <= window for spot in null_spots)

    used: set[tuple[str, str]] = set()
    for valence_list, sign in (
        (CONFIG.get("valence_positive", []), 1),
        (CONFIG.get("valence_negative", []), -1),
    ):
        for word in valence_list:
            needle = normalized(word)
            if not needle:
                continue
            for spot in _find_all(haystack, needle):
                if suppressed(spot):
                    continue
                nearest = None
                for noun_spot, noun, orientation in nouns:
                    if abs(noun_spot - spot) <= window and not suppressed(noun_spot):
                        distance = abs(noun_spot - spot)
                        if nearest is None or distance < nearest[0]:
                            nearest = (distance, noun, orientation)
                if nearest is None:
                    continue
                _distance, noun, orientation = nearest
                if (needle, noun) in used:
                    continue
                used.add((needle, noun))
                polarity = -1 if _negated_at(haystack, spot) else 1
                score += sign * orientation * polarity
                evidence.append(f"{needle} + {noun}")

    for marker, sign in [(m, 1) for m in CONFIG.get("standalone_positive_markers", [])] + [
        (m, -1) for m in CONFIG.get("standalone_negative_markers", [])
    ]:
        needle = normalized(marker)
        spots = _find_all(haystack, needle)
        if spots and not suppressed(spots[0]):
            score += sign * (-1 if _negated_at(haystack, spots[0]) else 1)
            evidence.append(needle)

    if _hedged(sentence):
        score *= 0.5
    return score, evidence


def arrangement_reading(sentences: list[str]) -> tuple[float, list[str]]:
    """Decide which working arrangement the document actually speaks to.

    Most of this literature studies an arrangement rather than forecasting a
    trend, so the horizontal axis asks what is under study: remote and hybrid,
    or on-site and return-to-office. Office terms are weighted above remote
    terms because a paper about remote work mentions the office in passing far
    more often than the reverse, and explicit mandate language weighs more still.
    """
    remote_terms = CONFIG.get("remote_family_terms", [])
    onsite_terms = CONFIG.get("onsite_family_terms", [])
    onsite_weight = float(CONFIG.get("x_onsite_weight", 2.0))
    trend_weight = float(CONFIG.get("x_trend_weight", 1.5))
    score = 0.0
    evidence: list[str] = []

    for sentence in sentences:
        haystack = " " + normalized(sentence) + " "
        for term in remote_terms:
            needle = normalized(term)
            if needle and _find_all(haystack, needle):
                score += 1.0
                evidence.append(needle)
        for term in onsite_terms:
            needle = normalized(term)
            if needle and _find_all(haystack, needle):
                score -= onsite_weight
                evidence.append(needle)

    blob = " ".join(sentences)
    for cue in distinct_matches(blob, CONFIG.get("trend_more_remote_cues", [])):
        if contains_any(blob, remote_terms):
            score += trend_weight
            evidence.append(f"trend: {cue}")
    for cue in distinct_matches(blob, CONFIG.get("trend_less_remote_cues", [])):
        score -= trend_weight
        evidence.append(f"trend: {cue}")

    return score, evidence


def arrangement_terms() -> list[str]:
    return list(CONFIG.get("remote_family_terms", [])) + list(CONFIG.get("onsite_family_terms", []))


def outcome_terms() -> list[str]:
    return list(CONFIG.get("outcome_nouns_good_when_high", [])) + list(
        CONFIG.get("outcome_nouns_good_when_low", [])
    )


def evidence_sentences(text: str) -> list[str]:
    """Sentences where a working-arrangement term and an outcome noun co-occur.

    This co-occurrence is what stops the classifier from pairing an arrangement
    claim in one paragraph with an unrelated outcome claim in another.
    """
    arrangements = arrangement_terms()
    outcomes = outcome_terms()
    out: list[str] = []
    for sentence in split_sentences(text):
        if contains_any(sentence, arrangements) and contains_any(sentence, outcomes):
            out.append(sentence)
    return out


def evidence_strength(text: str) -> tuple[str, list[str]]:
    strengths = CONFIG.get("evidence_strength_terms", {})
    for label in ("experimental", "quasi_experimental", "observational"):
        hits = distinct_matches(text, strengths.get(label, []))
        if hits:
            return label, hits
    return "unspecified", []


def classify_matrix(title: str, abstract: str, body: str = "") -> dict[str, Any]:
    """Place an item in the 2x2, or explain why it cannot be placed.

    X axis: which working arrangement the evidence speaks to.
    Y axis: whether effectiveness comes out higher or lower.

    The effectiveness reading is taken only from sentences that tie an outcome
    to an arrangement, so the two axes are about the same claim.
    """
    blob = " ".join(x for x in (title, abstract, body) if x)
    all_sentences = split_sentences(blob)
    linked = evidence_sentences(blob)

    x_raw, x_evidence = arrangement_reading(all_sentences)

    y_raw = 0.0
    y_evidence: list[str] = []
    for sentence in linked:
        score, evidence = effectiveness_reading(sentence)
        y_raw += score
        y_evidence.extend(evidence)

    margin = float(CONFIG.get("matrix_margin", 1))
    min_sentences = int(CONFIG.get("min_matrix_evidence_sentences", 1))

    reasons: list[str] = []
    if len(linked) < min_sentences:
        reasons.append("no sentence ties a working arrangement to an outcome")
    if abs(x_raw) < margin:
        reasons.append("the arrangement under study is not clear enough to place")
    if abs(y_raw) < margin:
        reasons.append("no clear direction on the effectiveness axis")

    strength, strength_hits = evidence_strength(blob)
    common = {
        "matrix_x_score": round(x_raw, 2),
        "matrix_y_score": round(y_raw, 2),
        "matrix_evidence_sentences": linked[:3],
        "matrix_x_evidence": sorted(set(x_evidence))[:8],
        "matrix_y_evidence": sorted(set(y_evidence))[:8],
        "evidence_strength": strength,
        "evidence_strength_hits": strength_hits[:4],
    }

    if reasons:
        return {
            "matrix_cell": "",
            "matrix_x": "",
            "matrix_y": "",
            "matrix_unplaced_reason": "; ".join(reasons),
            **common,
        }

    x_key = "more_remote" if x_raw > 0 else "less_remote"
    y_key = "higher_effectiveness" if y_raw > 0 else "lower_effectiveness"
    return {
        "matrix_cell": f"{x_key}-{y_key}",
        "matrix_x": x_key,
        "matrix_y": y_key,
        "matrix_unplaced_reason": "",
        **common,
    }


def load_manual_placements() -> dict[str, dict[str, Any]]:
    """Curator overrides, keyed by DOI or link.

    The classifier reads language, not meaning, and it will sometimes get a
    paper wrong: an abstract that asks "Is workplace flexibility penalised?" and
    answers yes still reads as mixed to a lexical scorer. Rather than tune the
    vocabulary until one paper lands correctly and three others break, a curator
    can pin a placement here and the scanner will respect it on every run.
    """
    path = ROOT / "manual_placements.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"manual_placements.json could not be read ({type(exc).__name__}); ignoring it")
        return {}
    entries = raw.get("placements") if isinstance(raw, dict) else raw
    out: dict[str, dict[str, Any]] = {}
    valid_cells = set(CONFIG.get("matrix_cells", {}))
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        key = normalized_link(entry.get("link", "")) or stable_identity(entry.get("title", ""), "")
        cell = clean_text(entry.get("matrix_cell"))
        if not key:
            continue
        if cell and cell not in valid_cells:
            log(f"manual placement for {key[:60]} names an unknown cell {cell!r}; ignoring it")
            continue
        out[key] = {"matrix_cell": cell, "note": clean_text(entry.get("note"))}
    if out:
        log(f"loaded {len(out)} curator placement overrides")
    return out


def apply_manual_placement(item: dict[str, Any], overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    key = normalized_link(item.get("link", "")) or stable_identity(item.get("title", ""), "")
    override = overrides.get(key)
    if not override:
        return item
    cell = override["matrix_cell"]
    item["matrix_cell"] = cell
    if cell:
        x_key, y_key = cell.split("-", 1)
        item["matrix_x"] = x_key
        item["matrix_y"] = y_key
        item["matrix_unplaced_reason"] = ""
    else:
        item["matrix_x"] = ""
        item["matrix_y"] = ""
        item["matrix_unplaced_reason"] = "held unplaced by a curator"
    item["placement_source"] = "curator"
    item["curator_note"] = override["note"]
    diag("manual_placement_applied")
    return item


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------


def admit(candidate: dict[str, Any]) -> dict[str, Any] | None:
    """Apply the admission gate and attach matrix placement.

    Placement never gates admission. An item that is clearly on topic and from a
    listed source stays in the corpus even when it cannot be placed.
    """
    title = clean_text(candidate.get("title"))
    abstract = clean_text(candidate.get("abstract"))
    body = clean_text(candidate.get("body"))

    reason = exclusion_reason(title, abstract)
    if reason:
        diag(f"reject_{reason.split(':')[0]}")
        return None

    if not probably_english(f"{title} {abstract}"):
        diag("reject_not_english")
        return None

    published = candidate.get("date")
    if not isinstance(published, dt.date):
        published = parse_date(published)
    if published is None:
        diag("reject_no_date")
        return None

    tier = int(candidate.get("tier", 9))
    floor = EXTENDED_FLOOR if tier <= 1 else DATE_FLOOR
    if published < floor:
        diag("reject_too_old")
        return None
    # Journals routinely carry a forward issue date for accepted articles, so a
    # scholarly item dated some months ahead is normal metadata rather than an
    # error. An institutional page dated in the future is not.
    ahead_days = int(
        CONFIG.get("max_future_days_scholarly", 400)
        if candidate.get("kind") == "scholarly"
        else CONFIG.get("max_future_days_institutional", 45)
    )
    if published > dt.date.today() + dt.timedelta(days=ahead_days):
        diag("reject_future_dated")
        return None

    if tier >= 9:
        diag("reject_source_not_listed")
        return None

    text_words = word_count(abstract) + word_count(body)
    if candidate.get("kind") == "scholarly" and word_count(abstract) < int(CONFIG.get("min_abstract_words", 40)):
        diag("reject_thin_abstract")
        return None
    if candidate.get("kind") == "institutional" and text_words < int(CONFIG.get("institution_min_words", 400)):
        diag("reject_thin_page")
        return None

    topic = topic_evidence(title, abstract, body)
    if not topic["on_topic"]:
        diag("reject_off_topic")
        return None

    placement = classify_matrix(title, abstract, body)

    item: dict[str, Any] = {
        "title": title,
        "authors": clean_text(candidate.get("authors")) or "Unattributed",
        "source": clean_text(candidate.get("source")) or "Unknown source",
        "source_tier": f"Tier {tier}",
        "source_kind": candidate.get("kind", "scholarly"),
        "date": published.isoformat(),
        "link": clean_text(candidate.get("link")),
        "type": candidate.get("type", "peer-reviewed article"),
        "abstract": abstract[:2400],
        "work_evidence": topic["work_hits"][:8],
        "location_evidence": topic["front_location_hits"][:8],
        "effect_evidence": topic["effect_hits"][:8],
        "discovered_via": candidate.get("discovered_via", ""),
        "placement_source": "classifier",
        "curator_note": "",
    }
    item.update(placement)
    diag("admitted")
    diag("placed" if placement["matrix_cell"] else "unplaced")
    return item


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


class SourceStopped(RuntimeError):
    """Raised when an endpoint rate-limits us; the family stops for this run."""


def get(url: str, timeout: int | None = None, retries: int | None = None, **kwargs: Any) -> requests.Response | None:
    """Fetch a URL, backing off on 429 before giving up on the whole family.

    A single 429 usually means we asked too fast, not that the endpoint is
    closed to us. Retrying after a short cooldown recovers the run; only a
    persistent 429 stops the source family, so the remaining stages keep their
    budget instead of burning it on a dead endpoint.
    """
    attempts = int(CONFIG.get("public_retries", 2)) if retries is None else int(retries)
    cooldown = float(CONFIG.get("public_429_cooldown_seconds", 6))
    for attempt in range(attempts + 1):
        try:
            response = SESSION.get(
                url, timeout=timeout or int(CONFIG.get("request_timeout_seconds", 12)), **kwargs
            )
        except Exception as exc:
            diag("http_error")
            log(f"request failed {url[:90]}: {type(exc).__name__}")
            return None
        if response.status_code == 429:
            diag("http_429")
            if attempt >= attempts or deadline_reached(cooldown + 5):
                raise SourceStopped(f"429 from {url_domain(url)} after {attempt + 1} attempts")
            time.sleep(cooldown * (attempt + 1))
            continue
        if response.status_code >= 400:
            diag("http_status_error")
            return None
        return response
    return None


class RateGate:
    """Simple shared minimum-interval throttle for a public API."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = float(min_interval)
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            gap = self.min_interval - (time.monotonic() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


def openalex_abstract(inverted: dict[str, list[int]] | None) -> str:
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in inverted.items():
        for spot in spots or []:
            positions.append((int(spot), word))
    positions.sort()
    return clean_text(" ".join(word for _, word in positions))


def openalex_source_name(work: dict[str, Any]) -> tuple[str, str]:
    src = (work.get("primary_location") or {}).get("source") or {}
    return clean_text(src.get("display_name")), clean_text(
        src.get("host_organization_name") or src.get("publisher")
    )


def openalex_authors(work: dict[str, Any]) -> str:
    names = []
    for entry in (work.get("authorships") or [])[:8]:
        name = clean_text((entry.get("author") or {}).get("display_name"))
        if name:
            names.append(name)
    if len(work.get("authorships") or []) > 8:
        names.append("et al.")
    return ", ".join(names)


def openalex_candidate(work: dict[str, Any], query: str) -> dict[str, Any] | None:
    title = clean_text(work.get("title") or work.get("display_name"))
    doi = clean_text(work.get("doi"))
    if not title:
        return None
    if bool(CONFIG.get("skip_known_items_before_classification", True)):
        if stable_identity(title, doi) in KNOWN_IDENTITIES:
            diag("skip_known_openalex")
            return None

    source_name, host = openalex_source_name(work)
    tier, _label = journal_tier(source_name)
    kind = "scholarly"
    type_label = "peer-reviewed article"

    if tier is None:
        # Working papers from listed institutions still count, at the tier the
        # institution carries.
        for location in [work.get("primary_location") or {}, work.get("best_oa_location") or {}]:
            landing = clean_text(location.get("landing_page_url"))
            hit = institution_for_domain(url_domain(landing))
            if hit:
                tier = int(hit.get("tier", 2))
                source_name = str(hit.get("name")) or source_name
                kind = "institutional"
                type_label = "working paper"
                break

    if (
        tier is None
        and bool(CONFIG.get("accept_trusted_publisher_fallback", False))
        and trusted_publisher(host)
        and normalized(work.get("type")) in {"article", "review"}
    ):
        tier = 3
        type_label = "peer-reviewed article"

    if tier is None:
        diag("openalex_source_not_listed")
        return None

    link = clean_text(doi) or clean_text((work.get("primary_location") or {}).get("landing_page_url"))
    if link.startswith("10."):
        link = "https://doi.org/" + link

    return {
        "title": title,
        "authors": openalex_authors(work),
        "source": source_name or host or "Unknown source",
        "tier": tier,
        "kind": kind,
        "type": type_label,
        "date": parse_date(work.get("publication_date")),
        "link": link,
        "abstract": openalex_abstract(work.get("abstract_inverted_index")),
        "body": "",
        "discovered_via": f"openalex:{query}",
    }


def collect_openalex(queries: list[str], stage_deadline: float, depth_state: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    gate = RateGate(float(CONFIG.get("openalex_min_interval_seconds", 0.35)))
    per_page = int(CONFIG.get("openalex_per_query", 60))
    depth_max = max(1, int(CONFIG.get("openalex_depth_pages_max", 4)))
    timeout = int(CONFIG.get("scholarly_api_timeout_seconds", 12))
    workers = max(1, int(CONFIG.get("openalex_workers", 4)))
    stopped = threading.Event()
    executed: list[str] = []
    executed_lock = threading.Lock()
    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()

    from_date = (DATE_FLOOR - dt.timedelta(days=int(CONFIG.get("discovery_overlap_days", 21)))).isoformat()

    def run_query(query: str) -> None:
        if stopped.is_set() or stage_deadline_reached(stage_deadline, 5):
            return
        page = 1 + (int(depth_state.get(query, 0)) % depth_max)
        params = {
            "search": query,
            "filter": f"from_publication_date:{from_date}",
            "per-page": per_page,
            "page": page,
            "sort": "publication_date:desc",
        }
        if OPENALEX_MAILTO:
            params["mailto"] = OPENALEX_MAILTO
        gate.wait()
        try:
            response = get("https://api.openalex.org/works", timeout=timeout, params=params)
        except SourceStopped as exc:
            log(f"openalex stopped for this run: {exc}")
            stopped.set()
            return
        if response is None:
            return
        try:
            works = (response.json() or {}).get("results") or []
        except Exception:
            diag("openalex_bad_json")
            return

        with executed_lock:
            executed.append(query)
            depth_state[query] = int(depth_state.get(query, 0)) + 1

        local: list[dict[str, Any]] = []
        for work in works:
            candidate = openalex_candidate(work, query)
            if candidate:
                local.append(candidate)
        with results_lock:
            results.extend(local)
        diag("openalex_works_seen", len(works))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_query, q) for q in queries]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"openalex worker error: {type(exc).__name__}: {exc}")

    log(f"openalex: {len(executed)}/{len(queries)} queries ran, {len(results)} candidates")
    return results, executed


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


def crossref_date(item: dict[str, Any]) -> dt.date | None:
    for key in ("published-print", "published-online", "issued"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            year = int(parts[0])
            month = int(parts[1]) if len(parts) > 1 else 1
            day = int(parts[2]) if len(parts) > 2 else 1
            try:
                return dt.date(year, month, day)
            except Exception:
                continue
    return None


def crossref_authors(item: dict[str, Any]) -> str:
    names = []
    for author in (item.get("author") or [])[:8]:
        given = clean_text(author.get("given"))
        family = clean_text(author.get("family"))
        full = " ".join(x for x in (given, family) if x)
        if full:
            names.append(full)
    if len(item.get("author") or []) > 8:
        names.append("et al.")
    return ", ".join(names)


def crossref_candidate(item: dict[str, Any], via: str) -> dict[str, Any] | None:
    title = clean_text((item.get("title") or [""])[0])
    doi = clean_text(item.get("DOI"))
    if not title:
        return None
    if bool(CONFIG.get("skip_known_items_before_classification", True)):
        if stable_identity(title, doi) in KNOWN_IDENTITIES:
            diag("skip_known_crossref")
            return None

    container = clean_text((item.get("container-title") or [""])[0])
    publisher = clean_text(item.get("publisher"))
    tier, _label = journal_tier(container)
    if (
        tier is None
        and bool(CONFIG.get("accept_trusted_publisher_fallback", False))
        and trusted_publisher(publisher)
        and normalized(item.get("type")) in {"journal article", "posted content", "report"}
    ):
        tier = 3
    if tier is None:
        diag("crossref_source_not_listed")
        return None

    abstract = clean_text(item.get("abstract"))
    return {
        "title": title,
        "authors": crossref_authors(item),
        "source": container or publisher or "Unknown source",
        "tier": tier,
        "kind": "scholarly",
        "type": "peer-reviewed article",
        "date": crossref_date(item),
        "link": ("https://doi.org/" + doi) if doi else clean_text(item.get("URL")),
        "abstract": abstract,
        "body": "",
        "discovered_via": via,
    }


def resolve_journal_issns(
    names: list[str],
    cache: dict[str, str],
    stage_deadline: float,
) -> dict[str, str]:
    """Resolve journal names to ISSNs, caching the answer across runs.

    Crossref's container-title search is fuzzy: asking for the Journal of
    Organizational Behavior also returns the International Journal of
    Organizational Analysis and several other near-misses. Filtering by ISSN is
    exact, so journal-first harvesting actually reads the journal we asked for.
    ISSNs do not change, so a resolved name is looked up once and then reused.
    """
    resolved: dict[str, str] = {}
    timeout = int(CONFIG.get("scholarly_api_timeout_seconds", 12))
    gate = RateGate(float(CONFIG.get("crossref_min_interval_seconds", 0.45)))
    for name in names:
        key = normalized(name)
        if key in cache:
            if cache[key]:
                resolved[name] = cache[key]
            continue
        if stage_deadline_reached(stage_deadline, 10):
            break
        gate.wait()
        try:
            response = get("https://api.crossref.org/journals", timeout=timeout, params={"query": name, "rows": 5})
        except SourceStopped:
            break
        if response is None:
            continue
        try:
            entries = ((response.json() or {}).get("message") or {}).get("items") or []
        except Exception:
            continue
        issn = ""
        for entry in entries:
            if normalized(entry.get("title")) == key and (entry.get("ISSN") or []):
                issn = clean_text((entry.get("ISSN") or [""])[0])
                break
        cache[key] = issn
        if issn:
            resolved[name] = issn
            diag("journal_issn_resolved")
        else:
            diag("journal_issn_unresolved")
            log(f"could not resolve an ISSN for {name!r}; skipping journal-first pass for it")
    return resolved


def collect_crossref(
    queries: list[str],
    journals: list[str],
    stage_deadline: float,
    issn_cache: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    gate = RateGate(float(CONFIG.get("crossref_min_interval_seconds", 0.45)))
    timeout = int(CONFIG.get("scholarly_api_timeout_seconds", 12))
    workers = max(1, int(CONFIG.get("crossref_workers", 4)))
    rows_query = int(CONFIG.get("crossref_rows_per_query", 60))
    rows_journal = int(CONFIG.get("crossref_journal_rows", 40))
    stopped = threading.Event()
    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()
    ran_queries: list[str] = []
    ran_journals: list[str] = []
    exec_lock = threading.Lock()

    from_date = (DATE_FLOOR - dt.timedelta(days=int(CONFIG.get("discovery_overlap_days", 21)))).isoformat()
    base_filter = f"from-pub-date:{from_date},type:journal-article"

    def fetch(params: dict[str, Any], via: str, label: str, bucket: list[str]) -> None:
        if stopped.is_set() or stage_deadline_reached(stage_deadline, 5):
            return
        if CROSSREF_MAILTO:
            params["mailto"] = CROSSREF_MAILTO
        gate.wait()
        try:
            response = get("https://api.crossref.org/works", timeout=timeout, params=params)
        except SourceStopped as exc:
            log(f"crossref stopped for this run: {exc}")
            stopped.set()
            return
        if response is None:
            return
        try:
            items = ((response.json() or {}).get("message") or {}).get("items") or []
        except Exception:
            diag("crossref_bad_json")
            return
        with exec_lock:
            bucket.append(label)
        local = [c for c in (crossref_candidate(i, via) for i in items) if c]
        with results_lock:
            results.extend(local)
        diag("crossref_items_seen", len(items))

    tasks: list[tuple[dict[str, Any], str, str, list[str]]] = []
    for query in queries:
        tasks.append(
            (
                {"query.bibliographic": query, "rows": rows_query, "filter": base_filter, "sort": "published", "order": "desc"},
                f"crossref:{query}",
                query,
                ran_queries,
            )
        )
    issns = resolve_journal_issns(journals, issn_cache if issn_cache is not None else {}, stage_deadline)
    for journal in journals:
        issn = issns.get(journal)
        if not issn:
            continue
        tasks.append(
            (
                {
                    "query.bibliographic": "remote hybrid working from home telework office",
                    "rows": rows_journal,
                    "filter": f"{base_filter},issn:{issn}",
                    "sort": "published",
                    "order": "desc",
                },
                f"crossref-journal:{journal}",
                journal,
                ran_journals,
            )
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch, *task) for task in tasks]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"crossref worker error: {type(exc).__name__}: {exc}")

    log(
        f"crossref: {len(ran_queries)}/{len(queries)} queries and "
        f"{len(ran_journals)}/{len(journals)} journals ran, {len(results)} candidates"
    )
    return results, ran_queries, ran_journals


# ---------------------------------------------------------------------------
# Institutional sources
# ---------------------------------------------------------------------------


def page_published_date(soup: BeautifulSoup, fallback: dt.date | None) -> dt.date | None:
    selectors = [
        ("meta", {"property": "article:published_time"}, "content"),
        ("meta", {"name": "citation_publication_date"}, "content"),
        ("meta", {"name": "dcterms.date"}, "content"),
        ("meta", {"name": "date"}, "content"),
        ("meta", {"property": "og:updated_time"}, "content"),
        ("time", {}, "datetime"),
    ]
    for tag, attrs, attribute in selectors:
        node = soup.find(tag, attrs=attrs)
        if node:
            value = node.get(attribute) or node.get_text(" ", strip=True)
            parsed = parse_date(value)
            if parsed:
                return parsed
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or "{}")
        except Exception:
            continue
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            parsed = parse_date(entry.get("datePublished") or entry.get("dateModified"))
            if parsed:
                return parsed
    return fallback


def page_main_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form", "noscript"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    return clean_text(main.get_text(" ", strip=True))


def discover_institution_urls(src: dict[str, Any], stage_deadline: float) -> list[tuple[str, dt.date | None]]:
    """Find candidate article URLs for one institution.

    Sitemaps first because they carry lastmod dates, then the configured hub
    pages as a fallback for sites without a usable sitemap.
    """
    domain = str(src.get("domain"))
    base = f"https://{domain}"
    hints = [normalized(h) for h in src.get("path_hints", [])]
    found: dict[str, dt.date | None] = {}
    sitemap_timeout = int(CONFIG.get("sitemap_timeout_seconds", 10))
    max_entries = int(CONFIG.get("sitemap_max_entries", 900))

    def path_matches(url: str) -> bool:
        if url_domain(url) and not url_domain(url).endswith(domain.removeprefix("www.")):
            return False
        lowered = normalized(urlparse(url).path)
        return any(hint in lowered for hint in hints) if hints else True

    for candidate in CONFIG.get("sitemap_candidates", []):
        if stage_deadline_reached(stage_deadline, 3) or len(found) >= max_entries:
            break
        url = urljoin(base, candidate)
        try:
            response = get(url, timeout=sitemap_timeout)
        except SourceStopped:
            return []
        if response is None:
            continue
        text = response.text or ""
        if candidate.endswith("robots.txt"):
            for line in text.splitlines():
                if line.lower().startswith("sitemap:"):
                    nested = line.split(":", 1)[1].strip()
                    try:
                        nested_response = get(nested, timeout=sitemap_timeout)
                    except SourceStopped:
                        return []
                    if nested_response is not None:
                        text += "\n" + (nested_response.text or "")
            continue
        try:
            root = ElementTree.fromstring(text.encode("utf-8", "ignore"))
        except Exception:
            continue
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for node in root.findall(".//sm:url", ns) or []:
            loc = clean_text((node.find("sm:loc", ns).text if node.find("sm:loc", ns) is not None else ""))
            lastmod = parse_date(node.find("sm:lastmod", ns).text if node.find("sm:lastmod", ns) is not None else "")
            if loc and path_matches(loc):
                found.setdefault(loc, lastmod)
            if len(found) >= max_entries:
                break
        # A sitemap index points at child sitemaps; follow a couple of them.
        children = [clean_text(n.text) for n in root.findall(".//sm:sitemap/sm:loc", ns) or []][:3]
        for child in children:
            if stage_deadline_reached(stage_deadline, 3) or len(found) >= max_entries:
                break
            try:
                child_response = get(child, timeout=sitemap_timeout)
            except SourceStopped:
                return []
            if child_response is None:
                continue
            try:
                child_root = ElementTree.fromstring((child_response.text or "").encode("utf-8", "ignore"))
            except Exception:
                continue
            for node in child_root.findall(".//sm:url", ns) or []:
                loc_node = node.find("sm:loc", ns)
                loc = clean_text(loc_node.text if loc_node is not None else "")
                lastmod_node = node.find("sm:lastmod", ns)
                lastmod = parse_date(lastmod_node.text if lastmod_node is not None else "")
                if loc and path_matches(loc):
                    found.setdefault(loc, lastmod)
                if len(found) >= max_entries:
                    break

    if len(found) < 5:
        for hub in src.get("hub_paths", []):
            if stage_deadline_reached(stage_deadline, 3):
                break
            try:
                response = get(urljoin(base, hub), timeout=int(CONFIG.get("institution_page_timeout_seconds", 12)))
            except SourceStopped:
                return []
            if response is None:
                continue
            soup = BeautifulSoup(response.text or "", "html.parser")
            for anchor in soup.find_all("a", href=True):
                url = urljoin(base, anchor["href"])
                if url.startswith("http") and path_matches(url):
                    found.setdefault(url, None)

    ordered = [
        (url, lastmod)
        for url, lastmod in found.items()
        if normalized_link(url) not in SEEN_INSTITUTION_URLS and normalized_link(url) not in KNOWN_LINKS
    ]
    ordered.sort(key=lambda pair: (pair[1] or dt.date(1970, 1, 1)), reverse=True)
    return ordered[: int(CONFIG.get("institution_pages_per_domain", 14))]


def fetch_institution_page(url: str, lastmod: dt.date | None, src: dict[str, Any]) -> dict[str, Any] | None:
    try:
        response = get(url, timeout=int(CONFIG.get("institution_page_timeout_seconds", 12)))
    except SourceStopped:
        return None
    if response is None:
        return None
    content_type = (response.headers.get("Content-Type") or "").lower()
    if "html" not in content_type:
        return None
    soup = BeautifulSoup(response.text or "", "html.parser")
    title_node = soup.find("meta", attrs={"property": "og:title"}) or soup.find("title")
    title = clean_text(title_node.get("content") if title_node and title_node.has_attr("content") else (title_node.get_text() if title_node else ""))
    body = page_main_text(soup)
    published = page_published_date(soup, lastmod)
    description_node = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    description = clean_text(description_node.get("content") if description_node else "")

    return {
        "title": title,
        "authors": str(src.get("name")),
        "source": str(src.get("name")),
        "tier": int(src.get("tier", 2)),
        "kind": "institutional",
        "type": "institutional report",
        "date": published,
        "link": url,
        "abstract": description or body[:1200],
        "body": body,
        "discovered_via": f"institution:{src.get('domain')}",
    }


def collect_institutions(sources: list[dict[str, Any]], stage_deadline: float) -> tuple[list[dict[str, Any]], list[str]]:
    discovery_workers = max(1, int(CONFIG.get("institution_discovery_workers", 8)))
    page_workers = max(1, int(CONFIG.get("institution_page_workers", 12)))
    results: list[dict[str, Any]] = []
    results_lock = threading.Lock()
    executed: list[str] = []
    exec_lock = threading.Lock()

    discovered: list[tuple[dict[str, Any], str, dt.date | None]] = []
    discovered_lock = threading.Lock()

    def discover(src: dict[str, Any]) -> None:
        if stage_deadline_reached(stage_deadline, 20):
            return
        urls = discover_institution_urls(src, stage_deadline)
        if urls:
            with exec_lock:
                executed.append(str(src.get("domain")))
        with discovered_lock:
            for url, lastmod in urls:
                discovered.append((src, url, lastmod))
        log(f"institution {src.get('domain')}: {len(urls)} candidate pages")

    with ThreadPoolExecutor(max_workers=discovery_workers) as pool:
        list(as_completed([pool.submit(discover, s) for s in sources]))

    def fetch_one(task: tuple[dict[str, Any], str, dt.date | None]) -> None:
        src, url, lastmod = task
        if stage_deadline_reached(stage_deadline, 8):
            return
        candidate = fetch_institution_page(url, lastmod, src)
        SEEN_INSTITUTION_URLS[normalized_link(url)] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        if candidate:
            with results_lock:
                results.append(candidate)

    with ThreadPoolExecutor(max_workers=page_workers) as pool:
        futures = [pool.submit(fetch_one, task) for task in discovered]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                log(f"institution worker error: {type(exc).__name__}: {exc}")

    log(f"institutions: {len(executed)}/{len(sources)} sources reached, {len(results)} pages fetched")
    return results, executed


# ---------------------------------------------------------------------------
# Abstract enrichment
# ---------------------------------------------------------------------------


def rescue_priority(candidate: dict[str, Any]) -> int:
    """Rank abstract-less records before spending the bounded recovery budget.

    Missing text is not negative evidence about an item, it is missing metadata.
    Most publishers simply do not deposit abstracts with Crossref. Discarding
    those records would silently remove a large share of exactly the journals
    this radar exists to watch, so the promising ones get a recovery attempt and
    the rest are dropped only after that.
    """
    title = clean_text(candidate.get("title"))
    score = 0
    if distinct_matches(title, CONFIG.get("location_of_work_terms", [])):
        score += 6
    if distinct_matches(title, CONFIG.get("work_terms", [])):
        score += 3
    if distinct_matches(title, CONFIG.get("effectiveness_terms", [])):
        score += 3
    tier = int(candidate.get("tier", 9))
    score += 4 if tier <= 1 else 2 if tier == 2 else 0
    return score


def _abstract_from_page(url: str, timeout: int) -> str:
    try:
        response = get(url, timeout=timeout, retries=0)
    except SourceStopped:
        return ""
    if response is None or "html" not in (response.headers.get("Content-Type") or "").lower():
        return ""
    soup = BeautifulSoup(response.text or "", "html.parser")
    for tag, attrs in (
        ("meta", {"name": "citation_abstract"}),
        ("meta", {"name": "dcterms.abstract"}),
        ("meta", {"property": "og:description"}),
        ("meta", {"name": "description"}),
    ):
        node = soup.find(tag, attrs=attrs)
        if node and node.get("content"):
            text = clean_text(node["content"])
            if word_count(text) >= 25:
                return text
    for selector in ("section.abstract", "div.abstract", "#abstract", ".abstractSection", ".hlFld-Abstract"):
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" ", strip=True))
            if word_count(text) >= 25:
                return text
    return ""


def enrich_missing_abstracts(candidates: list[dict[str, Any]], stage_deadline: float) -> int:
    """Recover abstracts for the most promising abstract-less scholarly records."""
    minimum = int(CONFIG.get("min_abstract_words", 30))
    queue = [
        c
        for c in candidates
        if c.get("kind") == "scholarly" and word_count(c.get("abstract", "")) < minimum and clean_text(c.get("link"))
    ]
    if not queue:
        return 0
    queue.sort(key=rescue_priority, reverse=True)
    threshold = int(CONFIG.get("rescue_priority_min_score", 8))
    queue = [c for c in queue if rescue_priority(c) >= threshold][: int(CONFIG.get("abstract_enrichment_per_scan", 90))]
    diag("abstract_rescue_queued", len(queue))
    if not queue:
        return 0
    log(f"abstract enrichment: {len(queue)} records queued for recovery")

    recovered = 0
    by_doi: dict[str, dict[str, Any]] = {}
    for candidate in queue:
        match = re.search(r"10\.\d{4,9}/[^\s?#]+", str(candidate.get("link", "")))
        if match:
            by_doi[match.group(0).rstrip(".,)").lower()] = candidate

    # Pass one: OpenAlex in DOI batches. One request covers fifty records.
    dois = list(by_doi)
    batch_size = 50
    for start in range(0, len(dois), batch_size):
        if stage_deadline_reached(stage_deadline, 5):
            break
        batch = dois[start : start + batch_size]
        params = {"filter": "doi:" + "|".join(batch), "per-page": batch_size}
        if OPENALEX_MAILTO:
            params["mailto"] = OPENALEX_MAILTO
        try:
            response = get("https://api.openalex.org/works", timeout=15, params=params, retries=1)
        except SourceStopped:
            log("abstract enrichment: OpenAlex unavailable, falling back to landing pages")
            break
        if response is None:
            continue
        try:
            works = (response.json() or {}).get("results") or []
        except Exception:
            continue
        for work in works:
            doi = clean_text(work.get("doi")).lower()
            key = re.search(r"10\.\d{4,9}/[^\s?#]+", doi)
            target = by_doi.get(key.group(0).rstrip(".,)")) if key else None
            if not target:
                continue
            abstract = openalex_abstract(work.get("abstract_inverted_index"))
            if word_count(abstract) >= minimum:
                target["abstract"] = abstract
                recovered += 1
                diag("abstract_rescued_openalex")

    # Pass two: bounded landing-page fallback for whatever is still missing.
    page_budget = int(CONFIG.get("abstract_enrichment_page_fetches", 30))
    still_missing = [c for c in queue if word_count(c.get("abstract", "")) < minimum][:page_budget]
    if still_missing and not stage_deadline_reached(stage_deadline, 5):
        timeout = int(CONFIG.get("abstract_enrichment_timeout_seconds", 8))

        def recover(candidate: dict[str, Any]) -> bool:
            if stage_deadline_reached(stage_deadline, 3):
                return False
            text = _abstract_from_page(clean_text(candidate.get("link")), timeout)
            if word_count(text) >= minimum:
                candidate["abstract"] = text
                return True
            return False

        with ThreadPoolExecutor(max_workers=max(1, int(CONFIG.get("abstract_enrichment_workers", 8)))) as pool:
            for future in as_completed([pool.submit(recover, c) for c in still_missing]):
                try:
                    if future.result():
                        recovered += 1
                        diag("abstract_rescued_page")
                except Exception:
                    diag("abstract_rescue_error")

    log(f"abstract enrichment: recovered {recovered} abstracts")
    return recovered


# ---------------------------------------------------------------------------
# Corpus merge
# ---------------------------------------------------------------------------


def load_previous() -> dict[str, Any]:
    if not DATA_PATH.is_file():
        return {}
    try:
        data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        log(f"could not read existing work_radar.json ({type(exc).__name__}); starting a new corpus")
        return {}


def prime_known_sets(previous: dict[str, Any]) -> None:
    for item in previous.get("items", []) or []:
        if not isinstance(item, dict):
            continue
        KNOWN_IDENTITIES.add(stable_identity(item.get("title", ""), item.get("link", "")))
        link = normalized_link(item.get("link", ""))
        if link:
            KNOWN_LINKS.add(link)
    cache = previous.get("institution_seen_cache")
    if isinstance(cache, dict):
        SEEN_INSTITUTION_URLS.update({str(k): str(v) for k, v in cache.items()})


def merge_corpus(previous_items: list[dict[str, Any]], fresh: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Cumulative merge, newest metadata wins, retention window applied.

    Tier 1 sources are held for the extended window; everything else ages out at
    the standard floor. Nothing is dropped merely because a scan did not find it
    again.
    """
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    def key_for(item: dict[str, Any]) -> str:
        link = normalized_link(item.get("link", ""))
        return link or stable_identity(item.get("title", ""), "")

    for item in previous_items:
        if not isinstance(item, dict):
            continue
        item = dict(item)
        item["new_this_scan"] = False
        key = key_for(item)
        if key not in by_key:
            order.append(key)
        by_key[key] = item

    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    new_count = 0
    for item in fresh:
        key = key_for(item)
        if key in by_key:
            existing = by_key[key]
            first_seen = existing.get("first_seen", now_iso)
            merged = dict(existing)
            merged.update(item)
            merged["first_seen"] = first_seen
            merged["new_this_scan"] = False
            by_key[key] = merged
            continue
        item = dict(item)
        item["first_seen"] = now_iso
        item["new_this_scan"] = True
        by_key[key] = item
        order.append(key)
        new_count += 1

    kept: list[dict[str, Any]] = []
    for key in order:
        item = by_key.get(key)
        if not item:
            continue
        published = parse_date(item.get("date"))
        tier_number = 9
        match = re.search(r"\d", str(item.get("source_tier", "")))
        if match:
            tier_number = int(match.group(0))
        floor = EXTENDED_FLOOR if tier_number <= 1 else DATE_FLOOR
        if published is None or published >= floor:
            kept.append(item)

    kept.sort(key=lambda x: (str(x.get("date", "")), str(x.get("title", ""))), reverse=True)
    cap = int(CONFIG.get("max_corpus_items", 0))
    if cap > 0:
        kept = kept[:cap]
    return kept, new_count


def matrix_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = {cell: 0 for cell in CONFIG.get("matrix_cells", {})}
    counts["unplaced"] = 0
    for item in items:
        cell = str(item.get("matrix_cell") or "")
        if cell in counts:
            counts[cell] += 1
        else:
            counts["unplaced"] += 1
    return counts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    global SCAN_DEADLINE_MONO

    started = dt.datetime.now(dt.timezone.utc)
    budget = int(CONFIG.get("scan_budget_seconds", 600))
    override = os.environ.get("WORK_RADAR_BUDGET_SECONDS", "").strip()
    if override:
        try:
            budget = max(60, min(1800, int(override)))
        except ValueError:
            log(f"ignoring unreadable budget override {override!r}")
    reserve = int(CONFIG.get("scan_finalize_reserve_seconds", 45))
    SCAN_DEADLINE_MONO = time.monotonic() + budget
    log(f"scan starting with a {budget}s budget ({budget / 60:.0f} minutes)")

    previous = load_previous()
    prime_known_sets(previous)
    state = dict(previous.get("scan_state") or {})
    previous_items = list(previous.get("items") or [])
    log(f"loaded {len(previous_items)} items from the existing corpus")

    warnings: list[str] = []
    all_queries = list(CONFIG.get("queries_core", [])) + list(CONFIG.get("queries_frontier", []))
    depth_state = dict(state.get("openalex_depth") or {})

    # --- OpenAlex -----------------------------------------------------------
    oa_cursor = int(state.get("openalex_cursor", 0))
    oa_queries, oa_next, _ = rotating_batch(all_queries, oa_cursor, int(CONFIG.get("openalex_queries_per_scan", 14)))
    oa_deadline = new_stage_deadline(int(CONFIG.get("openalex_stage_seconds", 150)), reserve)
    oa_candidates, oa_executed = collect_openalex(oa_queries, oa_deadline, depth_state)
    commit_cursor(state, "openalex_cursor", oa_cursor, oa_next, len(oa_executed))
    if not oa_executed and oa_queries:
        warnings.append("OpenAlex returned nothing this run")

    # --- Crossref -----------------------------------------------------------
    cr_cursor = int(state.get("crossref_cursor", 0))
    cr_queries, cr_next, _ = rotating_batch(all_queries, cr_cursor, int(CONFIG.get("crossref_queries_per_scan", 12)))
    cj_cursor = int(state.get("crossref_journal_cursor", 0))
    cr_journals, cj_next, _ = rotating_batch(
        list(CONFIG.get("crossref_priority_journals", [])), cj_cursor, int(CONFIG.get("crossref_journals_per_scan", 10))
    )
    cr_deadline = new_stage_deadline(int(CONFIG.get("crossref_stage_seconds", 165)), reserve)
    issn_cache = dict(state.get("journal_issn") or {})
    cr_candidates, cr_ran, cj_ran = collect_crossref(cr_queries, cr_journals, cr_deadline, issn_cache)
    state["journal_issn"] = issn_cache
    commit_cursor(state, "crossref_cursor", cr_cursor, cr_next, len(cr_ran))
    commit_cursor(state, "crossref_journal_cursor", cj_cursor, cj_next, len(cj_ran))
    if not cr_ran and not cj_ran:
        warnings.append("Crossref returned nothing this run")

    # --- Institutions -------------------------------------------------------
    inst_cursor = int(state.get("institution_cursor", 0))
    inst_sources, inst_next, _ = rotating_batch(
        list(CONFIG.get("institution_sources", [])), inst_cursor, int(CONFIG.get("institution_sources_per_scan", 9))
    )
    inst_deadline = new_stage_deadline(int(CONFIG.get("institution_stage_seconds", 195)), reserve)
    inst_candidates, inst_executed = collect_institutions(inst_sources, inst_deadline)
    commit_cursor(state, "institution_cursor", inst_cursor, inst_next, len(inst_executed))
    if not inst_executed and inst_sources:
        warnings.append("No institutional source could be reached this run")

    candidates = oa_candidates + cr_candidates + inst_candidates
    log(f"{len(candidates)} raw candidates before the admission gate")

    # --- Abstract enrichment ------------------------------------------------
    enrich_deadline = new_stage_deadline(int(CONFIG.get("abstract_enrichment_stage_seconds", 75)), reserve)
    recovered = enrich_missing_abstracts(candidates, enrich_deadline)

    # --- Admission ----------------------------------------------------------
    fresh: list[dict[str, Any]] = []
    seen_this_run: set[str] = set()
    for candidate in candidates:
        identity = stable_identity(candidate.get("title", ""), candidate.get("link", ""))
        if identity in seen_this_run:
            continue
        seen_this_run.add(identity)
        item = admit(candidate)
        if item:
            fresh.append(item)

    # --- Low-yield rescue ---------------------------------------------------
    rescue_ran = False
    trigger = int(CONFIG.get("low_yield_rescue_trigger_max_new", 4))
    min_remaining = int(CONFIG.get("low_yield_rescue_min_seconds_remaining", 120))
    if len(fresh) <= trigger and budget_remaining() > min_remaining:
        log(f"low yield ({len(fresh)} new); running one rescue pass on fresh queries")
        rescue_cursor = int(state.get("rescue_cursor", oa_next))
        rescue_queries, rescue_next, _ = rotating_batch(all_queries, rescue_cursor, 8)
        rescue_deadline = new_stage_deadline(int(CONFIG.get("low_yield_rescue_stage_seconds", 90)), reserve)
        rescue_candidates, rescue_executed = collect_openalex(rescue_queries, rescue_deadline, depth_state)
        commit_cursor(state, "rescue_cursor", rescue_cursor, rescue_next, len(rescue_executed))
        rescue_ran = bool(rescue_executed)
        for candidate in rescue_candidates:
            identity = stable_identity(candidate.get("title", ""), candidate.get("link", ""))
            if identity in seen_this_run:
                continue
            seen_this_run.add(identity)
            item = admit(candidate)
            if item:
                fresh.append(item)

    overrides = load_manual_placements()
    if overrides:
        fresh = [apply_manual_placement(i, overrides) for i in fresh]

    log(f"{len(fresh)} items passed the admission gate")

    # --- Merge and write ----------------------------------------------------
    items, new_count = merge_corpus(previous_items, fresh)
    if overrides:
        items = [apply_manual_placement(i, overrides) for i in items]
    counts = matrix_counts(items)
    completed = dt.datetime.now(dt.timezone.utc)

    state["openalex_depth"] = {k: v for k, v in list(depth_state.items())[:400]}
    cache_max = int(CONFIG.get("institution_seen_cache_max", 4000))
    trimmed_cache = dict(sorted(SEEN_INSTITUTION_URLS.items(), key=lambda kv: kv[1], reverse=True)[:cache_max])
    state["last_completed_at"] = completed.strftime("%Y-%m-%dT%H:%MZ")

    history = list(previous.get("scan_history") or [])[-19:]
    history.append(
        {
            "started_at": started.strftime("%Y-%m-%dT%H:%MZ"),
            "completed_at": completed.strftime("%Y-%m-%dT%H:%MZ"),
            "seconds": round((completed - started).total_seconds(), 1),
            "new_items": new_count,
            "corpus_size": len(items),
            "rescue_ran": rescue_ran,
        }
    )

    document = {
        "topic": CONFIG.get("topic"),
        "config_version": CONFIG.get("config_version"),
        "last_updated": completed.strftime("%Y-%m-%dT%H:%MZ"),
        "run_started_at": started.strftime("%Y-%m-%dT%H:%MZ"),
        "run_completed_at": completed.strftime("%Y-%m-%dT%H:%MZ"),
        "run_seconds": round((completed - started).total_seconds(), 1),
        "scan_budget_seconds": budget,
        "trigger": os.environ.get("WORK_RADAR_TRIGGER", "manual"),
        "corpus_start_date": DATE_FLOOR.isoformat(),
        "extended_corpus_start_date": EXTENDED_FLOOR.isoformat(),
        "admission_policy": CONFIG.get("admission_policy"),
        "matrix_policy": CONFIG.get("matrix_policy"),
        "matrix_axes": CONFIG.get("matrix_axes"),
        "matrix_cells": CONFIG.get("matrix_cells"),
        "matrix_counts": counts,
        "stats": {
            "corpus_size": len(items),
            "new_this_scan": new_count,
            "placed": sum(v for k, v in counts.items() if k != "unplaced"),
            "unplaced": counts.get("unplaced", 0),
            "raw_candidates": len(candidates),
            "abstracts_recovered": recovered,
            "admitted_this_run": len(fresh),
            "openalex_queries_run": len(oa_executed),
            "crossref_queries_run": len(cr_ran),
            "crossref_journals_run": len(cj_ran),
            "institutions_reached": len(inst_executed),
            "rescue_ran": rescue_ran,
        },
        "diagnostics": dict(sorted(DIAGNOSTICS.items())),
        "warnings": warnings,
        "scan_state": state,
        "scan_history": history,
        "institution_seen_cache": trimmed_cache,
        "items": items,
    }

    DATA_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")
    log(
        f"wrote {len(items)} items ({new_count} new); "
        f"placed={document['stats']['placed']} unplaced={document['stats']['unplaced']}"
    )
    log(f"matrix: {counts}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
