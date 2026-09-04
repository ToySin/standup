#!/usr/bin/env python3
"""Daily team schedule digest, sourced from the HR tool's Slack feed.

flex's Slack integration posts every approved leave and every cancellation to
one company-wide channel. Messages are ingested into a local SQLite store and
replayed in timestamp order, because leave is announced months ahead, a
cancellation can arrive before the grant it cancels, and leave can be
re-requested after being cancelled.

People are identified by the display name flex holds, matched **exactly**
against the table in config.local.yaml. See `names` for maintaining it.

Environment:
    SLACK_BOT_TOKEN     needs channels:history on the feed channel, groups:read
                        and chat:write on the destination channel, and
                        users:read plus users:read.email
    STANDUP_CONFIG      config path, default config.yaml beside this file
    STANDUP_STATE_DIR   where the caches go, default beside this file

Usage:
    python3 main.py doctor
    python3 main.py ingest [--full]
    python3 main.py preview [--date YYYY-MM-DD]
    python3 main.py post    [--date YYYY-MM-DD]
    python3 main.py names   [--days 120]
"""

import argparse
import datetime as dt
import os
import json
import sqlite3
import sys
import zoneinfo
from pathlib import Path

import yaml

import slack_source
from slack_source import SlackError, normalize, parse_message

HERE = Path(__file__).parent
CONFIG_PATH = Path(os.environ.get("STANDUP_CONFIG", HERE / "config.yaml"))
# Channel IDs and the name table are deployment-specific, and the name table is
# personal data, so they live in a gitignored file merged over the shared one.
LOCAL_CONFIG_PATH = CONFIG_PATH.with_name(
    CONFIG_PATH.stem + ".local" + CONFIG_PATH.suffix
)

# Both of these are caches, not sources of truth. A fresh run rebuilds them:
# the event store from a full channel read (about 30 seconds for 10k messages)
# and the roster from channel membership. So a stateless deployment can point
# STANDUP_STATE_DIR at ephemeral storage and lose nothing.
STATE_DIR = Path(os.environ.get("STANDUP_STATE_DIR", HERE))
DB_PATH = STATE_DIR / "events.db"
ROSTER_CACHE = STATE_DIR / "roster_cache.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    ts        TEXT PRIMARY KEY,
    flex_name TEXT NOT NULL,
    kind      TEXT NOT NULL,
    date_from TEXT NOT NULL,
    date_to   TEXT NOT NULL,
    span      TEXT NOT NULL,
    raw       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_range ON events (date_from, date_to);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def load_config():
    with CONFIG_PATH.open() as f:
        cfg = yaml.safe_load(f) or {}

    if not LOCAL_CONFIG_PATH.exists():
        sys.exit(
            f"{LOCAL_CONFIG_PATH.name} is missing.\n"
            f"Copy {LOCAL_CONFIG_PATH.stem}.example{LOCAL_CONFIG_PATH.suffix} "
            "to it and fill in the channel IDs and name table."
        )
    with LOCAL_CONFIG_PATH.open() as f:
        local = yaml.safe_load(f) or {}

    for key, value in local.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value

    missing = [k for k in ("feed_channel", "post_channel") if not cfg["slack"].get(k)]
    if missing:
        sys.exit(f"{LOCAL_CONFIG_PATH.name} is missing slack.{', slack.'.join(missing)}")
    return cfg


def build_roster(cfg):
    """The team is whoever is in the destination channel.

    Deriving the roster from channel membership rather than keeping a list by
    hand means it stays correct as people join and leave, and removes the
    guesswork that a hand-written list invites.
    """
    people = []
    for user_id in slack_source.channel_members(token(), cfg["slack"]["post_channel"]):
        info = slack_source.user_info(token(), user_id)
        if info and info["email"]:
            people.append(info)
    people.sort(key=lambda p: p["email"])
    return {"members": people, "builtAt": dt.datetime.now().isoformat(timespec="seconds")}


def load_roster(cfg, refresh=False):
    if not refresh and ROSTER_CACHE.exists():
        return json.loads(ROSTER_CACHE.read_text())
    roster = build_roster(cfg)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ROSTER_CACHE.write_text(json.dumps(roster, ensure_ascii=False, indent=2))
    return roster


def display_name(cfg, person):
    return (cfg.get("display_names") or {}).get(person["email"], person["name"])


def connect():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def token():
    value = os.environ.get("SLACK_BOT_TOKEN")
    if not value:
        sys.exit(
            "SLACK_BOT_TOKEN is not set.\n"
            "Needs channels:history on the feed channel and chat:write on the "
            "destination channel."
        )
    return value


# -- ingest ----------------------------------------------------------------


