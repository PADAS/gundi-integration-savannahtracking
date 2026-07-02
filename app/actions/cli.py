"""
Standalone CLI to fetch data from the Savannah Tracking API.

Usage:
    python -m app.actions.cli collars
    python -m app.actions.cli data IRI2016-4756 --lookback-days 3
    python -m app.actions.cli data IRI2016-4756 | jq .latitude

Credentials come from --username/--password, the ST_USERNAME/ST_PASSWORD
environment variables, or an interactive prompt, in that order.
"""
import argparse
import asyncio
import getpass
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

from . import client


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.actions.cli",
        description="Fetch data from the Savannah Tracking API.",
    )
    parser.add_argument("--username", "-u", default=None, help="Username (default: $ST_USERNAME)")
    parser.add_argument("--password", "-p", default=None, help="Password (default: $ST_PASSWORD)")
    parser.add_argument(
        "--base-url", default=client.DEFAULT_API_BASE_URL,
        help=f"API base URL (default: {client.DEFAULT_API_BASE_URL})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("collars", help="List available collar IDs, one per line")
    data_parser = subparsers.add_parser("data", help="Fetch records for a collar as JSON lines")
    data_parser.add_argument("collar_id", help="Collar ID, as listed by the collars command")
    data_parser.add_argument(
        "--record-index", type=int, default=-1,
        help="Start fetching after this record index (default: -1, the full history)",
    )
    data_parser.add_argument(
        "--lookback-days", type=int, default=None,
        help="Only print records newer than this many days (default: no filter)",
    )
    return parser


def resolve_credentials(args) -> tuple:
    username = args.username or os.environ.get("ST_USERNAME") or input("Savannah Tracking username? ")
    password = args.password or os.environ.get("ST_PASSWORD") or getpass.getpass("Savannah Tracking password? ")
    return username, password


async def run_collars(*, base_url: str, username: str, password: str):
    collar_ids = await client.get_collar_list(base_url=base_url, username=username, password=password)
    for collar_id in collar_ids:
        print(collar_id)


async def run_data(
        *, base_url: str, username: str, password: str,
        collar_id: str, record_index: int, lookback_days: int = None,
):
    min_date = None
    if lookback_days is not None:
        min_date = datetime.now(tz=timezone.utc) - timedelta(days=lookback_days)

    has_more_records = True
    while has_more_records:
        page, has_more_records = await client.get_collar_data_page(
            base_url=base_url,
            username=username,
            password=password,
            collar_id=collar_id,
            record_index=record_index,
        )
        if page:
            record_index = max(record.record_index for record in page)
        elif has_more_records:
            print(f"Got a page with no parseable records for collar {collar_id}. Stopping.", file=sys.stderr)
            break
        for record in page:
            if min_date and record.recorded_at < min_date:
                continue
            print(record.json())


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    username, password = resolve_credentials(args)
    base_url = args.base_url.rstrip("/")
    try:
        if args.command == "collars":
            asyncio.run(run_collars(base_url=base_url, username=username, password=password))
        else:
            asyncio.run(run_data(
                base_url=base_url,
                username=username,
                password=password,
                collar_id=args.collar_id,
                record_index=args.record_index,
                lookback_days=args.lookback_days,
            ))
    except client.SavannahBadCredentialsException as e:
        print(f"Authentication failed: {e}", file=sys.stderr)
        return 2
    except httpx.HTTPError as e:
        print(f"Request failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
