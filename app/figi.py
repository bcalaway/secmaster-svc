"""FIGIs for Treasury securities and STRIPS from OpenFIGI (mkt-data's docs/phase-3.md, step 3d), and the
mapping client the futures lookups share (app/futures_figi.py, docs/phase-4.md step 2b).

OpenFIGI's mapping API (https://api.openfigi.com/v3/mapping) turns a CUSIP
into its FIGI, composite FIGI and Bloomberg-style ticker ("T 4 1/4 08/15/35").
Each CUSIP is asked once: `figi_lookup` remembers the answer, and a CUSIP
OpenFIGI didn't know is asked again after RETRY_AFTER (it may have been added).
A CUSIP OpenFIGI answered with an error (anything but data or "No identifier
found") is asked again after RETRY_ERROR_AFTER, since an error may be passing.

Limits (OpenFIGI's documentation, checked 2026-10-07): with an API key, 25
requests per 6 seconds of up to 100 CUSIPs each; without, 25 per minute of up
to 10. The key comes from SSM (/home-platform/secmaster-svc/openfigi-api-key,
OPENFIGI_API_KEY in the container); without it the job still runs, slowly,
and does at most MAX_WITHOUT_KEY CUSIPs a run.
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx2

URL = "https://api.openfigi.com/v3/mapping"
TIMEOUT_SECONDS = 60
RETRY_AFTER = timedelta(days=30)
RETRY_ERROR_AFTER = timedelta(days=1)
MAX_WITHOUT_KEY = 250
USER_AGENT = "secmaster-svc/1 (personal market data platform; bcalaway)"


@dataclass(frozen=True)
class Limits:
    batch: int  # CUSIPs per request
    requests: int  # requests per window
    window: float  # seconds


WITH_KEY = Limits(100, 25, 6.0)
WITHOUT_KEY = Limits(10, 25, 60.0)


class FigiError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answer:
    cusip: str
    outcome: str  # found | not_found | error
    figi: str | None = None
    composite_figi: str | None = None
    ticker: str | None = None
    detail: dict | None = None  # OpenFIGI's answer for this CUSIP, as returned


def _pick(rows: list[dict]) -> dict:
    """OpenFIGI can return a few listings for one CUSIP; prefer the government-bond one."""
    return next((r for r in rows if r.get("marketSector") == "Govt"), rows[0])


def parse(cusips: list[str], body: list) -> list[Answer]:
    """One request's answer: a list in the order of the CUSIPs asked."""
    if not isinstance(body, list) or len(body) != len(cusips):
        raise FigiError(f"expected {len(cusips)} answers, got {str(body)[:200]}")
    out = []
    for cusip, item in zip(cusips, body, strict=True):
        if isinstance(item, dict) and item.get("data"):
            r = _pick(item["data"])
            out.append(Answer(cusip, "found", r.get("figi"), r.get("compositeFIGI"), r.get("ticker"), item))
        elif isinstance(item, dict) and "No identifier found" in str(item.get("warning", "")):
            out.append(Answer(cusip, "not_found", detail=item))
        else:
            out.append(Answer(cusip, "error", detail=item if isinstance(item, dict) else {"answer": item}))
    return out


def map_jobs(jobs: list[dict], api_key: str | None, post=None, sleep=time.sleep, clock=time.monotonic) -> list:
    """Send OpenFIGI mapping jobs (`{"idType": ..., "idValue": ..., filters}`), in batches, within the rate
    limit; OpenFIGI's answer to each, in order. Raises FigiError if it can't be reached or answers badly."""
    post = post or httpx2.post
    limits = WITH_KEY if api_key else WITHOUT_KEY
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if api_key:
        headers["X-OPENFIGI-APIKEY"] = api_key
    sent: list[float] = []
    out: list = []
    for i in range(0, len(jobs), limits.batch):
        batch = jobs[i:i + limits.batch]
        for attempt in range(3):
            now = clock()
            sent = [t for t in sent if now - t < limits.window]
            if len(sent) >= limits.requests:
                sleep(limits.window - (now - sent[0]) + 0.1)
            sent.append(clock())
            try:
                r = post(URL, json=batch, headers=headers, timeout=TIMEOUT_SECONDS)
            except httpx2.HTTPError as e:
                raise FigiError(f"OpenFIGI unreachable: {e}") from None
            if r.status_code == 429 and attempt < 2:
                sleep(float(r.headers.get("ratelimit-reset", limits.window)) + 1)
                continue
            if r.status_code != 200:
                raise FigiError(f"OpenFIGI answered HTTP {r.status_code}: {r.text[:200]}")
            body = r.json()
            if not isinstance(body, list) or len(body) != len(batch):
                raise FigiError(f"expected {len(batch)} answers, got {str(body)[:200]}")
            out.extend(body)
            break
    return out


def map_cusips(cusips: list[str], api_key: str | None, post=None, sleep=time.sleep, clock=time.monotonic) -> list[Answer]:
    """Ask OpenFIGI about each CUSIP, in batches, within the rate limit. Raises FigiError if it can't be reached."""
    body = map_jobs([{"idType": "ID_CUSIP", "idValue": c} for c in cusips], api_key, post, sleep, clock)
    return parse(cusips, body)


def due(last: datetime | None, outcome: str | None, now: datetime) -> bool:
    """Whether a CUSIP should be asked (again): never asked, or not found / failed long enough ago."""
    if last is None:
        return True
    if last.tzinfo is None:  # SQLite (tests) hands back naive UTC times
        last = last.replace(tzinfo=UTC)
    if outcome == "found":
        return False
    return now - last >= (RETRY_ERROR_AFTER if outcome == "error" else RETRY_AFTER)