def ingest(cfg, conn, full=False):
    """Pull new messages into the store. Returns (scanned, stored)."""
    oldest = None
    if not full:
        row = conn.execute("SELECT value FROM meta WHERE key='last_ts'").fetchone()
        oldest = row["value"] if row else None

    scanned = stored = 0
    newest = oldest
    for message in slack_source.fetch_messages(
        token(), cfg["slack"]["feed_channel"], oldest=oldest
    ):
        ts = message.get("ts")
        text = message.get("text") or ""
        if not ts:
            continue
        scanned += 1
        if newest is None or float(ts) > float(newest):
            newest = ts

        posted_on = dt.datetime.fromtimestamp(
            float(ts), zoneinfo.ZoneInfo(cfg["timezone"])
        ).date()
        event = parse_message(text, posted_on)
        if not event:
            continue
        conn.execute(
            "INSERT OR REPLACE INTO events "
            "(ts, flex_name, kind, date_from, date_to, span, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                ts,
                event["flexName"],
                event["kind"],
                event["dateFrom"],
                event["dateTo"],
                event["span"],
                text,
            ),
        )
        stored += 1

    if newest:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_ts', ?)", (newest,)
        )
    conn.commit()
    return scanned, stored


# -- query -----------------------------------------------------------------


def active_leave(conn, day):
    """Leave in force on `day`, after replaying grants and cancellations.

    The last event for a given (person, range, span) wins, so a cancellation
    that arrives before its grant does not suppress it, and leave re-requested
    after a cancellation counts again.
    """
    rows = conn.execute(
        "SELECT * FROM events WHERE date_from <= ? AND date_to >= ? ORDER BY ts",
        (day.isoformat(), day.isoformat()),
    ).fetchall()

    latest = {}
    for row in rows:
        key = (normalize(row["flex_name"]), row["date_from"], row["date_to"], row["span"])
        latest[key] = row  # rows arrive in timestamp order, so the last one wins

    return [row for row in latest.values() if row["kind"] == "grant"]


def alias_index(cfg, roster):
    """Map each configured flex display name to a roster member.

    Exact matching only, after whitespace collapsing and case folding.
    """
    by_email = {p["email"]: p for p in roster["members"]}
    index = {}
    for email, aliases in (cfg.get("flex_names") or {}).items():
        person = by_email.get(email.lower())
        if person:
            for alias in aliases or []:
                index[normalize(alias)] = person
    return index


def unmapped_members(cfg, roster):
    configured = {
        email.lower()
        for email, aliases in (cfg.get("flex_names") or {}).items()
        if aliases
    }
    return sorted(
        display_name(cfg, p) for p in roster["members"] if p["email"] not in configured
    )


def collect(cfg, conn, day, roster):
    index = alias_index(cfg, roster)
    entries = []
    for row in active_leave(conn, day):
        person = index.get(normalize(row["flex_name"]))
        if not person:
            continue  # someone from another team, or an alias we have not mapped
        entries.append(
            {
                "name": display_name(cfg, person),
                "span": row["span"],
                "multiDay": row["date_from"] != row["date_to"],
                "dateFrom": row["date_from"],
                "dateTo": row["date_to"],
            }
        )
    return entries


# -- rendering -------------------------------------------------------------


def render(cfg, day, entries, unmapped):
    weekday = "월화수목금토일"[day.weekday()]
    lines = [f"*{cfg['slack']['header']}* — {day:%Y-%m-%d}({weekday})", ""]

    if not entries:
        lines.append("오늘 휴가자 없습니다.")
    else:
        for entry in sorted(entries, key=lambda e: e["name"]):
            detail = entry["span"]
            if entry["multiDay"]:
                detail += f", {entry['dateFrom']}~{entry['dateTo']}"
            lines.append(f"• {entry['name']} — 휴가 ({detail})")

    if unmapped:
        lines += ["", "_별칭 미등록:_ " + ", ".join(unmapped)]
    return "\n".join(lines)


# -- commands --------------------------------------------------------------


def resolve_day(cfg, args):
    tz = zoneinfo.ZoneInfo(cfg["timezone"])
    return dt.date.fromisoformat(args.date) if args.date else dt.datetime.now(tz).date()


def cmd_digest(cfg, args, send):
    day = resolve_day(cfg, args)
    if cfg.get("skip_weekends") and day.weekday() >= 5:
        print(f"{day} is a weekend, skipping.", file=sys.stderr)
        return
    if day.isoformat() in (cfg.get("skip_dates") or []):
        print(f"{day} is a configured holiday, skipping.", file=sys.stderr)
        return

    conn = connect()
    if not args.no_ingest:
        ingest(cfg, conn)

    roster = load_roster(cfg)
    entries = collect(cfg, conn, day, roster)
    text = render(cfg, day, entries, unmapped_members(cfg, roster))
    print(text)

    if send and not entries and not cfg.get("post_when_empty", True):
        print("\n[slack] nobody is out, not posting", file=sys.stderr)
        return
    if send:
        # --channel lets the first real post go somewhere harmless before it
        # goes to a channel of 20 people.
        destination = args.channel or cfg["slack"]["post_channel"]
        slack_source.post_message(token(), destination, text)
        print(f"\n[slack] posted to {destination}", file=sys.stderr)


