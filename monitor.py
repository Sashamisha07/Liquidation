"""
Monitor for checking active protocol user risk on Arbitrum.

Protocol modes:
1. Aave / Radiant: getUserAccountData(user)
2. Lodestar: Comptroller.getAccountLiquidity(user)
"""

from __future__ import annotations

import json
import logging
import os
from decimal import Decimal, getcontext
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from protocol_config import get_active_protocol_config
from web3 import HTTPProvider, Web3


LOGGER = logging.getLogger(__name__)

USERS_FILE = "users.json"
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL_BATCH_SIZE = 100
BASE_CURRENCY_DECIMALS = 10**8
HEALTH_FACTOR_DECIMALS = 10**18
HF_ALERT_THRESHOLD = Decimal("1.05")
HOT_THRESHOLD = Decimal("1.1")
WARM_THRESHOLD = Decimal("1.3")
HOT_INTERVAL_SECONDS = 10
WARM_INTERVAL_SECONDS = 60
COLD_INTERVAL_SECONDS = 600
MAX_UINT256 = 2**256 - 1

getcontext().prec = 50

POOL_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserAccountData",
        "outputs": [
            {"internalType": "uint256", "name": "totalCollateralBase", "type": "uint256"},
            {"internalType": "uint256", "name": "totalDebtBase", "type": "uint256"},
            {"internalType": "uint256", "name": "availableBorrowsBase", "type": "uint256"},
            {"internalType": "uint256", "name": "currentLiquidationThreshold", "type": "uint256"},
            {"internalType": "uint256", "name": "ltv", "type": "uint256"},
            {"internalType": "uint256", "name": "healthFactor", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

COMPTROLLER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "getAccountLiquidity",
        "outputs": [
            {"internalType": "uint256", "name": "", "type": "uint256"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]

MULTICALL3_ABI: list[dict[str, Any]] = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "target", "type": "address"},
                    {"internalType": "bool", "name": "allowFailure", "type": "bool"},
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Call3[]",
                "name": "calls",
                "type": "tuple[]",
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"internalType": "bool", "name": "success", "type": "bool"},
                    {"internalType": "bytes", "name": "returnData", "type": "bytes"},
                ],
                "internalType": "struct Multicall3.Result[]",
                "name": "returnData",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "payable",
        "type": "function",
    }
]


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


