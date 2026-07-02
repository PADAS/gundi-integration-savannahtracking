# Registration naming overrides

Date: 2026-07-02
Status: Approved

## Problem

The self-registration code derives display names mechanically:

- An action's `name` comes from the handler function name
  (`action_read_observations` → "Read Observations") in
  `app/services/self_registration.py`.
- The integration type's `name` comes from the slug
  (`savannahtracking` → "Savannahtracking"), which produces wrong
  names for multi-word brands like "Savannah Tracking".

There is no way to set either name directly.

## Design

### 1. Action title override — `@action_title` decorator

New decorator in `app/actions/core.py`:

```python
def action_title(title: str):
    def decorator(func):
        setattr(func, "action_title", title)
        return func
    return decorator
```

No wrapper — it tags the function and returns it, so it stacks safely
with `@crontab_schedule` and `@activity_logger()` in either order
(those use `functools.wraps`, which copies the attribute through their
wrappers).

In `app/services/self_registration.py`:

```python
action_name = getattr(func, "action_title", None) or action_id.replace("_", " ").title()
```

The custom title also flows into the action's generated `description`.

### 2. Integration type name override — `--name` / `INTEGRATION_TYPE_NAME`

Mirrors the existing slug/service_url pattern in all three layers:

- `app/settings/base.py`: `INTEGRATION_TYPE_NAME = env.str("INTEGRATION_TYPE_NAME", None)`
- `app/register.py`: new `--name` click option, passed as `type_name`
  to `register_integration_in_gundi`
- `app/services/self_registration.py`:
  `integration_type_name = type_name or INTEGRATION_TYPE_NAME or integration_type_slug.replace("_", " ").title()`

The name flows everywhere `integration_type_name` is already used: the
type's `name`/`description`, every action's description, and the
webhook's name/description.

### 3. Applying it to this integration

Add `--name "Savannah Tracking"` to the three `register` launch
configurations in `connector.code-workspace`. No `@action_title` usages
are needed yet; the derived action names are acceptable.

## Compatibility

Both overrides are optional; existing behavior is the fallback, so the
change is backward compatible and upstreamable to the template.

## Testing

Extend the self-registration tests with two cases:

1. A handler carrying `action_title` registers with the custom name.
2. `type_name` overrides the slug-derived integration type name.
