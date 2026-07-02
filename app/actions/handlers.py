import logging
import random
from datetime import datetime, timedelta, timezone

from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager

from . import client
from .configurations import (
    CredentialsConfig,
    ReadObservationsConfig,
    ReadObservationsPerCollarConfig,
    get_auth_config,
)

logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()


GUNDI_BATCH_SIZE = 200
WATERMARK_STATE_ACTION_ID = "read_observations"
BACKOFF_STATE_ACTION_ID = "read_observations_backoff"
# When a collar's newest record is older than the lookback window, skip it for
# ~21-27 hours (randomized to spread the load across runs), like the legacy
# CDIP integration did.
BACKOFF_TTL_RANGE_SECONDS = (76000, 96000)


def _get_base_url(integration) -> str:
    return (integration.base_url or client.DEFAULT_API_BASE_URL).rstrip("/")


def _transform(collar_id: str, subject_type: str, record: client.SavannahRecord) -> dict:
    return {
        "source": collar_id,
        "type": "tracking-device",
        "subject_type": subject_type,
        "recorded_at": record.recorded_at.isoformat(),
        "location": {
            "lat": record.latitude,
            "lon": record.longitude,
        },
        "additional": {
            "speed": record.speed,
            "heading": record.heading,
            "temperature": record.temperature,
            "accuracy": record.h_accuracy,
            "hdop": record.hdop,
            "battery": record.battery,
            "record_index": record.record_index,
        },
    }


async def action_check_credentials(integration, action_config: CredentialsConfig):
    logger.info(f"Executing auth action with integration {integration} and action_config {action_config}...")
    try:
        collar_ids = await client.get_collar_list(
            base_url=_get_base_url(integration),
            username=action_config.username,
            password=action_config.password.get_secret_value(),
        )
    except client.SavannahBadCredentialsException as e:
        return {"valid_credentials": False, "message": str(e)}
    return {"valid_credentials": True, "collars_qty": len(collar_ids)}


@crontab_schedule("*/5 * * * *")
@activity_logger()
async def action_read_observations(integration, action_config: ReadObservationsConfig):
    logger.info(f"Executing read_observations action with integration {integration} and action_config {action_config}...")
    auth_config = get_auth_config(integration)
    collar_ids = await client.get_collar_list(
        base_url=_get_base_url(integration),
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    for collar_id in collar_ids:
        await trigger_action(
            integration_id=str(integration.id),
            action_id=action_read_observations_per_collar.__name__[len("action_"):],
            config=ReadObservationsPerCollarConfig(
                collar_id=collar_id,
                lookback_days=action_config.lookback_days,
                subject_type=action_config.subject_type,
            ),
        )
    return {"collars_triggered": len(collar_ids)}


@activity_logger()
async def action_read_observations_per_collar(integration, action_config: ReadObservationsPerCollarConfig):
    collar_id = action_config.collar_id
    integration_id = str(integration.id)
    logger.info(f"Executing read_observations_per_collar action for collar {collar_id} in integration {integration_id}...")

    backoff = await state_manager.get_state(integration_id, BACKOFF_STATE_ACTION_ID, collar_id)
    if backoff:
        logger.info(f"Collar {collar_id} is in a dormant-collar backoff period. Skipping.")
        return {"collar_id": collar_id, "skipped": True, "reason": "Dormant-collar backoff period is active"}

    auth_config = get_auth_config(integration)
    base_url = _get_base_url(integration)
    watermark = await state_manager.get_state(integration_id, WATERMARK_STATE_ACTION_ID, collar_id)
    record_index = watermark.get("record_index", -1)

    records = []
    has_more_records = True
    while has_more_records:
        page, has_more_records = await client.get_collar_data_page(
            base_url=base_url,
            username=auth_config.username,
            password=auth_config.password.get_secret_value(),
            collar_id=collar_id,
            record_index=record_index,
        )
        if page:
            record_index = max(record.record_index for record in page)
            records.extend(page)
        elif has_more_records:
            # A page with no parseable records can't advance the watermark;
            # stop rather than re-request the same index forever.
            logger.warning(f"Got a page with no parseable records for collar {collar_id}. Stopping pagination.")
            break

    min_date = datetime.now(tz=timezone.utc) - timedelta(days=action_config.lookback_days)
    newest_record = max(records, key=lambda record: record.recorded_at) if records else None
    fresh_records = [record for record in records if record.recorded_at >= min_date]
    # If all records are older than the lookback window, still send the newest
    # one so the collar's last known position stays current downstream.
    records_to_send = fresh_records or ([newest_record] if newest_record else [])

    observations = [_transform(collar_id, action_config.subject_type, record) for record in records_to_send]
    observations_sent = 0
    for i in range(0, len(observations), GUNDI_BATCH_SIZE):
        batch = observations[i: i + GUNDI_BATCH_SIZE]
        await send_observations_to_gundi(observations=batch, integration_id=integration_id)
        observations_sent += len(batch)

    if records:
        await state_manager.set_state(
            integration_id,
            WATERMARK_STATE_ACTION_ID,
            {
                "record_index": record_index,
                "latest_timestamp": newest_record.recorded_at.isoformat(),
            },
            collar_id,
        )
        if newest_record.recorded_at < min_date:
            await state_manager.set_if_absent(
                integration_id,
                BACKOFF_STATE_ACTION_ID,
                ttl_seconds=random.randint(*BACKOFF_TTL_RANGE_SECONDS),
                source_id=collar_id,
            )

    return {
        "collar_id": collar_id,
        "observations_extracted": len(records_to_send),
        "observations_sent": observations_sent,
    }
