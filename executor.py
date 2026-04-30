"""
Transaction executor for calling the active liquidator contract on Arbitrum.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from protocol_config import get_active_protocol_config
from requests import exceptions as requests_exceptions
from web3 import HTTPProvider, Web3


MAX_UINT256 = 2**256 - 1
DEFAULT_GAS_LIMIT = 2_000_000
DEFAULT_PRIORITY_FEE_WEI = 100_000_000  # 0.1 gwei
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
MAX_RPC_RETRIES = 3
RPC_RETRY_DELAY_SECONDS = 2

load_dotenv()
PROTOCOL = get_active_protocol_config()

LODESTAR_COMPTROLLER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "closeFactorMantissa",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

LODESTAR_MARKET_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "getAccountSnapshot",
        "outputs": [
            {"internalType": "uint256", "name": "", "type": "uint256"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


class LiquidationExecutionError(RuntimeError):
    def __init__(self, message: str, tx_hash: str | None = None) -> None:
        super().__init__(message)
        self.tx_hash = tx_hash


def _load_env() -> tuple[str, str, str]:
    load_dotenv()

    rpc_url = os.getenv("HTTP_RPC_URL")
    private_key = os.getenv("PRIVATE_KEY")
    owner_address = os.getenv("OWNER_ADDRESS")

    if not rpc_url:
        raise ValueError("HTTP_RPC_URL is not set in .env")
    if not private_key:
        raise ValueError("PRIVATE_KEY is not set in .env")
    if not owner_address:
        raise ValueError("OWNER_ADDRESS is not set in .env")

    return rpc_url, private_key, owner_address


def _create_web3(rpc_url: str, timeout: int = 60) -> Web3:
    web3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    if not web3.is_connected():
        raise ConnectionError(f"Could not connect to RPC provider: {rpc_url}")
    return web3


def _load_contract_abi(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Foundry artifact not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        artifact = json.load(file)

    abi = artifact.get("abi")
    if not isinstance(abi, list):
        raise ValueError(f"Artifact ABI is missing or invalid: {path}")

    return abi


def _build_fee_params(web3: Web3) -> dict[str, int]:
    latest_block = web3.eth.get_block("latest")
    base_fee = int(latest_block.get("baseFeePerGas") or 0)
    priority_fee = DEFAULT_PRIORITY_FEE_WEI

    if hasattr(web3.eth, "max_priority_fee"):
        try:
            priority_fee = int(web3.eth.max_priority_fee)
        except Exception:  # noqa: BLE001
            priority_fee = DEFAULT_PRIORITY_FEE_WEI

    if base_fee > 0:
        max_fee = (base_fee * 2) + priority_fee
        return {
            "maxPriorityFeePerGas": priority_fee,
            "maxFeePerGas": max_fee,
        }

    gas_price = int(web3.eth.gas_price)
    return {"gasPrice": max(gas_price * 2, gas_price + priority_fee)}


def _build_contract(web3: Web3):
    return web3.eth.contract(address=LIQUIDATOR_ADDRESS, abi=LIQUIDATOR_ABI)


def _is_retryable_rpc_error(exc: Exception) -> bool:
    retryable_types = (
        requests_exceptions.ConnectionError,
        requests_exceptions.Timeout,
        TimeoutError,
        ConnectionError,
        OSError,
    )
    if isinstance(exc, retryable_types):
        return True

    message = str(exc).lower()
    retryable_markers = [
        "connection aborted",
        "connection reset",
        "remote end closed connection",
        "temporarily unavailable",
        "read timed out",
        "max retries exceeded",
    ]
    return any(marker in message for marker in retryable_markers)


def _retry_rpc_call(operation_name: str, func):
    last_error: Exception | None = None

    for attempt in range(1, MAX_RPC_RETRIES + 1):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt == MAX_RPC_RETRIES or not _is_retryable_rpc_error(exc):
                break
            time.sleep(RPC_RETRY_DELAY_SECONDS)

    raise LiquidationExecutionError(
        f"{operation_name} failed after {MAX_RPC_RETRIES} attempts: {last_error}",
        tx_hash=None,
    ) from last_error


def _resolve_lodestar_ltoken(asset_address: str) -> str:
    checksum_asset = Web3.to_checksum_address(asset_address)
    for market in PROTOCOL.markets.values():
        if Web3.to_checksum_address(market["underlying"]) == checksum_asset:
            return Web3.to_checksum_address(market["lToken"])
    raise LiquidationExecutionError(f"Could not resolve Lodestar lToken for asset {asset_address}")


def _resolve_lodestar_debt_to_cover(web3: Web3, debt_ltoken: str, target_user: str, requested_debt_to_cover: int) -> int:
    if requested_debt_to_cover > 0:
        return requested_debt_to_cover

    comptroller = web3.eth.contract(
        address=Web3.to_checksum_address(PROTOCOL.comptroller_address or PROTOCOL.pool_address),
        abi=LODESTAR_COMPTROLLER_ABI,
    )
    market = web3.eth.contract(address=Web3.to_checksum_address(debt_ltoken), abi=LODESTAR_MARKET_ABI)
    close_factor_mantissa = int(comptroller.functions.closeFactorMantissa().call())
    _, _, borrow_balance_raw, _ = market.functions.getAccountSnapshot(Web3.to_checksum_address(target_user)).call()

    debt_to_cover = int((int(borrow_balance_raw) * close_factor_mantissa) // (10**18))
    if debt_to_cover <= 0:
        debt_to_cover = int(borrow_balance_raw)
    if debt_to_cover <= 0:
        raise LiquidationExecutionError(f"Borrower {target_user} has no Lodestar debt in market {debt_ltoken}")

    return debt_to_cover


ARTIFACT_PATH = Path(PROTOCOL.liquidator_artifact_path)
LIQUIDATOR_ADDRESS = PROTOCOL.liquidator_address
DEFAULT_UNISWAP_POOL_FEE = PROTOCOL.default_uniswap_pool_fee
DEFAULT_MIN_AMOUNT_OUT = PROTOCOL.default_min_amount_out
DEFAULT_SQRT_PRICE_LIMIT_X96 = PROTOCOL.default_sqrt_price_limit_x96
DEFAULT_DEADLINE_SECONDS = PROTOCOL.default_deadline_seconds

RPC_URL, PRIVATE_KEY, OWNER_ADDRESS = _load_env()
OWNER_ADDRESS = Web3.to_checksum_address(OWNER_ADDRESS)
if not LIQUIDATOR_ADDRESS:
    raise ValueError(f"{PROTOCOL.name} liquidator address is not configured")
LIQUIDATOR_ADDRESS = Web3.to_checksum_address(LIQUIDATOR_ADDRESS)
LIQUIDATOR_ABI = _load_contract_abi(ARTIFACT_PATH)


def execute_liquidation(
    target_user: str,
    collateral_asset: str,
    debt_asset: str,
    debt_to_cover: int,
    *,
    collateral_ltoken: str | None = None,
    debt_ltoken: str | None = None,
) -> tuple[str, Any]:
    """
    Call the active liquidator contract's triggerLiquidation and wait for confirmation.

    For Aave-style liquidators the on-chain call still uses MAX_UINT256 by design.
    For Lodestar, the executor resolves the matching lTokens and computes a valid
    close-factor-sized debtToCover when the caller leaves it as 0.
    """

    target_user = Web3.to_checksum_address(target_user)
    collateral_asset = Web3.to_checksum_address(collateral_asset)
    debt_asset = Web3.to_checksum_address(debt_asset)
    zero_address = Web3.to_checksum_address(ZERO_ADDRESS)

    web3 = _create_web3(RPC_URL)
    liquidator = _build_contract(web3)

    nonce = _retry_rpc_call(
        "get_transaction_count",
        lambda: web3.eth.get_transaction_count(OWNER_ADDRESS, "pending"),
    )
    fee_params = _retry_rpc_call("build_fee_params", lambda: _build_fee_params(web3))
    latest_block = _retry_rpc_call("get_latest_block", lambda: web3.eth.get_block("latest"))
    chain_id = _retry_rpc_call("get_chain_id", lambda: web3.eth.chain_id)
    swap_deadline = int(latest_block["timestamp"]) + DEFAULT_DEADLINE_SECONDS

    if PROTOCOL.protocol_family == "compound_v2":
        resolved_debt_ltoken = Web3.to_checksum_address(debt_ltoken or _resolve_lodestar_ltoken(debt_asset))
        resolved_collateral_ltoken = Web3.to_checksum_address(
            collateral_ltoken or _resolve_lodestar_ltoken(collateral_asset)
        )
        resolved_debt_to_cover = _resolve_lodestar_debt_to_cover(
            web3,
            resolved_debt_ltoken,
            target_user,
            int(debt_to_cover),
        )

        transaction = _retry_rpc_call(
            "build_transaction",
            lambda: liquidator.functions.triggerLiquidation(
                target_user,
                debt_asset,
                resolved_debt_ltoken,
                collateral_asset,
                resolved_collateral_ltoken,
                resolved_debt_to_cover,
                DEFAULT_UNISWAP_POOL_FEE,
                DEFAULT_MIN_AMOUNT_OUT,
                DEFAULT_SQRT_PRICE_LIMIT_X96,
                swap_deadline,
            ).build_transaction(
                {
                    "from": OWNER_ADDRESS,
                    "nonce": nonce,
                    "gas": DEFAULT_GAS_LIMIT,
                    "chainId": chain_id,
                    **fee_params,
                }
            ),
        )
    else:
        transaction = _retry_rpc_call(
            "build_transaction",
            lambda: liquidator.functions.triggerLiquidation(
                target_user,
                collateral_asset,
                debt_asset,
                zero_address,
                MAX_UINT256,
                False,
                DEFAULT_UNISWAP_POOL_FEE,
                DEFAULT_MIN_AMOUNT_OUT,
                DEFAULT_SQRT_PRICE_LIMIT_X96,
                swap_deadline,
            ).build_transaction(
                {
                    "from": OWNER_ADDRESS,
                    "nonce": nonce,
                    "gas": DEFAULT_GAS_LIMIT,
                    "chainId": chain_id,
                    **fee_params,
                }
            ),
        )

    signed_tx = web3.eth.account.sign_transaction(transaction, private_key=PRIVATE_KEY)
    tx_hash = _retry_rpc_call(
        "send_raw_transaction",
        lambda: web3.eth.send_raw_transaction(signed_tx.raw_transaction),
    )
    tx_hash_hex = tx_hash.hex()

    try:
        receipt = _retry_rpc_call(
            "wait_for_transaction_receipt",
            lambda: web3.eth.wait_for_transaction_receipt(tx_hash),
        )
    except LiquidationExecutionError as exc:
        raise LiquidationExecutionError(str(exc), tx_hash=tx_hash_hex) from exc

    if int(receipt.status) != 1:
        raise LiquidationExecutionError("Transaction reverted on-chain", tx_hash=tx_hash_hex)

    return tx_hash_hex, receipt
