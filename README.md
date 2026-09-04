# standup — daily team schedule digest

Posts a morning summary of who is on leave today to a team Slack channel.

Reads the channel where flex's own Slack integration posts every approved
leave and every cancellation for the whole company. That integration is
admin-configured, so it covers everyone with no per-member opt-in.

```
*오늘의 팀 일정* — 2026-09-11(금)

• 손희관 — 휴가 (종일)
• 홍승훈 — 휴가 (종일, 2026-09-09~2026-09-14)
• 박경우 — 휴가 (09:00-14:00)
```

## Setup

Create a Slack app, invite its bot to both channels, and give it:

| Scope | For |
|---|---|
| `channels:history` | reading the feed channel (public) |
| `groups:read` | listing the destination channel's membership (private) |
| `users:read`, `users:read.email` | resolving members to emails |
| `chat:write` | posting the digest |

[`slack-app-manifest.yaml`](./slack-app-manifest.yaml) declares exactly these;
create the app from it rather than ticking boxes.

Then set up configuration. `config.yaml` is committed and generic;
`config.local.yaml` holds the channel IDs and the name table, and is gitignored
because that table is personal data:

```bash
cp config.local.example.yaml config.local.yaml
$EDITOR config.local.yaml         # channel IDs first, flex_names as you learn them
```

```bash
export SLACK_BOT_TOKEN='xoxb-...'
python3 main.py doctor            # token and feed reachability
python3 main.py roster --refresh  # read the team from the channel
python3 main.py ingest --full     # backfill the store
python3 main.py names             # audit the flex-name table
```

## Usage

```bash
python3 main.py preview                   # ingest, then render today
python3 main.py preview --date 2026-09-11
python3 main.py preview --no-ingest       # query the store as-is
python3 main.py post                      # render and post to Slack
python3 main.py roster                    # show the derived roster
python3 main.py roster --refresh          # re-read channel membership
python3 main.py names --days 120          # flex-name audit
python3 -m unittest test_digest           # no token needed
```

## The roster

The team is **whoever is in the destination channel**, read from it via
`conversations.members` and cached in `roster_cache.json`. It is not a list in
config, so it stays correct as people join and leave. Refresh it with
`roster --refresh`; the digest uses the cache otherwise.

## The flex-name table

This is the part that needs upkeep.

The feed identifies people **only by the display name registered in flex**,
which is maintained separately from Slack and drifts from it. Slack's
One person's Slack name appeared in the feed spelled differently, and another's
differed only in capitalisation.

So `config.yaml` carries an explicit `flex_names` entry per email, matched
**exactly** after whitespace collapsing and case folding. There is deliberately
no fuzzy matching, because the feed turned out to contain two genuinely
different employees whose names differed by a single letter. Announcing that a
colleague
is out when they are at their desk is a worse failure than announcing nothing,
and unlike a gap nobody notices it.

Consequences to keep in mind:

- A channel member with no entry never appears. They are listed in the digest
  footer under 별칭 미등록 rather than passing silently as "no leave".
- If someone changes their name in flex, they silently drop out. `names` shows
  each member's configured name, how many events it matched, and flags entries
  that are set but never seen. Run it every so often.
- `names` also suggests candidate strings from the feed for unmapped members.
  These are suggestions for a human, never applied automatically. Verify each
  against the actual person before adding it.

## How it works

Leave is announced months ahead, cancellations can arrive before the grant they
cancel, and leave can be re-requested after cancellation. So messages are
ingested into a local SQLite store (`events.db`, gitignored) and replayed in
timestamp order: for a given person, date range and span, the last event wins.
`preview` and `post` ingest incrementally first, then query that store.

## Limits

- **No leave type, and no way to get it.** flex genericizes every reason to
  휴가 before posting, so 병가, 예비군 and 보상휴가 are indistinguishable here.
  The same is true of the calendar mirror. The only source that exposes the
  real type is the flex Open API, which requires the Enterprise plan and was
  ruled out on cost. See [FLEX-API.md](./FLEX-API.md). Treat this as
  permanent: if the team
  needs the distinction, someone has to say so in the channel by hand, as they
  do today.
- **No 재택 / 외근 / 출장.** The feed carries leave only.
- Identity rests on a hand-maintained name table, as above.

## Configuration

`config.yaml` is committed and holds only generic settings: timezone, message
header, weekend and holiday skipping. `config.local.yaml` is gitignored and
holds the two channel IDs, the `flex_names` table and optional `display_names`
overrides. Point `STANDUP_CONFIG` elsewhere to use a different pair.

The roster itself is derived from the destination channel, not configured.

## Scheduling

Runs from a systemd user timer on a workstation, at 08:00 KST on weekdays.
The units are not in this repo since they carry local paths; see
**Running it elsewhere** below for the shape.

[`deploy/github-actions.yml`](./deploy/github-actions.yml) is a ready
GitHub Actions equivalent, on `0 23 * * 0-4` UTC which is 08:00 KST Monday to
Friday, for whenever this should stop depending on one machine. It is parked
outside `.github/workflows/` so it stays inert, and because pushing to that
path needs a token with the `workflow` scope. A late run still picks the right
day either way, because the date is resolved in the timezone from
`config.yaml` rather than from the runner's clock.

That workflow would need one repository secret:

| Secret | Value |
|---|---|
| `SLACK_BOT_TOKEN` | the bot token from the Slack app above |

It enables `workflow_dispatch` with inputs for `date`, `channel` and
`preview_only`, so a change can be tried against a scratch channel without
posting to the team, and a `test` job that needs no secret.

### Running it anywhere

`events.db` and `roster_cache.json` are caches that a fresh run rebuilds in
about 36 seconds, so any scheduler works. A systemd user timer wants
`OnCalendar=Mon..Fri 08:00` with `Persistent=true`, an `EnvironmentFile`
holding `SLACK_BOT_TOKEN`, and `StateDirectory=standup`. A crontab works too:

```cron
0 8 * * 1-5 cd /path/to/standup && \
  SLACK_BOT_TOKEN='xoxb-...' /usr/bin/python3 main.py post >> /tmp/standup.log 2>&1
```

Set `STANDUP_STATE_DIR` to keep the caches somewhere other than alongside the
code, which a read-only or containerised deployment will want.
