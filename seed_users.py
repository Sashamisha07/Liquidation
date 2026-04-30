"""
seed_users.py — Dust Collection bootstrap tool.

Downloads up to TARGET_COUNT unique borrower addresses from the
official Aave V3 Arbitrum Subgraph (The Graph) and saves them to
users.json in the format expected by indexer.py / monitor.py.

The script fetches users whose *current* borrow balance is > 0,
using cursor-based pagination so it never misses any records.

Usage:
    python seed_users.py                    # fetch up to 2000 users
    python seed_users.py --count 500        # fetch up to 500 users
    python seed_users.py --output my.json   # custom output file
    python seed_users.py --dry-run          # print addresses, don't save

Env vars (optional, from .env):
    GRAPH_API_KEY   — The Graph hosted-service API key (leave empty for free endpoint)
    MIN_DEBT_USD    — Minimum debt in USD to include a user (default: 0, i.e. any debt > 0)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOGGER = logging.getLogger(__name__)

# Official Aave V3 Arbitrum Subgraph (The Graph decentralised network).
# Falls back to the free hosted-service endpoint if no API key is provided.
_API_KEY = os.getenv("GRAPH_API_KEY", "")

# Decentralised network endpoint (requires API key):
_GRAPH_NETWORK_URL = (
    f"https://gateway.thegraph.com/api/{_API_KEY}/subgraphs/id/"
    "GQFbb95cE6d8mV989mL5figjaGaKCQB3xqYrr1bRyXqF"  # Aave V3 Arbitrum
)

# Free hosted-service endpoint (no key needed, may have rate limits):
_GRAPH_HOSTED_URL = (
    "https://api.thegraph.com/subgraphs/name/aave/protocol-v3-arbitrum"
)

GRAPH_URL: str = _GRAPH_NETWORK_URL if _API_KEY else _GRAPH_HOSTED_URL

# Output file — same path as the one indexer.py / monitor.py uses.
OUTPUT_FILE = "users.json"

# How many users we aim to collect.
TARGET_COUNT = 2_000

# The Graph paginates in pages of at most 1000 records.
PAGE_SIZE = 1_000

# Seconds to sleep between pages to avoid rate-limiting.
PAGE_SLEEP = 0.5

# Retry configuration for HTTP errors.
MAX_RETRIES = 5
RETRY_SLEEP = 3.0

# ---------------------------------------------------------------------------
# GraphQL query
# ---------------------------------------------------------------------------

# We iterate over users with borrows using cursor-based pagination on `id`.
# The condition `borrowedReservesCount_gt: 0` ensures only active borrowers
# are returned; this is indexed and fast on the Aave subgraph.
QUERY_TEMPLATE = """
query FetchBorrowers($first: Int!, $lastId: String!) {
  users(
    first: $first
    where: {
      borrowedReservesCount_gt: 0
      id_gt: $lastId
    }
    orderBy: id
    orderDirection: asc
  ) {
    id
    borrowedReservesCount
  }
}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post_graphql(payload: dict[str, Any], session: requests.Session) -> dict[str, Any]:
    """Send a GraphQL request with retry logic."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.post(
                GRAPH_URL,
                json=payload,
                timeout=30,
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                raise ValueError(f"GraphQL errors: {data['errors']}")
            return data
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES:
                LOGGER.error("Graph API request failed after %d attempts: %s", MAX_RETRIES, exc)
                raise
            sleep = RETRY_SLEEP * attempt
            LOGGER.warning(
                "Graph API error (attempt %d/%d): %s — retrying in %.1fs",
                attempt, MAX_RETRIES, exc, sleep,
            )
            time.sleep(sleep)

    return {}  # unreachable, but keeps mypy happy


def fetch_borrowers(target: int, session: requests.Session) -> list[str]:
    """Paginate through the subgraph and return up to `target` borrower addresses."""
    addresses: list[str] = []
    last_id = ""
    page = 0

    LOGGER.info("Fetching up to %d borrowers from The Graph (%s) …", target, GRAPH_URL)

    while len(addresses) < target:
        page += 1
        fetch_size = min(PAGE_SIZE, target - len(addresses))

        payload = {
            "query": QUERY_TEMPLATE,
            "variables": {"first": fetch_size, "lastId": last_id},
        }

        LOGGER.info("Page %d — requesting %d users (cursor: %s) …", page, fetch_size, last_id or "<start>")
        result = _post_graphql(payload, session)
        users_page: list[dict[str, Any]] = result.get("data", {}).get("users", [])

        if not users_page:
            LOGGER.info("No more users returned — subgraph exhausted at page %d.", page)
            break

        for user in users_page:
            uid = user.get("id", "").strip().lower()
            if uid:
                # Normalise to checksummed address format
                addresses.append(uid)

        last_id = users_page[-1]["id"]
        LOGGER.info(
            "Page %d done — got %d users, total so far: %d",
            page, len(users_page), len(addresses),
        )

        if len(users_page) < fetch_size:
            # Subgraph returned fewer than requested — we've reached the end.
            break

        time.sleep(PAGE_SLEEP)

    return addresses


def load_existing_users(path: Path) -> list[str]:
    """Load existing users.json to avoid overwriting good data."""
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        LOGGER.warning("%s is not a JSON array — ignoring existing data.", path)
        return []
    return [addr.strip().lower() for addr in data if isinstance(addr, str)]


def save_users(addresses: list[str], path: Path) -> None:
    """Save a deduplicated, sorted list of addresses to JSON."""
    unique = sorted(set(addresses))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(unique, f, indent=2)
        f.write("\n")
    LOGGER.info("Saved %d unique addresses to %s", len(unique), path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed users.json with active Aave V3 Arbitrum borrowers via The Graph.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--count", "-n",
        type=int,
        default=TARGET_COUNT,
        help="Maximum number of borrower addresses to fetch.",
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=OUTPUT_FILE,
        help="Path to the output JSON file.",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        default=True,
        help="Merge fetched addresses with existing users.json (default: True).",
    )
    parser.add_argument(
        "--no-merge",
        dest="merge",
        action="store_false",
        help="Overwrite existing users.json instead of merging.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Fetch and print addresses but do NOT write to disk.",
    )
    parser.add_argument(
        "--url",
        type=str,
        default="",
        help="Override the subgraph URL (useful for testing with a local or staging graph).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    args = parse_args()

    # Allow URL override from CLI flag
    global GRAPH_URL
    if args.url:
        GRAPH_URL = args.url
        LOGGER.info("Using custom subgraph URL: %s", GRAPH_URL)
    elif not _API_KEY:
        LOGGER.warning(
            "GRAPH_API_KEY is not set — using free hosted endpoint. "
            "Consider setting GRAPH_API_KEY in .env for higher rate limits."
        )

    output_path = Path(args.output)

    with requests.Session() as session:
        new_addresses = fetch_borrowers(target=args.count, session=session)

    if not new_addresses:
        LOGGER.error("No addresses were fetched. Check your network connection or GRAPH_API_KEY.")
        sys.exit(1)

    if args.dry_run:
        LOGGER.info("DRY-RUN: would save %d addresses (not writing to disk).", len(new_addresses))
        for addr in new_addresses[:20]:
            print(addr)
        if len(new_addresses) > 20:
            print(f"  … and {len(new_addresses) - 20} more")
        return

    # Merge with existing list if requested
    if args.merge:
        existing = load_existing_users(output_path)
        LOGGER.info("Merging %d new with %d existing addresses.", len(new_addresses), len(existing))
        combined = existing + new_addresses
    else:
        combined = new_addresses

    save_users(combined, output_path)

    # Summary
    unique_count = len(set(combined))
    print()
    print("=" * 60)
    print("  Aave V3 Arbitrum — Dust Collection Seed Complete")
    print("=" * 60)
    print(f"  Fetched   : {len(new_addresses):>6} addresses")
    print(f"  Unique    : {unique_count:>6} addresses (after merge)")
    print(f"  Saved to  : {output_path.resolve()}")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