def load_users(path: str | Path = USERS_FILE) -> list[str]:
    input_path = Path(path)
    if not input_path.exists():
        raise FileNotFoundError(f"{input_path} does not exist")

    with input_path.open("r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(f"{input_path} must contain a JSON array of addresses")

    return [Web3.to_checksum_address(address) for address in data]


def format_usd(raw_value: int) -> str:
    value = Decimal(raw_value) / Decimal(BASE_CURRENCY_DECIMALS)
    return f"{value:,.2f}"


def format_health_factor(raw_value: int) -> tuple[str, Decimal | None]:
    if raw_value == MAX_UINT256:
        return "Infinity", None

    hf = Decimal(raw_value) / Decimal(HEALTH_FACTOR_DECIMALS)
    return f"{hf:.4f}", hf


def get_monitoring_tier(health_factor_value: Decimal | None) -> str:
    if health_factor_value is None:
        return "cold"
    if health_factor_value < HOT_THRESHOLD:
        return "hot"
    if health_factor_value < WARM_THRESHOLD:
        return "warm"
    return "cold"


def get_monitoring_interval(health_factor_value: Decimal | None) -> int:
    tier = get_monitoring_tier(health_factor_value)
    if tier == "hot":
        return HOT_INTERVAL_SECONDS
    if tier == "warm":
        return WARM_INTERVAL_SECONDS
    return COLD_INTERVAL_SECONDS


def _decode_aave_account_data(web3: Web3, encoded: bytes) -> tuple[int, int, int, int, int, int]:
    decoded = web3.codec.decode(
        ["uint256", "uint256", "uint256", "uint256", "uint256", "uint256"],
        encoded,
    )
    return tuple(int(value) for value in decoded)  # type: ignore[return-value]


def _decode_lodestar_account_data(web3: Web3, encoded: bytes) -> tuple[int, int, int]:
    decoded = web3.codec.decode(["uint256", "uint256", "uint256"], encoded)
    return tuple(int(value) for value in decoded)  # type: ignore[return-value]


def _build_aave_status(user: str, account_data: tuple[int, int, int, int, int, int]) -> dict[str, object]:
    total_collateral_base = int(account_data[0])
    total_debt_base = int(account_data[1])
    health_factor_raw = int(account_data[5])
    health_factor_str, health_factor_value = format_health_factor(health_factor_raw)

    return {
        "address": Web3.to_checksum_address(user),
        "protocol": get_active_protocol_config().name,
        "total_collateral_base": total_collateral_base,
        "total_debt_base": total_debt_base,
        "collateral_usd": Decimal(total_collateral_base) / Decimal(BASE_CURRENCY_DECIMALS),
        "debt_usd": Decimal(total_debt_base) / Decimal(BASE_CURRENCY_DECIMALS),
        "health_factor_raw": health_factor_raw,
        "health_factor": health_factor_str,
        "health_factor_value": health_factor_value,
        "shortfall_raw": 0,
        "tier": get_monitoring_tier(health_factor_value),
        "next_interval_seconds": get_monitoring_interval(health_factor_value),
    }


def _build_lodestar_status(user: str, account_data: tuple[int, int, int]) -> dict[str, object]:
    error_code = int(account_data[0])
    liquidity = int(account_data[1])
    shortfall = int(account_data[2])
    health_factor_value = Decimal("0") if shortfall > 0 else None

    return {
        "address": Web3.to_checksum_address(user),
        "protocol": get_active_protocol_config().name,
        "error_code": error_code,
        "liquidity_raw": liquidity,
        "shortfall_raw": shortfall,
        "total_collateral_base": 0,
        "total_debt_base": 0,
        "health_factor_raw": 0 if shortfall > 0 else MAX_UINT256,
        "health_factor": "SHORTFALL" if shortfall > 0 else "SAFE",
        "health_factor_value": health_factor_value,
        "tier": "hot" if shortfall > 0 else "cold",
        "next_interval_seconds": HOT_INTERVAL_SECONDS if shortfall > 0 else COLD_INTERVAL_SECONDS,
    }


def get_user_status(web3: Web3, pool_address: str, user: str) -> dict[str, object]:
    protocol = get_active_protocol_config()
    target_address = Web3.to_checksum_address(pool_address)
    checksum_user = Web3.to_checksum_address(user)

    if protocol.protocol_family == "compound_v2":
        contract = web3.eth.contract(address=target_address, abi=COMPTROLLER_ABI)
        account_data = contract.functions.getAccountLiquidity(checksum_user).call()
        return _build_lodestar_status(user, tuple(int(value) for value in account_data))

    contract = web3.eth.contract(address=target_address, abi=POOL_ABI)
    account_data = contract.functions.getUserAccountData(checksum_user).call()
    return _build_aave_status(user, tuple(int(value) for value in account_data))


def scan_user_statuses(web3: Web3, pool_address: str, users: list[str]) -> list[dict[str, object]]:
    if not users:
        return []

    protocol = get_active_protocol_config()
    target_address = Web3.to_checksum_address(pool_address)
    multicall = web3.eth.contract(address=Web3.to_checksum_address(MULTICALL3_ADDRESS), abi=MULTICALL3_ABI)
    results: list[dict[str, object]] = []

    if protocol.protocol_family == "compound_v2":
        target_contract = web3.eth.contract(address=target_address, abi=COMPTROLLER_ABI)
        encode_method = "getAccountLiquidity"
    else:
        target_contract = web3.eth.contract(address=target_address, abi=POOL_ABI)
        encode_method = "getUserAccountData"

    for start in range(0, len(users), MULTICALL_BATCH_SIZE):
        batch = users[start : start + MULTICALL_BATCH_SIZE]
        calls = [
            (
                target_address,
                True,
                target_contract.encode_abi(encode_method, args=[Web3.to_checksum_address(user)]),
            )
            for user in batch
        ]

        try:
            batch_results = multicall.functions.aggregate3(calls).call()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Multicall batch failed for users %s-%s: %s", start, start + len(batch) - 1, exc)
            for user in batch:
                try:
                    results.append(get_user_status(web3, target_address, user))
                except Exception as inner_exc:  # noqa: BLE001
                    LOGGER.error("User %s | RPC error: %s", user, inner_exc)
            continue

        for user, result in zip(batch, batch_results, strict=False):
            success = bool(result[0])
            return_data = bytes(result[1])
            if not success:
                LOGGER.warning("Multicall returned failure for user %s", user)
                continue

            try:
                if protocol.protocol_family == "compound_v2":
                    account_data = _decode_lodestar_account_data(web3, return_data)
                    results.append(_build_lodestar_status(user, account_data))
                else:
                    account_data = _decode_aave_account_data(web3, return_data)
                    results.append(_build_aave_status(user, account_data))
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Could not decode account data for user %s: %s", user, exc)

    return results


def monitor_users(web3: Web3, pool_address: str, users: list[str]) -> None:
    protocol = get_active_protocol_config()

    LOGGER.info("HTTP RPC connected successfully. Latest block number: %s", web3.eth.block_number)
    LOGGER.info("Loaded %s users from %s", len(users), USERS_FILE)
    LOGGER.info("Checking %s via Multicall3: %s", protocol.name, MULTICALL3_ADDRESS)

    statuses = scan_user_statuses(web3, pool_address, users)
    for index, status in enumerate(statuses, start=1):
        if protocol.protocol_family == "compound_v2":
            prefix = "HOT " if int(status["shortfall_raw"]) > 0 else ""
            LOGGER.info(
                "%s[%s/%s] Address: %s | Tier: %s | Liquidity: %s | Shortfall: %s | Status: %s",
                prefix,
                index,
                len(statuses),
                status["address"],
                status["tier"],
                status["liquidity_raw"],
                status["shortfall_raw"],
                status["health_factor"],
            )
            continue

        health_factor_value = status["health_factor_value"]
        prefix = "ALERT " if health_factor_value is not None and health_factor_value < HF_ALERT_THRESHOLD else ""
        LOGGER.info(
            "%s[%s/%s] Address: %s | Tier: %s | Collateral: $%s | Debt: $%s | Health Factor: %s",
            prefix,
            index,
            len(statuses),
            status["address"],
            status["tier"],
            format_usd(int(status["total_collateral_base"])),
            format_usd(int(status["total_debt_base"])),
            status["health_factor"],
        )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rpc_url, pool_address = load_config()
    web3 = create_web3(rpc_url)
    users = load_users()
    monitor_users(web3, pool_address, users)


if __name__ == "__main__":
    main()
