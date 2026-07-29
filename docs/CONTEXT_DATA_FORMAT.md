# Historical context data format

Flowdesk reads historical context from `data/context`. The files are local and deterministic so a research run can be reproduced from the same market-data and context fingerprints.

## `economic_calendar.csv`

Header:

```csv
source_id,scheduled_at,event_name,currency,importance,forecast,actual,previous,published_at,source
```

Fields:

- `source_id`: stable provider identifier. Flowdesk derives one if omitted.
- `scheduled_at`: scheduled release time in ISO 8601 UTC, for example `2026-07-14T12:30:00Z`.
- `event_name`: CPI, PPI, NFP, FOMC decision, Fed speech, and similar.
- `currency`: normally `USD` for MES-relevant United States releases.
- `importance`: `high`, `medium`, or `low`.
- `forecast`, `actual`, `previous`: numeric values when applicable.
- `published_at`: exact timestamp at which the actual value became available. This field is mandatory for point-in-time-safe surprise features. When omitted, Flowdesk conservatively uses `scheduled_at`.
- `source`: provider name.

The research engine does not expose `actual` before `published_at`.

## `news_events.jsonl`

One JSON object per line:

```json
{"source_id":"provider-123","published_at":"2026-07-14T14:01:12.250Z","headline":"Example headline","provider":"example","relevance":0.9,"sentiment":-0.6,"symbols":["MES","ES"]}
```

Fields:

- `source_id`: stable provider identifier.
- `published_at`: first public availability timestamp in ISO 8601 UTC.
- `headline`: original headline.
- `provider`: source/provider name.
- `relevance`: value from 0 to 1.
- `sentiment`: value from -1 to 1. This is context, not an autonomous trade instruction.
- `symbols`: affected instruments.

Flowdesk never exposes a headline before `published_at`.

## `coverage.json`

Example:

```json
{
  "economicCalendar": {
    "source": "provider-name",
    "coverageStart": "2026-01-01T00:00:00Z",
    "coverageEnd": "2026-07-01T00:00:00Z"
  },
  "news": {
    "source": "provider-name",
    "coverageStart": "2026-01-01T00:00:00Z",
    "coverageEnd": "2026-07-01T00:00:00Z"
  }
}
```

Coverage dates describe the historical interval actually supplied. File creation or modification time is not evidence of coverage. Flowdesk requires both declared coverage and imported rows spanning the complete market-data interval before context coverage is considered complete.

## Safety rules

- All timestamps must be UTC or include an explicit offset.
- Do not backfill a headline with an earlier timestamp than its first publication.
- Revised economic values need their own publication timestamp; do not overwrite the original point-in-time value without preserving revision history.
- Provider licenses and redistribution restrictions remain the user's responsibility.
