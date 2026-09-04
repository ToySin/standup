"""Read approved leave from the HR tool's Slack feed.

flex's Slack integration posts every approved leave, and every cancellation, to
one company-wide channel. It is admin-configured, so it covers everyone with no
per-member opt-in, but it identifies people only by the display name registered
in flex and it collapses every leave reason to 휴가.

Two consequences shape this module:

Names are matched **exactly** (whitespace-normalized, case-folded) against a
configured table. Never approximately. A real feed turned out to contain two
different employees whose names differed by a single letter, and attributing
one's leave to the other is a worse failure than missing it outright, because
nobody notices it is wrong.

The feed is transactional and not ordered by leave date: a cancellation can
arrive before the grant it cancels, and leave can be re-requested after being
cancelled. Events are therefore replayed in timestamp order and the last event
for a given (person, range) wins.
"""

import datetime as dt
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

SLACK_API = "https://slack.com/api"

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE = 2  # seconds, doubled per attempt

MESSAGE = re.compile(r"^:(?P<icon>[\w-]+):\s*\[(?P<name>.+?)\]\s*-\s*(?P<body>.+)$")
KO_DATE = re.compile(r"(\d{1,2})월\s*(\d{1,2})일")
EN_MONTHS = (
    "January February March April May June July August September "
    "October November December"
).split()
EN_DATE = re.compile(r"\b(" + "|".join(EN_MONTHS) + r")\s+(\d{1,2})\b")
KO_TIME = re.compile(r"(오전|오후)\s*(\d{1,2}):(\d{2})")
EN_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)

CANCEL_MARKERS = ("취소", "cancel")
ALL_DAY_MARKERS = ("하루종일", "all day")


class SlackError(RuntimeError):
    pass


def _call(token, method, params=None, body=None):
    """One Slack API call, retrying transient failures.

    This runs unattended from cron, where a single timed-out socket or a
    rate-limit reply should not end the run. Slack asks callers to honour
    Retry-After on 429, so that value is used verbatim rather than guessed at.
    """
    url = f"{SLACK_API}/{method}"
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json; charset=utf-8"
    elif params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)

    last = None
    for attempt in range(MAX_ATTEMPTS):
        if attempt:
            time.sleep(BACKOFF_BASE * (2 ** (attempt - 1)))
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(int(exc.headers.get("Retry-After", 1)) + 1)
                last = "rate limited"
                continue
            if 500 <= exc.code < 600:
                last = f"HTTP {exc.code}"
                continue
            raise SlackError(f"{method}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = getattr(exc, "reason", exc) or "connection failed"
            continue

        if payload.get("ok"):
            return payload
        error = payload.get("error", "unknown error")
        # Slack also signals throttling in the body on some endpoints.
        if error == "ratelimited":
            time.sleep(BACKOFF_BASE * (2**attempt))
            last = error
            continue
        raise SlackError(f"{method}: {error}")

    raise SlackError(f"{method}: giving up after {MAX_ATTEMPTS} attempts ({last})")


def normalize(name):
    """Collapse whitespace (the feed uses narrow no-break spaces) and casefold."""
    return " ".join(name.replace(" ", " ").replace("\xa0", " ").split()).casefold()


def _to_24h(meridiem, hour, minute):
    hour = int(hour) % 12
    if meridiem.lower() in ("오후", "pm"):
        hour += 12
    return f"{hour:02d}:{minute}"


def parse_message(text, posted_on):
    """Parse one bot message into a leave event, or None if it is not one."""
    match = MESSAGE.match(text.strip())
    if not match:
        return None
    body = match["body"]

    months_days = [(int(m), int(d)) for m, d in KO_DATE.findall(body)]
    months_days += [
        (EN_MONTHS.index(month) + 1, int(day)) for month, day in EN_DATE.findall(body)
    ]
    if not months_days:
        return None

    def resolve(month, day):
        # The feed carries no year. Anchor to the posting date: leave is
        # announced ahead of time, so a month far behind the posting month
        # belongs to next year, and one far ahead to last year.
        year = posted_on.year
        if month < posted_on.month - 6:
            year += 1
        elif month > posted_on.month + 6:
            year -= 1
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None

    start = resolve(*months_days[0])
    end = resolve(*months_days[-1]) if len(months_days) > 1 else start
    if not start or not end or end < start:
        return None

    times = [_to_24h(a, b, c) for a, b, c in KO_TIME.findall(body)]
    times += [_to_24h(c, a, b) for a, b, c in EN_TIME.findall(body)]
    all_day = (
        any(marker in body.lower() for marker in ALL_DAY_MARKERS)
        or (len(months_days) > 1 and not times)
        or not times
    )

    lowered = body.lower()
    return {
        "flexName": match["name"].strip(),
        "kind": "cancel"
        if any(marker in lowered for marker in CANCEL_MARKERS)
        else "grant",
        "dateFrom": start.isoformat(),
        "dateTo": end.isoformat(),
        "span": "종일" if all_day else "-".join(times[:2]),
    }


def fetch_messages(token, channel, oldest=None, limit_pages=50):
    """Yield raw messages from the channel, newest first, following pagination."""
    cursor, pages = None, 0
    while pages < limit_pages:
        params = {"channel": channel, "limit": 200}
        if oldest:
            params["oldest"] = oldest
        if cursor:
            params["cursor"] = cursor
        payload = _call(token, "conversations.history", params)

        yield from payload.get("messages") or []
        pages += 1

        meta = payload.get("response_metadata") or {}
        cursor = meta.get("next_cursor")
        if not payload.get("has_more") or not cursor:
            return


def channel_members(token, channel):
    """User IDs in the channel, following pagination."""
    members, cursor = [], None
    while True:
        params = {"channel": channel, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        payload = _call(token, "conversations.members", params)
        members.extend(payload.get("members") or [])
        cursor = (payload.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return members


def user_info(token, user_id):
    """Display name and email for one user, or None for bots and deleted users."""
    payload = _call(token, "users.info", {"user": user_id})
    user = payload.get("user") or {}
    if user.get("is_bot") or user.get("deleted"):
        return None
    profile = user.get("profile") or {}
    return {
        "id": user_id,
        "name": profile.get("real_name") or user.get("real_name") or user.get("name"),
        "email": (profile.get("email") or "").lower(),
    }


def post_message(token, channel, text):
    return _call(token, "chat.postMessage", body={"channel": channel, "text": text})
