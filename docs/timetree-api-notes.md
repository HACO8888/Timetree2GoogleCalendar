# TimeTree private API — observed shape

Recorded from a live account on 2026-08-15 with `tt2gcal recon`. Raw payloads land
in `raw-timetree/` (gitignored — they contain personal data). Everything here is
observation, not documentation: TimeTree publishes none.

Calendars are referred to as A–F rather than by their real names, and no event
titles, ids or user ids appear anywhere in this file. Run `tt2gcal recon` against
your own account to see the real thing.

Base URI `https://timetreeapp.com/api/v1`, header `X-Timetreea: web/2.1.0/en`,
auth via the `_session_id` cookie from `PUT /auth/email/signin`.

## Endpoints in use

| Endpoint | Returns |
| --- | --- |
| `GET /calendars?since=0` | every calendar, **with `calendar_labels` and `calendar_users` inline** |
| `GET /calendar/{id}/events/sync` | all events; `chunk: true` means call again with `?since=<cursor>` |
| `GET /calendar/{id}/labels` | the same labels as the inline copy — redundant, so we do not call it |

The account under test returned 6 calendars / 106 events, of which **58 are
syncable** once birthdays and memos are dropped.

## Calendar object

```
id, name, type (0 for all observed), permission ("permitted"),
alias_code, color (int), order, deactivated_at, note,
calendar_labels[], calendar_users[]
```

`deactivated_at` exists on calendars too, so a left/removed calendar can appear
in the list with a timestamp. Filter it.

Only shared calendars appear here; the user's own private calendar is not listed.

## Event object

Every event carries the same keys, so the schema is stable:

```
uuid, id            identical strings; uuid is the stable primary key
primary_id          separate numeric id, not used
calendar_id, label_id
type                0 = normal, 1 = birthday
category            1 = normal, 2 = memo
title, note, location, location_lat, location_lon, url
all_day             bool
start_at, end_at    epoch milliseconds
start_timezone, end_timezone   IANA names
alerts[]            minutes before start
recurrences[]       full "RRULE:..." content lines
recurring_uuid, parent_id      set on per-occurrence children
attendees[], attachment{}, files[], like_count, media_content_count
lunar, pinned_at, row_order, link_object_id
deactivated_at      <- tombstone
created_at, updated_at
```

## A. Deletion — `deactivated_at`

`deactivated_at` is **present on every event object** and was `null` for all 106.
So no deleted events were in flight during the snapshot, but the field exists in
the schema, which means the API can hand us tombstones.

**Consequence:** the mapper filters any event with a non-null `deactivated_at`.
Not doing so would resurrect deleted events on the next run. This costs nothing
when the field is always null, and is the difference between correct and broken
when it is not.

Deletion is still detected primarily by absence from the full fetch, so the sync
is correct whether TimeTree omits deleted events or returns them tombstoned.

## B. Recurring events — still unanswered

Every event carrying `recurrences` in this account is a **birthday**
(`type == 1`), and the correlation is exact in all six calendars:

| Calendar | events | with `recurrences` | of those, birthdays |
| --- | --- | --- | --- |
| A | 23 | 13 | 13 |
| B | 12 | 5 | 5 |
| C | 11 | 5 | 5 |
| D | 17 | 9 | 9 |
| E | 18 | 10 | 10 |
| F | 25 | 6 | 6 |

Birthdays are auto-generated, titleless, `RRULE:FREQ=YEARLY`, and filtered out
anyway — so **there are zero real recurring events to learn from**, and zero
`parent_id` / `recurring_uuid` children.

**Status: inconclusive.** Re-run `tt2gcal recon` after creating a weekly
repeating event in TimeTree, editing one occurrence and deleting another. Until
then the mapper passes `recurrences` straight through as Google `recurrence`
and *refuses* to insert any child whose master is also present, so a modified
occurrence can never double-book a date.

## C. Times and time zones

Of the 58 syncable events: 36 all-day, 22 timed.

- **All-day**: `all_day: true`, `start_timezone == end_timezone == "UTC"`.
  TimeTree's end date is **inclusive** — a one-day event has `start_at == end_at`
  (27 of 36), and a multi-day span carries the last day itself (spans of 1, 3, 4
  and 6 days were observed). Google's end date is exclusive, so both cases map by
  adding exactly one day to the end.
- **Timed**: real IANA zone (`Asia/Taipei` throughout this account). Durations
  observed: 0 min (×3), 50, 60 (×14), 90, 120, and one 12-day event.
- **Zero-duration timed events are real** — TimeTree lets an event be a point in
  time, and 3 of 22 are. Google rejects a timed event whose end equals its start,
  so the mapper extends those to one minute. Any invented duration is a fabrication;
  one minute is the smallest one, and it beats the event failing on every run.
  *Not yet confirmed against the live API — verify during the first real sync.*
- `start_timezone != end_timezone` never occurred (0 of 106), and no event had an
  end before its start.
- `alerts` are minutes before start and map 1:1 onto Google
  `reminders.overrides[].minutes`. `900` on an all-day event is Google's own
  encoding of "1 day before at 09:00" (24 h − 9 h), so it passes through unchanged.

## Labels

`calendar_labels` gives 10 labels per calendar as `{id, name, color, order}` with
`color` as an integer — format with `f"#{color:06x}"`.

**58 of 60 labels in this account have an empty `name`.** Colour is therefore the
only meaningful label signal, which is why the mapper carries labels across as
Google `colorId` rather than as text.
