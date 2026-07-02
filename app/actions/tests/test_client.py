import httpx
import pytest
import respx

from app.actions import client


BASE_URL = "https://api.savannahtracking.co.ke"


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_list_success():
    route = respx.post(f"{BASE_URL}{client.DATA_AUTH_ENDPOINT}").respond(
        json={"sucess": True, "records": ["ST2010-3034", "ST2010-3035"]}
    )

    collar_ids = await client.get_collar_list(
        base_url=BASE_URL, username="testuser", password="testpassword"
    )

    assert collar_ids == ["ST2010-3034", "ST2010-3035"]
    request = route.calls.last.request
    assert b"request=authenticate" in request.content
    assert b"uid=testuser" in request.content
    assert b"pwd=testpassword" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_list_with_bad_credentials():
    respx.post(f"{BASE_URL}{client.DATA_AUTH_ENDPOINT}").respond(
        json={"sucess": False, "login_error_msg": "Invalid username or password"}
    )

    with pytest.raises(client.SavannahBadCredentialsException, match="Invalid username or password"):
        await client.get_collar_list(
            base_url=BASE_URL, username="testuser", password="wrongpassword"
        )


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_list_with_http_error():
    respx.post(f"{BASE_URL}{client.DATA_AUTH_ENDPOINT}").respond(status_code=500)

    with pytest.raises(httpx.HTTPStatusError):
        await client.get_collar_list(
            base_url=BASE_URL, username="testuser", password="testpassword"
        )


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_data_page_success():
    route = respx.post(f"{BASE_URL}{client.DATA_REQUEST_ENDPOINT}").respond(
        json={
            "records": [
                {
                    "record_index": 101,
                    "record_time": "2026-07-01 10:00:00",
                    "latitude": -1.2921,
                    "longitude": 36.8219,
                    "speed": 1.5,
                    "heading": 90.0,
                    "temperature": 30.2,
                    "h_accuracy": 3,
                    "hdop": 0.8,
                    "battery": 3.9,
                },
                {
                    "record_index": 102,
                    "record_time": "2026-07-01 11:00:00",
                    "latitude": -1.2922,
                    "longitude": 36.8220,
                },
            ],
            "has_more_records": True,
        }
    )

    records, has_more_records = await client.get_collar_data_page(
        base_url=BASE_URL,
        username="testuser",
        password="testpassword",
        collar_id="ST2010-3034",
        record_index=100,
    )

    assert has_more_records is True
    assert [r.record_index for r in records] == [101, 102]
    # Naive timestamps are assumed to be UTC
    assert records[0].recorded_at.isoformat() == "2026-07-01T10:00:00+00:00"
    assert records[0].battery == 3.9
    assert records[1].speed is None
    request = route.calls.last.request
    assert b"request=data_download" in request.content
    assert b"collar=ST2010-3034" in request.content
    assert b"record_index=100" in request.content


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_data_page_parses_us_style_timestamps():
    # Real record shape returned by the Savannah Tracking API
    respx.post(f"{BASE_URL}{client.DATA_REQUEST_ENDPOINT}").respond(
        json={
            "records": [
                {
                    "record_index": 44022175,
                    "record_time": "5/14/2023 3:43:10 PM",
                    "time_to_fix": 0,
                    "latitude": -3.606871,
                    "longitude": 39.87716,
                    "hdop": 0,
                    "h_accuracy": 0,
                    "heading": 0,
                    "speed": 0,
                    "speed_accuracy": 0,
                    "altitude": 0,
                    "temperature": 32.4,
                    "initial_data": "",
                    "battery": 3.99,
                },
            ],
            "has_more_records": False,
        }
    )

    records, _ = await client.get_collar_data_page(
        base_url=BASE_URL,
        username="testuser",
        password="testpassword",
        collar_id="IRI2016-4756",
        record_index=44022174,
    )

    assert len(records) == 1
    assert records[0].recorded_at.isoformat() == "2023-05-14T15:43:10+00:00"
    assert records[0].battery == 3.99


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_data_page_skips_invalid_records():
    respx.post(f"{BASE_URL}{client.DATA_REQUEST_ENDPOINT}").respond(
        json={
            "records": [
                {
                    "record_index": 101,
                    "record_time": "not-a-date",
                    "latitude": -1.2921,
                    "longitude": 36.8219,
                },
                {
                    "record_index": 102,
                    "record_time": "2026-07-01 11:00:00",
                    "latitude": -1.2922,
                    "longitude": 36.8220,
                },
            ],
            "has_more_records": False,
        }
    )

    records, has_more_records = await client.get_collar_data_page(
        base_url=BASE_URL,
        username="testuser",
        password="testpassword",
        collar_id="ST2010-3034",
        record_index=100,
    )

    assert has_more_records is False
    assert [r.record_index for r in records] == [102]


@pytest.mark.asyncio
@respx.mock
async def test_get_collar_data_page_with_empty_records():
    respx.post(f"{BASE_URL}{client.DATA_REQUEST_ENDPOINT}").respond(
        json={"records": [], "has_more_records": False}
    )

    records, has_more_records = await client.get_collar_data_page(
        base_url=BASE_URL,
        username="testuser",
        password="testpassword",
        collar_id="ST2010-3034",
        record_index=-1,
    )

    assert records == []
    assert has_more_records is False