def cmd_ingest(cfg, args):
    conn = connect()
    scanned, stored = ingest(cfg, conn, full=args.full)
    total = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    print(f"scanned {scanned} messages, stored {stored} events, {total} total")


def cmd_names(cfg, args):
    """Audit the flex_names table against what the feed actually contains."""
    conn = connect()
    roster = load_roster(cfg)
    index = alias_index(cfg, roster)
    configured = {k.lower(): v for k, v in (cfg.get("flex_names") or {}).items()}
    since = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()

    rows = conn.execute(
        "SELECT flex_name, COUNT(*) AS n FROM events WHERE date_to >= ? "
        "GROUP BY flex_name ORDER BY n DESC",
        (since,),
    ).fetchall()
    seen = {normalize(r["flex_name"]): r["n"] for r in rows}

    print(f"roster: {len(roster['members'])} members (from the channel, "
          f"built {roster['builtAt']})")
    print(f"feed names in the last {args.days}d: {len(rows)}\n")

    missing = []
    for person in roster["members"]:
        name = display_name(cfg, person)
        aliases = configured.get(person["email"]) or []
        hits = sum(seen.get(normalize(a), 0) for a in aliases)
        if not aliases:
            print(f"  {name:16} NO FLEX NAME CONFIGURED")
            missing.append(person)
        elif hits:
            print(f"  {name:16} {hits:>4} events  ({', '.join(aliases)})")
        else:
            print(f"  {name:16} configured but never seen: {', '.join(aliases)}")

    if missing:
        # Suggestions for a human. Never applied automatically: feeds hold
        # distinct people whose names differ by a single letter.
        print("\ncandidate feed names to review by hand (NOT auto-matched)")
        print("-" * 62)
        for person in missing:
            parts = [p for p in normalize(person["email"].split("@")[0]).replace(".", " ").split() if p]
            parts += normalize(person["name"]).split()
            hints = [
                r["flex_name"]
                for r in rows
                if normalize(r["flex_name"]) not in index
                and any(part in normalize(r["flex_name"]) for part in parts)
            ]
            print(f"  {display_name(cfg, person):16} <- "
                  f"{', '.join(dict.fromkeys(hints))[:70] or '(no hint)'}")
        print("\nVerify each against the actual person before adding it. Feeds "
              "contain\ndifferent employees whose names differ by one letter.")


def cmd_roster(cfg, args):
    roster = load_roster(cfg, refresh=args.refresh)
    configured = {k.lower() for k, v in (cfg.get("flex_names") or {}).items() if v}
    print(f"{len(roster['members'])} members in "
          f"{cfg['slack']['post_channel']} (built {roster['builtAt']})\n")
    for person in roster["members"]:
        mark = "ok " if person["email"] in configured else "no flex name"
        print(f"  {display_name(cfg, person):16} {person['email']:34} {mark}")


def cmd_doctor(cfg, args):
    print("config       :", CONFIG_PATH)
    print("db           :", DB_PATH, "(exists)" if DB_PATH.exists() else "(not created)")
    print("roster cache :", "present" if ROSTER_CACHE.exists() else "not built yet")
    print("flex names   :", sum(1 for v in (cfg.get("flex_names") or {}).values() if v))
    print("SLACK_BOT_TOKEN:", "set" if os.environ.get("SLACK_BOT_TOKEN") else "MISSING")
    if not os.environ.get("SLACK_BOT_TOKEN"):
        return
    print("\nreading feed ...", end=" ", flush=True)
    try:
        count = sum(
            1
            for _ in zip(
                range(5),
                slack_source.fetch_messages(
                    token(), cfg["slack"]["feed_channel"], limit_pages=1
                ),
            )
        )
        print(f"ok ({count} messages sampled)")
    except SlackError as exc:
        print(f"FAILED\n  {exc}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("preview", "post"):
        p = sub.add_parser(name)
        p.add_argument("--date", help="YYYY-MM-DD (default: today)")
        p.add_argument(
            "--no-ingest", action="store_true", help="query the store as-is"
        )
        p.add_argument(
            "--channel", help="post here instead of the configured channel"
        )
    p = sub.add_parser("ingest")
    p.add_argument("--full", action="store_true", help="re-read the whole channel")
    p = sub.add_parser("names")
    p.add_argument("--days", type=int, default=120)
    p = sub.add_parser("roster")
    p.add_argument("--refresh", action="store_true", help="re-read channel membership")
    sub.add_parser("doctor")

    args = parser.parse_args()
    cfg = load_config()
    try:
        if args.cmd == "ingest":
            cmd_ingest(cfg, args)
        elif args.cmd == "names":
            cmd_names(cfg, args)
        elif args.cmd == "roster":
            cmd_roster(cfg, args)
        elif args.cmd == "doctor":
            cmd_doctor(cfg, args)
        else:
            cmd_digest(cfg, args, send=args.cmd == "post")
    except SlackError as exc:
        sys.exit(f"Slack API error: {exc}")


if __name__ == "__main__":
    main()
