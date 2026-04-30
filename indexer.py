"""
Indexer for collecting unique borrower addresses for the active lending protocol.

Protocol modes:
1. Aave / Radiant: scan Borrow events from the pool contract
2. Lodestar: scan Borrow events across the configured lToken contracts
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from protocol_config import get_active_protocol_config
from requests.exceptions import HTTPError
from web3 import HTTPProvider, Web3
from web3.exceptions import Web3Exception


LOGGER = logging.getLogger(__name__)

LOOKBACK_BLOCKS = 100
MAX_LOOKBACK = 100
HISTORICAL_BATCH_SIZE = 10
INCREMENTAL_BATCH_SIZE = 10
MAX_WORKERS = 1
OUTPUT_FILE = "users.json"
STATE_FILE = "state.json"

AAVE_BORROW_EVENT_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "reserve", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "onBehalfOf", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
            {"indexed": False, "internalType": "uint8", "name": "interestRateMode", "type": "uint8"},
            {"indexed": False, "internalType": "uint256", "name": "borrowRate", "type": "uint256"},
            {"indexed": True, "internalType": "uint16", "name": "referralCode", "type": "uint16"},
        ],
        "name": "Borrow",
        "type": "event",
    }
]

RADIANT_BORROW_EVENT_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "reserve", "type": "address"},
            {"indexed": False, "internalType": "address", "name": "user", "type": "address"},
            {"indexed": True, "internalType": "address", "name": "onBehalfOf", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "amount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "borrowRateMode", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "borrowRate", "type": "uint256"},
            {"indexed": True, "internalType": "uint16", "name": "referral", "type": "uint16"},
        ],
        "name": "Borrow",
        "type": "event",
    }
]

LODESTAR_BORROW_EVENT_ABI: list[dict[str, Any]] = [
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "internalType": "address", "name": "borrower", "type": "address"},
            {"indexed": False, "internalType": "uint256", "name": "borrowAmount", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "accountBorrows", "type": "uint256"},
            {"indexed": False, "internalType": "uint256", "name": "totalBorrows", "type": "uint256"},
        ],
        "name": "Borrow",
        "type": "event",
    }
]


def _state_key(protocol_name: str) -> str:
    return f"{protocol_name}_last_indexed_block"


def _get_borrow_event_abi(protocol_family: str) -> list[dict[str, Any]]:
    if protocol_family == "compound_v2":
        return LODESTAR_BORROW_EVENT_ABI
    if protocol_family == "aave_v2_like":
        return RADIANT_BORROW_EVENT_ABI
    return AAVE_BORROW_EVENT_ABI


def _get_borrower_arg_name(protocol_family: str) -> str:
    if protocol_family == "compound_v2":
        return "borrower"
    return "user"


def load_config() -> tuple[str, str]:
    load_dotenv()
    protocol = get_active_protocol_config()
    rpc_url = os.getenv("HTTP_RPC_URL")

    if not rpc_url:
        raise ValueError("HTTP_RPC_URL is not set in .env")

    return rpc_url, Web3.to_checksum_address(protocol.pool_address)


def create_web3(rpc_url: str, timeout: int = 60) -> Web3:
    web3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    if not web3.is_connected():
        raise ConnectionError(f"Could not connect to RPC provider: {rpc_url}")
    return web3


def load_existing_users(path: str | Path) -> set[str]:
    output_path = Path(path)
    if not output_path.exists():
        return set()

    with output_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(f"{output_path} must contain a JSON array of addresses")

    return {Web3.to_checksum_address(address) for address in data}


def save_users(users: set[str], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(sorted(users), file, indent=2)
        file.write("\n")


def load_state(path: str | Path = STATE_FILE) -> dict[str, int]:
    state_path = Path(path)
    if not state_path.exists():
        return {}

    with state_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, dict):
        raise ValueError(f"{state_path} must contain a JSON object")

    return {str(key): int(value) for key, value in data.items()}


def save_state(state: dict[str, int], path: str | Path = STATE_FILE) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)

    with state_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
        file.write("\n")


def fetch_logs_with_retries(
    event_type: Any,
    from_block: int,
    to_block: int,
    max_retries: int = 3,
    retry_sleep: float = 2.0,
):
    from_block_int = int(from_block)
    to_block_int = int(to_block)

    for attempt in range(1, max_retries + 1):
        try:
            return event_type.get_logs(from_block=from_block_int, to_block=to_block_int)
        except (Web3Exception, HTTPError, Exception) as exc:
            error_details = str(exc)
            response_text = ""
            
            if hasattr(exc, "response") and exc.response is not None:
                response_text = exc.response.text
                error_details += f" | Response Body: {response_text}"
            
            if attempt == max_retries:
                LOGGER.error("Final RPC error after %s attempts: %s", max_retries, error_details)
                raise

            sleep_for = retry_sleep * attempt
            # If it's a rate limit, sleep longer
            if "429" in error_details or "rate limit" in error_details.lower():
                sleep_for = max(sleep_for, 5.0 * attempt)

            LOGGER.warning(
                "RPC error on blocks %s-%s, attempt %s/%s: %s",
                from_block_int,
                to_block_int,
                attempt,
                max_retries,
                error_details,
            )
            time.sleep(sleep_for)

    return []


def fetch_logs_range(
    event_type: Any,
    from_block: int,
    to_block: int,
    chunk_size: int,
):
    del chunk_size
    return fetch_logs_with_retries(
        event_type=event_type,
        from_block=from_block,
        to_block=to_block,
    )


def scan_borrowers(
    web3: Web3,
    pool_address: str,
    lookback_blocks: int = LOOKBACK_BLOCKS,
    batch_size: int | None = None,
    max_workers: int = MAX_WORKERS,
    output_file: str = OUTPUT_FILE,
    state_file: str = STATE_FILE,
) -> set[str]:
    del pool_address

    protocol = get_active_protocol_config()
    state = load_state(state_file)
    state_key = _state_key(protocol.name)
    
    # Get current block number once and ensure it's an int
    end_block = int(web3.eth.block_number)
    
    # Calculate start block: max(latest - 100, last_indexed)
    checkpoint_block = state.get(state_key)
    if checkpoint_block is not None:
        start_block = int(checkpoint_block)
    else:
        start_block = max(0, end_block - MAX_LOOKBACK)
    
    # Also enforce MAX_LOOKBACK for sanity (optional, but requested)
    start_block = max(start_block, end_block - MAX_LOOKBACK)

    has_existing_checkpoint = state_key in state

    effective_batch_size = (
        batch_size
        if batch_size is not None
        else (INCREMENTAL_BATCH_SIZE if has_existing_checkpoint else HISTORICAL_BATCH_SIZE)
    )

    if start_block > end_block:
        LOGGER.info("Indexer is up to date. %s=%s latest_block=%s", state_key, start_block, end_block)
        return load_existing_users(output_file)

    event_abi = _get_borrow_event_abi(protocol.protocol_family)
    borrower_arg_name = _get_borrower_arg_name(protocol.protocol_family)
    users = load_existing_users(output_file)
    active_markets = [Web3.to_checksum_address(address) for address in protocol.borrow_event_addresses]
    
    event_contracts = {
        address: web3.eth.contract(address=address, abi=event_abi)
        for address in active_markets
    }

    LOGGER.info("HTTP RPC connected successfully. Latest block number: %s", end_block)
    LOGGER.info(
        "Scanning Borrow logs for %s from %s to %s across %s market(s) with chunk_size=%s (%s mode)",
        protocol.name,
        start_block,
        end_block,
        len(active_markets),
        effective_batch_size,
        "incremental" if has_existing_checkpoint else "historical",
    )

    for market_address in active_markets:
        LOGGER.info("Processing market: %s", market_address)
        event_type = event_contracts[market_address].events.Borrow
        
        # We process each market sequentially in chunks
        for from_block in range(start_block, end_block + 1, effective_batch_size):
            to_block = min(from_block + effective_batch_size - 1, end_block)
            
            logs = fetch_logs_range(
                event_type,
                from_block,
                to_block,
                effective_batch_size,
            )
            
            batch_new_users = 0
            for log in logs:
                try:
                    user = Web3.to_checksum_address(log["args"][borrower_arg_name])
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning(
                        "Could not process Borrow log for %s in block %s: %s",
                        market_address,
                        log.get("blockNumber"),
                        exc,
                    )
                    continue

                if user not in users:
                    users.add(user)
                    batch_new_users += 1

            if batch_new_users > 0:
                save_users(users, output_file)

            LOGGER.info(
                "Market: %s | Range: %s-%s | New: %s | Total users: %s | Logs: %s",
                market_address,
                from_block,
                to_block,
                batch_new_users,
                len(users),
                len(logs),
            )

    save_users(users, output_file)
    state[state_key] = end_block + 1
    save_state(state, state_file)
    return users


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rpc_url, pool_address = load_config()
    web3 = create_web3(rpc_url)
    users = scan_borrowers(web3=web3, pool_address=pool_address)
    LOGGER.info("Done. Stored %s unique borrowers in %s", len(users), OUTPUT_FILE)


if __name__ == "__main__":
    main()
