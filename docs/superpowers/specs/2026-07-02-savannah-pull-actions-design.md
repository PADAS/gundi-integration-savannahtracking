# Savannah Tracking Pull Actions — Design

Date: 2026-07-02

## Goal

Replace the legacy CDIP integration (`cdip-integrations/savannah`, a K8s cronjob built on
`cdip_connector.AbstractConnector`) with actions in this Gundi v2 action runner. The new
implementation pulls collar positions from the Savannah Tracking API and sends them to Gundi
as observations, preserving the legacy production behavior.

Scope decision (confirmed): **observations only**. Alerts (`exceptions_download`) are out of
scope and can be added later as a separate action.

## Savannah Tracking API (from legacy code)

Base URL: `https://api.savannahtracking.co.ke` (the legacy config used `endpoint`; here we use
`integration.base_url`, falling back to this default).

- `POST {base}/savannah_data/data_auth` with form data
  `{request: "authenticate", uid, pwd}` → `{"sucess": true, "records": [collar_id, ...]}`
  (note the API's misspelled `sucess` key) or `{"sucess": false, "login_error_msg": "..."}`.
- `POST {base}/savannah_data/data_request` with form data
  `{request: "data_download", uid, pwd, collar, record_index}` →
  `{"records": [...], "has_more_records": bool}`.
  Records carry `record_index`, `record_time` (naive UTC), `latitude`, `longitude`, `speed`,
  `heading`, `temperature`, `h_accuracy`, `hdop`, `battery`. Pagination is based on
  `record_index`; pass the highest index seen to resume.

## Actions

### `check_credentials` — `action_check_credentials`

- Config: `CredentialsConfig(AuthActionConfiguration, ExecutableActionMixin)` with
  `username: str` and `password: pydantic.SecretStr` (password widget, field order set via
  `GlobalUISchemaOptions`).
- Handler calls `data_auth`. Returns `{"valid_credentials": bool}` plus collar count on
  success; on bad credentials returns `{"valid_credentials": False}` rather than raising, so
  the portal shows a clean result.

### `read_observations` — `action_read_observations`

- Config: `ReadObservationsConfig(PullActionConfiguration)` with
  `lookback_days: int = 3` (1–30, matches the legacy 3-day minimum-date window) and
  `subject_type: str = "unassigned"`, applied to every observation.
- Scheduled with `@crontab_schedule("*/5 * * * *")`, matching the legacy cronjob cadence.
- Handler: reads the `check_credentials` config from the integration, fetches the collar list, and triggers
  a `read_observations_per_collar` sub-action for each collar via
  `app.services.action_scheduler.trigger_action`. Returns `{"collars_triggered": n}`.

### `read_observations_per_collar` — `action_read_observations_per_collar`

- Config: `ReadObservationsPerCollarConfig(InternalActionConfiguration)` with `collar_id: str`,
  `lookback_days: int = 3`, and `subject_type: str = "unassigned"` (propagated from the parent
  action). Internal: not shown in the
  portal.
- Handler flow:
  1. **Dormant backoff check**: if the backoff key for this collar exists in the state store,
     return `{"skipped": true, ...}` without querying the API.
  2. Read the watermark: `{"record_index": int, "latest_timestamp": iso}`; default
     `record_index=-1` (legacy default for a new collar).
  3. Page through `data_download` from the watermark until `has_more_records` is false,
     accumulating records and advancing the `record_index`.
  4. Filter records to those with `recorded_at >= now - lookback_days`; if all records are
     older, keep only the newest record (legacy `vals or vals_tail` behavior, which keeps the
     downstream track alive).
  5. Transform to Gundi observations (see below) and send with
     `send_observations_to_gundi` in batches of 200.
  6. Persist the watermark.
  7. **Dormant backoff set**: if records were fetched and the newest one is older than the
     lookback window, set the backoff key with a random TTL of 76000–96000 seconds
     (~21–27h), so dormant collars are queried roughly daily (legacy behavior).
- State keys (via `IntegrationStateManager`):
  - Watermark: `action_id="read_observations"`, `source_id=<collar_id>`.
  - Backoff: `set_if_absent` with `action_id="read_observations_backoff"`,
    `source_id=<collar_id>` and the TTL above; the check is `get_state` truthiness.

## Observation format

```json
{
  "source": "<collar_id>",
  "type": "tracking-device",
  "subject_type": "<from config, default unassigned>",
  "recorded_at": "<record_time parsed as UTC, ISO-8601>",
  "location": {"lat": <latitude>, "lon": <longitude>},
  "additional": {
    "speed": ..., "heading": ..., "temperature": ...,
    "accuracy": <h_accuracy>, "hdop": ..., "battery": ...,
    "record_index": ...
  }
}
```

Same field mapping as the legacy `SavannahConnector.transform`, plus a configurable
`subject_type` (default `unassigned`).

## New modules

- `app/actions/client.py` — thin async API client:
  - `SavannahBadCredentialsException` / `SavannahApiException`.
  - `SavannahRecord` pydantic model (tolerant: numeric fields optional).
  - `get_collar_list(base_url, username, password) -> list[str]`.
  - `get_collar_data_page(base_url, username, password, collar_id, record_index) ->
    (records, has_more_records)`.
  - Uses `httpx.AsyncClient` with a sane timeout; raises on non-2xx.
- `app/actions/configurations.py` — the three config models + `get_auth_config(integration)`
  helper that raises `ConfigurationNotFound` if the `check_credentials` action is not configured.
- `app/actions/handlers.py` — the three handlers, decorated with `@activity_logger()`.

## Error handling

- Bad credentials in `read_observations`: raise, so the action runner records the error and
  the portal surfaces it (the `check_credentials` action is the way to test credentials cleanly).
- Per-collar HTTP errors: raise from the sub-action; failures are isolated per collar and
  logged by the activity logger. No swallow-and-continue as the legacy did — the action
  runner already provides retry/error visibility.

## Testing

`app/actions/tests/` (new):
- `test_client.py` — respx-mocked httpx: auth success/failure, pagination, record parsing.
- `test_handlers.py` — mock client, `send_observations_to_gundi`, `trigger_action`, and state
  manager: fan-out, watermark advance, lookback filtering with tail-keep, backoff skip/set,
  auth handler results.

Existing `app/conftest.py` fixtures (mock integration, publish_event, etc.) are reused.
