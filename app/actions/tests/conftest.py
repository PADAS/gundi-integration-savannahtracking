import pytest
from gundi_core.schemas.v2 import Integration

from app.conftest import async_return


@pytest.fixture
def savannah_integration_as_dict():
    return {
        "id": "a1b2c3d4-5589-4f4c-9e0a-ae8d6c9edff0",
        "name": "Savannah Tracking Provider",
        "base_url": "https://api.savannahtracking.co.ke",
        "enabled": True,
        "type": {
            "id": "50229e21-a9fe-4caa-862c-8592dfb2479c",
            "name": "Savannah Tracking",
            "value": "savannah_tracking",
            "description": "Integration type for Savannah Tracking",
            "actions": [
                {
                    "id": "80448d1c-4696-4b32-a59f-f3494fc949ad",
                    "type": "auth",
                    "name": "Authenticate",
                    "value": "auth",
                    "description": "Authenticate against the Savannah Tracking API",
                    "schema": {},
                },
                {
                    "id": "75b3040f-ab1f-42e7-b39f-8965c088b155",
                    "type": "pull",
                    "name": "Pull Observations",
                    "value": "read_observations",
                    "description": "Extract observations from the Savannah Tracking API",
                    "schema": {},
                },
            ],
        },
        "owner": {
            "id": "a91b400b-482a-4546-8fcb-ee42b01deeb6",
            "name": "Test Org",
            "description": "",
        },
        "configurations": [
            {
                "id": "30f8878c-4a98-4c95-88eb-79f73c40fb2e",
                "integration": "a1b2c3d4-5589-4f4c-9e0a-ae8d6c9edff0",
                "action": {
                    "id": "80448d1c-4696-4b32-a59f-f3494fc949ad",
                    "type": "auth",
                    "name": "Authenticate",
                    "value": "auth",
                },
                "data": {"username": "testuser", "password": "testpassword"},
            },
            {
                "id": "5577c323-b961-4277-9047-b1f27fd6a1b8",
                "integration": "a1b2c3d4-5589-4f4c-9e0a-ae8d6c9edff0",
                "action": {
                    "id": "75b3040f-ab1f-42e7-b39f-8965c088b155",
                    "type": "pull",
                    "name": "Pull Observations",
                    "value": "read_observations",
                },
                "data": {"lookback_days": 3},
            },
        ],
        "additional": {},
        "default_route": None,
        "status": "healthy",
        "status_details": "",
    }


@pytest.fixture
def savannah_integration(savannah_integration_as_dict):
    return Integration.parse_obj(savannah_integration_as_dict)


@pytest.fixture
def savannah_integration_without_auth(savannah_integration_as_dict):
    savannah_integration_as_dict["configurations"] = [
        config for config in savannah_integration_as_dict["configurations"]
        if config["action"]["value"] != "auth"
    ]
    return Integration.parse_obj(savannah_integration_as_dict)


@pytest.fixture
def mock_state_manager_empty(mocker):
    mock_state_manager = mocker.MagicMock()
    mock_state_manager.get_state.return_value = async_return({})
    mock_state_manager.set_state.return_value = async_return(None)
    mock_state_manager.set_if_absent.return_value = async_return(True)
    return mock_state_manager


@pytest.fixture
def mock_publish_event(mocker):
    return mocker.patch("app.services.activity_logger.publish_event", return_value=async_return(None))
