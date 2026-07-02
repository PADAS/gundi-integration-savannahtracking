import logging
import random
from datetime import datetime, timedelta, timezone

from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager
from app.services.utils import generate_batches
from app.settings import MAX_ACTION_EXECUTION_TIME

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
LOCK_STATE_ACTION_ID = "read_observations_lock"
# When a collar's newest record is older than the lookback window, skip it for
# ~21-27 hours (randomized to spread the load across runs), like the legacy
# CDIP integration did.
BACKOFF_TTL_RANGE_SECONDS = (76000, 96000)
# A backfill can outlive the 5-minute schedule (the runner allows up to
# MAX_ACTION_EXECUTION_TIME), so runs for the same collar can overlap. The lock
# TTL outlasts the longest possible run in case the holder dies without
# releasing it.
LOCK_TTL_SECONDS = MAX_ACTION_EXECUTION_TIME + 60


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


@activity_logger()
async def action_check_credentials(integration, action_config: CredentialsConfig):
    logger.info(f"Executing check_credentials action for integration '{integration.id}'...")
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
    logger.info(f"Executing read_observations action for integration '{integration.id}'...")
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


def _parse_stored_timestamp(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@activity_logger()
async def action_read_observations_per_collar(integration, action_config: ReadObservationsPerCollarConfig):
    collar_id = action_config.collar_id
    integration_id = str(integration.id)
    logger.info(f"Executing read_observations_per_collar action for collar {collar_id} in integration {integration_id}...")

    backoff = await state_manager.get_state(integration_id, BACKOFF_STATE_ACTION_ID, collar_id)
    if backoff:
        logger.info(f"Collar {collar_id} is in a dormant-collar backoff period. Skipping.")
        return {"collar_id": collar_id, "skipped": True, "reason": "Dormant-collar backoff period is active"}

    lock_acquired = await state_manager.set_if_absent(
        integration_id, LOCK_STATE_ACTION_ID, ttl_seconds=LOCK_TTL_SECONDS, source_id=collar_id
    )
    if not lock_acquired:
        logger.info(f"Another run for collar {collar_id} is still in progress. Skipping.")
        return {"collar_id": collar_id, "skipped": True, "reason": "Another run for this collar is still in progress"}
    try:
        return await _read_collar_observations(integration, action_config)
    finally:
        await state_manager.delete_state(integration_id, LOCK_STATE_ACTION_ID, collar_id)


async def _read_collar_observations(integration, action_config: ReadObservationsPerCollarConfig):
    collar_id = action_config.collar_id
    integration_id = str(integration.id)
    auth_config = get_auth_config(integration)
    base_url = _get_base_url(integration)
    watermark = await state_manager.get_state(integration_id, WATERMARK_STATE_ACTION_ID, collar_id)
    record_index = watermark.get("record_index", -1)
    stored_latest = watermark.get("latest_timestamp")

    min_date = datetime.now(tz=timezone.utc) - timedelta(days=action_config.lookback_days)
    checkpointed_index = record_index
    newest_record = None
    fresh_found = 0
    observations_sent = 0

    async for records, record_index in client.iter_collar_pages(
        base_url=base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
        collar_id=collar_id,
        record_index=record_index,
    ):
        if records:
            page_newest = max(records, key=lambda record: record.recorded_at)
            if newest_record is None or page_newest.recorded_at > newest_record.recorded_at:
                newest_record = page_newest
            fresh_records = [record for record in records if record.recorded_at >= min_date]
            fresh_found += len(fresh_records)
            observations = [_transform(collar_id, action_config.subject_type, record) for record in fresh_records]
            for batch in generate_batches(observations, GUNDI_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)
                observations_sent += len(batch)
        if records or record_index != checkpointed_index:
            # Checkpoint after every page so a backfill interrupted by the
            # runner's execution timeout resumes where it left off.
            await state_manager.set_state(
                integration_id,
                WATERMARK_STATE_ACTION_ID,
                {
                    "record_index": record_index,
                    "latest_timestamp": newest_record.recorded_at.isoformat() if newest_record else stored_latest,
                },
                collar_id,
            )
            checkpointed_index = record_index

    observations_extracted = fresh_found
    if newest_record and not fresh_found:
        # All records are older than the lookback window: still send the newest
        # one so the collar's last known position stays current downstream.
        await send_observations_to_gundi(
            observations=[_transform(collar_id, action_config.subject_type, newest_record)],
            integration_id=integration_id,
        )
        observations_sent += 1
        observations_extracted = 1

    # The stored timestamp covers runs that fetch nothing at all, so a dormant
    # collar re-arms its backoff after each TTL expiry instead of being polled
    # at the full 5-minute cadence forever.
    latest_recorded_at = newest_record.recorded_at if newest_record else _parse_stored_timestamp(stored_latest)
    if latest_recorded_at and latest_recorded_at < min_date:
        await state_manager.set_if_absent(
            integration_id,
            BACKOFF_STATE_ACTION_ID,
            ttl_seconds=random.randint(*BACKOFF_TTL_RANGE_SECONDS),
            source_id=collar_id,
        )

    return {
        "collar_id": collar_id,
        "observations_extracted": observations_extracted,
        "observations_sent": observations_sent,
    }
