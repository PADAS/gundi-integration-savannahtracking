from datetime import datetime, timedelta, timezone

import pytest

from app.actions import client
from app.actions.configurations import (
    CredentialsConfig,
    ReadObservationsConfig,
    ReadObservationsPerCollarConfig,
)
from app.conftest import AsyncMock, async_return
from app.services.errors import ConfigurationNotFound


def make_record(record_index: int, recorded_at: datetime) -> client.SavannahRecord:
    return client.SavannahRecord(
        record_index=record_index,
        record_time=recorded_at,
        latitude=-1.2921,
        longitude=36.8219,
        speed=1.5,
        heading=90.0,
        temperature=30.2,
        h_accuracy=3,
        hdop=0.8,
        battery=3.9,
    )


@pytest.fixture
def auth_config():
    return CredentialsConfig(username="testuser", password="testpassword")


@pytest.mark.asyncio
async def test_action_check_credentials_with_valid_credentials(mocker, savannah_integration, auth_config):
    mocker.patch(
        "app.actions.handlers.client.get_collar_list",
        AsyncMock(return_value=["ST2010-3034", "ST2010-3035"]),
    )
    from app.actions.handlers import action_check_credentials

    result = await action_check_credentials(savannah_integration, auth_config)

    assert result == {"valid_credentials": True, "collars_qty": 2}


@pytest.mark.asyncio
async def test_action_check_credentials_with_bad_credentials(mocker, savannah_integration, auth_config):
    mocker.patch(
        "app.actions.handlers.client.get_collar_list",
        AsyncMock(side_effect=client.SavannahBadCredentialsException("Invalid username or password")),
    )
    from app.actions.handlers import action_check_credentials

    result = await action_check_credentials(savannah_integration, auth_config)

    assert result["valid_credentials"] is False


@pytest.mark.asyncio
async def test_action_read_observations_triggers_subaction_per_collar(
        mocker, mock_publish_event, savannah_integration
):
    collar_ids = ["ST2010-3034", "ST2010-3035", "ST2010-3036"]
    mock_get_collar_list = AsyncMock(return_value=collar_ids)
    mocker.patch("app.actions.handlers.client.get_collar_list", mock_get_collar_list)
    mock_trigger_action = AsyncMock()
    mocker.patch("app.actions.handlers.trigger_action", mock_trigger_action)
    from app.actions.handlers import action_read_observations

    result = await action_read_observations(
        savannah_integration, ReadObservationsConfig(lookback_days=3)
    )

    assert result == {"collars_triggered": 3}
    # Credentials come from the integration's auth config
    call_kwargs = mock_get_collar_list.call_args.kwargs
    assert call_kwargs["username"] == "testuser"
    assert call_kwargs["password"] == "testpassword"
    assert call_kwargs["base_url"] == "https://api.savannahtracking.co.ke"
    assert mock_trigger_action.call_count == 3
    triggered_configs = [c.kwargs["config"] for c in mock_trigger_action.call_args_list]
    assert [config.collar_id for config in triggered_configs] == collar_ids
    assert all(config.lookback_days == 3 for config in triggered_configs)
    assert all(
        c.kwargs["action_id"] == "read_observations_per_collar"
        for c in mock_trigger_action.call_args_list
    )


