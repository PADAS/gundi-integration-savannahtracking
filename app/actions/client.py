import logging
from datetime import datetime, timezone
from typing import List, Optional, Tuple

import httpx
import pydantic

logger = logging.getLogger(__name__)


DEFAULT_API_BASE_URL = "https://api.savannahtracking.co.ke"
DATA_AUTH_ENDPOINT = "/savannah_data/data_auth"
DATA_REQUEST_ENDPOINT = "/savannah_data/data_request"
DEFAULT_TIMEOUT = httpx.Timeout(20.0, connect=3.1)
RECORD_TIME_FORMAT = "%m/%d/%Y %I:%M:%S %p"


class SavannahApiException(Exception):
    pass


class SavannahBadCredentialsException(SavannahApiException):
    pass


class SavannahRecord(pydantic.BaseModel):
    record_index: int
    record_time: datetime
    latitude: float
    longitude: float
    speed: Optional[float] = None
    heading: Optional[float] = None
    temperature: Optional[float] = None
    h_accuracy: Optional[float] = None
    hdop: Optional[float] = None
    battery: Optional[float] = None

    @pydantic.validator("record_time", pre=True)
    def parse_us_style_timestamp(cls, value):
        # The API returns US-style timestamps like "5/14/2023 3:43:10 PM"
        if isinstance(value, str):
            try:
                return datetime.strptime(value.strip(), RECORD_TIME_FORMAT)
            except ValueError:
                pass  # Fall through to pydantic's default parsing (e.g. ISO strings)
        return value

    @pydantic.validator("record_time")
    def assume_utc(cls, value):
        # The API returns naive timestamps which are in UTC
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def recorded_at(self) -> datetime:
        return self.record_time


async def get_collar_list(*, base_url: str, username: str, password: str) -> List[str]:
    """
    Authenticate against the Savannah Tracking API.
    Returns the list of collar IDs available to this account.
    """
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        response = await session.post(
            f"{base_url}{DATA_AUTH_ENDPOINT}",
            data={
                "request": "authenticate",
                "uid": username,
                "pwd": password,
            },
        )
    response.raise_for_status()
    result = response.json()
    # Notice: "sucess" (sic) is how the API spells it
    if not result.get("sucess"):
        raise SavannahBadCredentialsException(
            result.get("login_error_msg") or "Savannah Tracking login was not successful."
        )
    return result["records"]


async def get_collar_data_page(
    *, base_url: str, username: str, password: str, collar_id: str, record_index: int
) -> Tuple[List[SavannahRecord], bool]:
    """
    Fetch one page of position records for a collar, starting after record_index.
    Returns the parsed records and whether more pages are available.
    """
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT) as session:
        response = await session.post(
            f"{base_url}{DATA_REQUEST_ENDPOINT}",
            data={
                "request": "data_download",
                "uid": username,
                "pwd": password,
                "collar": collar_id,
                "record_index": record_index,
            },
        )
    response.raise_for_status()
    result = response.json()
    records = []
    for raw_record in result.get("records") or []:
        try:
            records.append(SavannahRecord.parse_obj(raw_record))
        except pydantic.ValidationError as e:
            logger.warning(f"Skipping invalid record for collar {collar_id}: {e}. Record: {raw_record}")
    return records, bool(result.get("has_more_records"))
