# flex Open API — target data source for the digest

The calendar source this tool currently uses collapses every kind of leave into
`🌴 휴가`. The flex Open API returns the leave type, so it is the correct
long-term source. This file records the spec so the swap is mechanical.

**Status: ruled out on cost.** The Open API needs the Enterprise plan, and the
upgrade was not worth it for a standup digest, so this is not a "just ask and
wait" item. Reopen it only if flex is being upgraded for some other reason, and
mention this use case then.

The digest reads the Slack feed instead. A working client for this API was
written before the decision; recover it from the history of the repository this
was extracted from rather than rewriting it.

Keeping this file because it records the one thing the Slack feed cannot do,
and exactly what it would take.

Docs: https://developers.flex.team/ (index at `/llms.txt`; append `.md` to any
reference URL for markdown)

## Why this bypasses the SSO problem

flex is behind Google SSO, so no script can log in as a user. The Open API is
separate: it uses OAuth2 **client credentials**, a machine identity that never
touches the SSO flow. This is the supported path for exactly this case, not a
workaround.

## Base URL

```
https://openapi.flex.team
```

## Authentication

```
POST https://openapi.flex.team/v2/auth/realms/open-api/protocol/openid-connect/token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<ID>&client_secret=<SECRET>
```

Response: `access_token`, `expires_in`, `token_type`, and optionally
`refresh_token` / `refresh_expires_in`. Send it as `Authorization: Bearer <access_token>`.

Credentials are issued in the flex admin console under Open API settings. The
Client Secret is shown **once**. Access-token lifetime is configurable (e.g. 10
minutes) and credential lifetime up to 365 days. Maximum 5 credentials.

A 10-minute token is fine for a daily cron: mint one per run, use it, discard.
No token storage needed, only the client id/secret in a secret store.

## Endpoints for the digest

| Purpose | Endpoint |
|---|---|
| Roster | `GET /v2/users/employee-numbers` (`pageSize` max 20, paginate via `nextPageKey`/`hasNext`) |
| Identity | `GET /v2/user-masters?employeeNumbers=...` → `name`, `email`, `primaryDepartment`, `primaryJobTitle` |
| **Leave for a date** | `GET /v2/users/time-off-uses/dates/{YYYY-MM-DD}?employeeNumbers=...` |
| Leave for a range | `GET /v2/users/time-off-uses/periods/...` |
| Work schedule | `GET /v2/user-work-schedules/...` (외근, 재택, 출장) |
| Holidays | `GET` holidays endpoint — replaces the hand-maintained `skip_dates` |

### The one that matters

```
GET /v2/users/time-off-uses/dates/2026-08-27?employeeNumbers=A00010&employeeNumbers=A00011
```

```json
{
  "userTimeOffUses": [
    {
      "employeeNumber": "A00010",
      "uses": [
        {
          "formName": "연차",
          "dateFrom": "2026-08-27",
          "dateTo": "2026-08-27",
          "dateTimeFrom": null,
          "dateTimeTo": null
        }
      ]
    }
  ]
}
```

`formName` is the leave type name (연차, 병가, 경조사 휴가, ...). This is the
field the calendar route cannot give us. `dateTimeFrom`/`dateTimeTo` are
non-null for 반차/시간차, which is how to render the time span.

## Constraints

- `employeeNumbers` accepts **1-20 per request**. A 22-person roster is 2 calls.
- Read-only. Leave cannot be created, approved, or rejected through the API.
- No push or webhooks. Polling is the only option, which suits a daily digest.
- In-progress shifts cannot be queried in real time.
- **Data with unset code values is omitted from responses.** Employee numbers,
  organization codes, position and job codes must be registered in flex first,
  or members silently disappear from results. Verify this before trusting output.
- Requires permissions for 인사정보 관리, 코스트부서, and 조직/구성원 연결.
- No documented rate limits.

## Open question

Whether 예비군 appears here depends on how the company configured it in flex.
If it is a 휴가 form it arrives as a `formName`. If it was set up as a work type
(공가/외근), it comes from the work-schedule endpoints instead. Check both when
the credentials land.

## Migration

Only `collect()` in `main.py` needs replacing. Rendering, the Slack post, the
weekend and holiday skips, and the config all carry over. The roster in
`config.yaml` switches from calendar addresses to employee numbers, resolved
once via `/v2/user-masters`.