@pytest.mark.asyncio
async def test_action_read_observations_without_auth_config(
        mocker, mock_publish_event, savannah_integration_without_auth
):
    from app.actions.handlers import action_read_observations

    with pytest.raises(ConfigurationNotFound):
        await action_read_observations(
            savannah_integration_without_auth, ReadObservationsConfig(lookback_days=3)
        )


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_sends_observations(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    now = datetime.now(tz=timezone.utc)
    page_one = [make_record(101, now - timedelta(hours=3)), make_record(102, now - timedelta(hours=2))]
    page_two = [make_record(103, now - timedelta(hours=1))]
    mock_get_collar_data_page = AsyncMock(side_effect=[(page_one, True), (page_two, False)])
    mocker.patch("app.actions.handlers.client.get_collar_data_page", mock_get_collar_data_page)
    mock_send_observations = AsyncMock(return_value=[{"object_id": "test"}])
    mocker.patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations)
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    result = await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    assert result["observations_extracted"] == 3
    assert result["observations_sent"] == 3
    # First page is requested from the default watermark
    assert mock_get_collar_data_page.call_args_list[0].kwargs["record_index"] == -1
    # Second page resumes from the highest index seen
    assert mock_get_collar_data_page.call_args_list[1].kwargs["record_index"] == 102
    observations = mock_send_observations.call_args.kwargs["observations"]
    assert [o["source"] for o in observations] == ["ST2010-3034"] * 3
    assert observations[0]["type"] == "tracking-device"
    assert observations[0]["location"] == {"lat": -1.2921, "lon": 36.8219}
    assert observations[0]["additional"]["record_index"] == 101
    # The watermark is persisted
    set_state_call = mock_state_manager_empty.set_state.call_args
    assert set_state_call.args[2]["record_index"] == 103
    # No backoff for an active collar
    mock_state_manager_empty.set_if_absent.assert_not_called()


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_resumes_from_watermark(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    now = datetime.now(tz=timezone.utc)
    mock_state_manager_empty.get_state.side_effect = [
        async_return({}),  # No backoff
        async_return({"record_index": 500, "latest_timestamp": now.isoformat()}),
    ]
    mock_get_collar_data_page = AsyncMock(return_value=([make_record(501, now)], False))
    mocker.patch("app.actions.handlers.client.get_collar_data_page", mock_get_collar_data_page)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    assert mock_get_collar_data_page.call_args.kwargs["record_index"] == 500


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_stops_on_unparseable_page(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    # An empty page with has_more_records=True must not re-request the same
    # index forever
    mock_get_collar_data_page = AsyncMock(return_value=([], True))
    mocker.patch("app.actions.handlers.client.get_collar_data_page", mock_get_collar_data_page)
    mock_send_observations = AsyncMock()
    mocker.patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations)
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    result = await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    assert mock_get_collar_data_page.call_count == 1
    assert result["observations_sent"] == 0
    mock_send_observations.assert_not_called()


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_skips_during_backoff(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    mock_state_manager_empty.get_state.side_effect = None
    mock_state_manager_empty.get_state.return_value = async_return(1)  # Backoff key present
    mock_get_collar_data_page = AsyncMock()
    mocker.patch("app.actions.handlers.client.get_collar_data_page", mock_get_collar_data_page)
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    result = await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    assert result["skipped"] is True
    mock_get_collar_data_page.assert_not_called()


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_with_stale_records_sets_backoff(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    now = datetime.now(tz=timezone.utc)
    stale_records = [
        make_record(101, now - timedelta(days=10)),
        make_record(102, now - timedelta(days=9)),
    ]
    mocker.patch(
        "app.actions.handlers.client.get_collar_data_page",
        AsyncMock(return_value=(stale_records, False)),
    )
    mock_send_observations = AsyncMock()
    mocker.patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations)
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    result = await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    # Only the newest record is sent to keep the last known position current
    assert result["observations_extracted"] == 1
    observations = mock_send_observations.call_args.kwargs["observations"]
    assert observations[0]["additional"]["record_index"] == 102
    # A dormant-collar backoff is set with a ~21-27h TTL
    set_if_absent_call = mock_state_manager_empty.set_if_absent.call_args
    assert 76000 <= set_if_absent_call.kwargs["ttl_seconds"] <= 96000
    assert set_if_absent_call.kwargs["source_id"] == "ST2010-3034"


@pytest.mark.asyncio
async def test_action_read_observations_per_collar_with_no_records(
        mocker, mock_publish_event, savannah_integration, mock_state_manager_empty
):
    mocker.patch(
        "app.actions.handlers.client.get_collar_data_page",
        AsyncMock(return_value=([], False)),
    )
    mock_send_observations = AsyncMock()
    mocker.patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations)
    mocker.patch("app.actions.handlers.state_manager", mock_state_manager_empty)
    from app.actions.handlers import action_read_observations_per_collar

    result = await action_read_observations_per_collar(
        savannah_integration,
        ReadObservationsPerCollarConfig(collar_id="ST2010-3034", lookback_days=3),
    )

    assert result["observations_extracted"] == 0
    assert result["observations_sent"] == 0
    mock_send_observations.assert_not_called()
    mock_state_manager_empty.set_state.assert_not_called()
    mock_state_manager_empty.set_if_absent.assert_not_called()
