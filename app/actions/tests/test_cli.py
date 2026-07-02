import json
from datetime import datetime, timedelta, timezone

import pytest

from app.actions import cli, client
from app.conftest import AsyncMock


def make_record(record_index: int, recorded_at: datetime) -> client.SavannahRecord:
    return client.SavannahRecord(
        record_index=record_index,
        record_time=recorded_at,
        latitude=-1.2921,
        longitude=36.8219,
        battery=3.9,
    )


@pytest.fixture(autouse=True)
def credentials_env(monkeypatch):
    monkeypatch.setenv("ST_USERNAME", "testuser")
    monkeypatch.setenv("ST_PASSWORD", "testpassword")


def test_collars_command(mocker, capsys):
    mock_get_collar_list = AsyncMock(return_value=["ST2010-3034", "IRI2016-4756"])
    mocker.patch("app.actions.cli.client.get_collar_list", mock_get_collar_list)

    exit_code = cli.main(["collars"])

    assert exit_code == 0
    assert capsys.readouterr().out == "ST2010-3034\nIRI2016-4756\n"
    call_kwargs = mock_get_collar_list.call_args.kwargs
    assert call_kwargs["username"] == "testuser"
    assert call_kwargs["password"] == "testpassword"
    assert call_kwargs["base_url"] == client.DEFAULT_API_BASE_URL


def test_data_command_paginates_and_prints_json_lines(mocker, capsys):
    now = datetime.now(tz=timezone.utc)
    page_one = [make_record(101, now - timedelta(hours=2))]
    page_two = [make_record(102, now - timedelta(hours=1))]
    mock_get_collar_data_page = AsyncMock(side_effect=[(page_one, True, 101), (page_two, False, 102)])
    mocker.patch("app.actions.cli.client.get_collar_data_page", mock_get_collar_data_page)

    exit_code = cli.main(["data", "ST2010-3034"])

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    records = [json.loads(line) for line in lines]
    assert [r["record_index"] for r in records] == [101, 102]
    assert records[0]["latitude"] == -1.2921
    # Pagination starts from the default index and resumes from the highest seen
    assert mock_get_collar_data_page.call_args_list[0].kwargs["record_index"] == -1
    assert mock_get_collar_data_page.call_args_list[1].kwargs["record_index"] == 101
    assert mock_get_collar_data_page.call_args_list[0].kwargs["collar_id"] == "ST2010-3034"


def test_data_command_with_record_index(mocker, capsys):
    now = datetime.now(tz=timezone.utc)
    mock_get_collar_data_page = AsyncMock(return_value=([make_record(501, now)], False, 501))
    mocker.patch("app.actions.cli.client.get_collar_data_page", mock_get_collar_data_page)

    exit_code = cli.main(["data", "ST2010-3034", "--record-index", "500"])

    assert exit_code == 0
    assert mock_get_collar_data_page.call_args.kwargs["record_index"] == 500


def test_data_command_with_lookback_filter(mocker, capsys):
    now = datetime.now(tz=timezone.utc)
    records = [
        make_record(101, now - timedelta(days=10)),
        make_record(102, now - timedelta(hours=1)),
    ]
    mocker.patch(
        "app.actions.cli.client.get_collar_data_page",
        AsyncMock(return_value=(records, False, 102)),
    )

    exit_code = cli.main(["data", "ST2010-3034", "--lookback-days", "3"])

    assert exit_code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert [json.loads(line)["record_index"] for line in lines] == [102]


def test_data_command_fails_loudly_when_index_cannot_advance(mocker, capsys):
    mocker.patch(
        "app.actions.cli.client.get_collar_data_page",
        AsyncMock(return_value=([], True, None)),
    )

    exit_code = cli.main(["data", "ST2010-3034"])

    assert exit_code == 1
    assert "no record_index to advance on" in capsys.readouterr().err


def test_bad_credentials_exit_code(mocker, capsys):
    mocker.patch(
        "app.actions.cli.client.get_collar_list",
        AsyncMock(side_effect=client.SavannahBadCredentialsException("Invalid username or password")),
    )

    exit_code = cli.main(["collars"])

    assert exit_code == 2
    assert "Authentication failed" in capsys.readouterr().err
