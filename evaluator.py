"""
Evaluator for inspecting collateral, debt, and simple liquidation economics
for the active lending protocol on Arbitrum.

Dust Collection strategy: no minimum loan-size filters.
A liquidation is executed as long as:
    Net Profit = Gross Profit - Flash-loan Fee - Gas >= MIN_NET_PROFIT_USD
"""

from __future__ import annotations

import logging
import os
from decimal import Decimal, ROUND_DOWN, getcontext
from typing import Any

from dotenv import load_dotenv
from protocol_config import get_active_protocol_config
from web3 import HTTPProvider, Web3


LOGGER = logging.getLogger(__name__)

load_dotenv()
PROTOCOL = get_active_protocol_config()
TARGET_USER = os.getenv("TARGET_USER", "0x7693517008eee395E93dFC8E784d48E667222FA6")
DATA_PROVIDER_ADDRESS = PROTOCOL.data_provider_address

# ---------------------------------------------------------------------------
# Cost / profit configuration for Dust Collection
# ---------------------------------------------------------------------------
# TX_FEE_USD — estimated gas cost in USD equivalent.
# Override via env var GAS_COST_USD for dynamic gas pricing.
TX_FEE = Decimal(os.getenv("GAS_COST_USD", "0.10"))  # default: $0.10 gas

# Minimum acceptable net profit (USD) to execute a liquidation.
# We accept even tiny wins down to $0.50 — but never go negative.
MIN_NET_PROFIT_USD: Decimal = Decimal(os.getenv("MIN_NET_PROFIT_USD", "0.50"))

FLASHLOAN_FEE_PERCENT = Decimal("0.0005")   # Aave V3 flash-loan fee: 0.05%
SWAP_FEE_PERCENT = Decimal("0.0005")         # Uniswap swap fee tier 0.05%
COMPOUND_MANTISSA = Decimal(10) ** 18

getcontext().prec = 50

PROTOCOL_DATA_PROVIDER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "getAllReservesTokens",
        "outputs": [
            {
                "components": [
                    {"internalType": "string", "name": "symbol", "type": "string"},
                    {"internalType": "address", "name": "tokenAddress", "type": "address"},
                ],
                "internalType": "struct IPoolDataProvider.TokenData[]",
                "name": "",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "asset", "type": "address"},
            {"internalType": "address", "name": "user", "type": "address"},
        ],
        "name": "getUserReserveData",
        "outputs": [
            {"internalType": "uint256", "name": "currentATokenBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "currentStableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "currentVariableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "principalStableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "scaledVariableDebt", "type": "uint256"},
            {"internalType": "uint256", "name": "stableBorrowRate", "type": "uint256"},
            {"internalType": "uint256", "name": "liquidityRate", "type": "uint256"},
            {"internalType": "uint40", "name": "stableRateLastUpdated", "type": "uint40"},
            {"internalType": "bool", "name": "usageAsCollateralEnabled", "type": "bool"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    }
]

COMPTROLLER_ABI: list[dict[str, Any]] = [
    {
        "inputs": [],
        "name": "closeFactorMantissa",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "liquidationIncentiveMantissa",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
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

PRICE_ORACLE_ABI: list[dict[str, Any]] = [
    {
        "inputs": [{"internalType": "address", "name": "lToken", "type": "address"}],
        "name": "getUnderlyingPrice",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]


def load_rpc_url() -> str:
    load_dotenv()
    rpc_url = os.getenv("HTTP_RPC_URL")
    if not rpc_url:
        raise ValueError("HTTP_RPC_URL is not set in .env")
    return rpc_url


def create_web3(rpc_url: str, timeout: int = 60) -> Web3:
    web3 = Web3(HTTPProvider(rpc_url, request_kwargs={"timeout": timeout}))
    if not web3.is_connected():
        raise ConnectionError(f"Could not connect to RPC provider: {rpc_url}")
    return web3


def to_human_amount(raw_value: int, decimals: int) -> Decimal:
    return Decimal(raw_value) / (Decimal(10) ** decimals)


def format_amount(value: Decimal, places: int = 6) -> str:
    quantum = Decimal(1).scaleb(-places)
    return format(value.quantize(quantum, rounding=ROUND_DOWN), "f")


def safe_text(value: str) -> str:
    return value.encode("ascii", errors="replace").decode("ascii")


def get_decimals(web3: Web3, token_address: str) -> int:
    token = web3.eth.contract(address=Web3.to_checksum_address(token_address), abi=ERC20_ABI)
    return int(token.functions.decimals().call())


def _evaluate_aave_like_liquidation(web3: Web3, user: str, data_provider_address: str) -> dict[str, Any] | None:
    """Evaluate Aave V3 liquidation for a given user.

    Dust Collection mode: NO minimum debt-size filter is applied here.
    Any position with collateral + variable debt is considered.
    Profitability filtering is done downstream by is_profitable().
    """
    user = Web3.to_checksum_address(user)
    data_provider_address = Web3.to_checksum_address(data_provider_address)

    contract = web3.eth.contract(address=data_provider_address, abi=PROTOCOL_DATA_PROVIDER_ABI)
    reserves = contract.functions.getAllReservesTokens().call()

    # Pick the largest collateral and largest debt across all reserves
    collateral_position: dict[str, Any] | None = None
    debt_position: dict[str, Any] | None = None

    for reserve in reserves:
        symbol = reserve[0]
        asset = Web3.to_checksum_address(reserve[1])
        data = contract.functions.getUserReserveData(asset, user).call()

        current_a_token_balance = int(data[0])
        current_variable_debt = int(data[2])
        usage_as_collateral_enabled = bool(data[8])

        # Skip reserve if user has no position in it
        if current_a_token_balance <= 0 and current_variable_debt <= 0:
            continue

        decimals = get_decimals(web3, asset)

        # Track largest collateral position (Dust Collection: no min-size guard)
        if current_a_token_balance > 0 and usage_as_collateral_enabled:
            candidate_col = {
                "symbol": safe_text(symbol),
                "asset": asset,
                "decimals": decimals,
                "raw_balance": current_a_token_balance,
                "human_balance": to_human_amount(current_a_token_balance, decimals),
            }
            if collateral_position is None or candidate_col["raw_balance"] > collateral_position["raw_balance"]:
                collateral_position = candidate_col

        # Track largest debt position (Dust Collection: no min-size guard)
        if current_variable_debt > 0:
            candidate_debt = {
                "symbol": safe_text(symbol),
                "asset": asset,
                "decimals": decimals,
                "raw_balance": current_variable_debt,
                "human_balance": to_human_amount(current_variable_debt, decimals),
            }
            if debt_position is None or candidate_debt["raw_balance"] > debt_position["raw_balance"]:
                debt_position = candidate_debt

    if collateral_position is None or debt_position is None:
        return None

    # Aave V3 liquidation economics
    close_factor = Decimal("0.5")                     # max 50% of debt can be liquidated
    liquidation_bonus_multiplier = Decimal("1.05")   # 5% bonus on collateral
    max_liquidatable_debt = debt_position["human_balance"] * close_factor
    collateral_received = max_liquidatable_debt * liquidation_bonus_multiplier
    gross_profit = collateral_received - max_liquidatable_debt   # = 5% of liquidatable debt

    # Cost breakdown (all in debt-token units / USD-equivalent)
    flashloan_fee = max_liquidatable_debt * FLASHLOAN_FEE_PERCENT   # 0.05%
    swap_fee = max_liquidatable_debt * SWAP_FEE_PERCENT             # 0.05%
    gas_cost = TX_FEE                                               # fixed USD gas estimate

    estimated_net_profit = gross_profit - flashloan_fee - swap_fee - gas_cost

    return {
        "user": user,
        "collateral_token": collateral_position["symbol"],
        "collateral_asset": collateral_position["asset"],
        "debt_token": debt_position["symbol"],
        "debt_asset": debt_position["asset"],
        "collateral_amount": collateral_position["human_balance"],
        "debt_amount": debt_position["human_balance"],
        "max_liquidatable_debt": max_liquidatable_debt,
        "max_liquidatable_debt_raw": int(debt_position["raw_balance"] // 2),
        "collateral_received": collateral_received,
        "gross_profit": gross_profit,
        "flashloan_fee": flashloan_fee,
        "swap_fee": swap_fee,
        "tx_fee": gas_cost,
        "estimated_net_profit": estimated_net_profit,
        # Helper flag — used by monitor/executor to skip unprofitable dust
        "is_profitable": estimated_net_profit >= MIN_NET_PROFIT_USD,
    }


def _evaluate_lodestar_liquidation(web3: Web3, user: str) -> dict[str, Any] | None:
    user = Web3.to_checksum_address(user)
    comptroller = web3.eth.contract(
        address=Web3.to_checksum_address(PROTOCOL.comptroller_address or PROTOCOL.pool_address),
        abi=COMPTROLLER_ABI,
    )
    oracle = web3.eth.contract(address=Web3.to_checksum_address(PROTOCOL.price_oracle_address), abi=PRICE_ORACLE_ABI)

    close_factor_mantissa = int(comptroller.functions.closeFactorMantissa().call())
    liquidation_incentive_mantissa = int(comptroller.functions.liquidationIncentiveMantissa().call())

    collateral_position: dict[str, Any] | None = None
    debt_position: dict[str, Any] | None = None

    for symbol, market in PROTOCOL.markets.items():
        ltoken = Web3.to_checksum_address(market["lToken"])
        underlying = Web3.to_checksum_address(market["underlying"])
        market_contract = web3.eth.contract(address=ltoken, abi=LODESTAR_MARKET_ABI)

        try:
            error_code, ltoken_balance_raw, borrow_balance_raw, exchange_rate_mantissa = market_contract.functions.getAccountSnapshot(user).call()
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("Could not fetch Lodestar snapshot for %s (%s): %s", symbol, ltoken, exc)
            continue

        if int(error_code) != 0:
            continue
        if int(ltoken_balance_raw) <= 0 and int(borrow_balance_raw) <= 0:
            continue

        decimals = get_decimals(web3, underlying)
        oracle_price = int(oracle.functions.getUnderlyingPrice(ltoken).call())

        if int(ltoken_balance_raw) > 0:
            supplied_underlying_raw = int((int(ltoken_balance_raw) * int(exchange_rate_mantissa)) // (10**18))
            collateral_value_wei = int((supplied_underlying_raw * oracle_price) // (10**18))
            candidate = {
                "symbol": safe_text(symbol),
                "lToken": ltoken,
                "asset": underlying,
                "decimals": decimals,
                "ltoken_balance_raw": int(ltoken_balance_raw),
                "raw_balance": supplied_underlying_raw,
                "human_balance": to_human_amount(supplied_underlying_raw, decimals),
                "oracle_price": oracle_price,
                "value_wei": collateral_value_wei,
            }
            if collateral_position is None or candidate["value_wei"] > collateral_position["value_wei"]:
                collateral_position = candidate

        if int(borrow_balance_raw) > 0:
            debt_value_wei = int((int(borrow_balance_raw) * oracle_price) // (10**18))
            candidate = {
                "symbol": safe_text(symbol),
                "lToken": ltoken,
                "asset": underlying,
                "decimals": decimals,
                "raw_balance": int(borrow_balance_raw),
                "human_balance": to_human_amount(int(borrow_balance_raw), decimals),
                "oracle_price": oracle_price,
                "value_wei": debt_value_wei,
            }
            if debt_position is None or candidate["value_wei"] > debt_position["value_wei"]:
                debt_position = candidate

    if collateral_position is None or debt_position is None:
        return None

    max_liquidatable_debt_raw = int((debt_position["raw_balance"] * close_factor_mantissa) // (10**18))
    if max_liquidatable_debt_raw <= 0:
        max_liquidatable_debt_raw = int(debt_position["raw_balance"])

    debt_value_wei = int((max_liquidatable_debt_raw * debt_position["oracle_price"]) // (10**18))
    gross_profit_wei = int(
        debt_value_wei * max(liquidation_incentive_mantissa - 10**18, 0) // (10**18)
    )

    debt_value_eth = Decimal(debt_value_wei) / COMPOUND_MANTISSA
    gross_profit_eth = Decimal(gross_profit_wei) / COMPOUND_MANTISSA
    flashloan_fee_eth = debt_value_eth * FLASHLOAN_FEE_PERCENT
    swap_fee_eth = debt_value_eth * SWAP_FEE_PERCENT
    estimated_net_profit = gross_profit_eth - flashloan_fee_eth - swap_fee_eth - TX_FEE

    return {
        "user": user,
        "collateral_token": collateral_position["symbol"],
        "collateral_asset": collateral_position["asset"],
        "collateral_ltoken": collateral_position["lToken"],
        "debt_token": debt_position["symbol"],
        "debt_asset": debt_position["asset"],
        "debt_ltoken": debt_position["lToken"],
        "collateral_amount": collateral_position["human_balance"],
        "debt_amount": debt_position["human_balance"],
        "max_liquidatable_debt": to_human_amount(max_liquidatable_debt_raw, int(debt_position["decimals"])),
        "max_liquidatable_debt_raw": max_liquidatable_debt_raw,
        "collateral_received": debt_value_eth * (Decimal(liquidation_incentive_mantissa) / COMPOUND_MANTISSA),
        "gross_profit": gross_profit_eth,
        "estimated_net_profit": estimated_net_profit,
        "tx_fee": TX_FEE,
        "flashloan_fee": flashloan_fee_eth,
        "swap_fee": swap_fee_eth,
    }


def is_profitable(result: dict[str, Any]) -> bool:
    """Return True only if the net profit meets the Dust Collection minimum ($0.50).

    This is the single gate-keeper function: call it after evaluate_liquidation()
    before submitting any on-chain transaction.

    Example::

        result = evaluate_liquidation(web3, user)
        if result and is_profitable(result):
            executor.execute(result)
    """
    net = result.get("estimated_net_profit", Decimal("-1"))
    if net < MIN_NET_PROFIT_USD:
        LOGGER.debug(
            "Skipping %s — net profit %s < min threshold %s",
            result.get("user"),
            net,
            MIN_NET_PROFIT_USD,
        )
        return False
    return True


def evaluate_liquidation(
    web3: Web3,
    user: str,
    data_provider_address: str = DATA_PROVIDER_ADDRESS,
) -> dict[str, Any] | None:
    if PROTOCOL.protocol_family == "compound_v2":
        return _evaluate_lodestar_liquidation(web3, user)
    return _evaluate_aave_like_liquidation(web3, user, data_provider_address)


def evaluate_user_reserves(web3: Web3, user: str, data_provider_address: str) -> None:
    protocol = get_active_protocol_config()
    user = Web3.to_checksum_address(user)

    LOGGER.info("HTTP RPC connected successfully. Latest block number: %s", web3.eth.block_number)
    LOGGER.info("Target user: %s", user)
    LOGGER.info("Protocol: %s", protocol.name)

    liquidation = evaluate_liquidation(web3, user, data_provider_address)
    if liquidation is None:
        LOGGER.warning("Could not compute liquidation for %s", user)
        return

    print()
    print("=" * 72)
    print(f"{safe_text(protocol.name.upper())} LIQUIDATION SNAPSHOT")
    print("=" * 72)
    print(f"User:                  {user}")
    print(f"Collateral:            {safe_text(str(liquidation['collateral_token']))} ({format_amount(liquidation['collateral_amount'])})")
    print(f"Debt:                  {safe_text(str(liquidation['debt_token']))} ({format_amount(liquidation['debt_amount'])})")
    print("-" * 72)
    print(f"Max Liquidatable Debt: {safe_text(str(liquidation['debt_token']))} {format_amount(liquidation['max_liquidatable_debt'])}")
    print(f"Collateral Received:   {safe_text(str(liquidation['collateral_token']))} {format_amount(liquidation['collateral_received'])}")
    print(f"Gross Profit:          ETH {format_amount(liquidation['gross_profit'])}")
    print(f"Estimated Net Profit:  ETH {format_amount(liquidation['estimated_net_profit'])}")
    print("=" * 72)
    print()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    rpc_url = load_rpc_url()
    web3 = create_web3(rpc_url)
    evaluate_user_reserves(
        web3=web3,
        user=TARGET_USER,
        data_provider_address=DATA_PROVIDER_ADDRESS,
    )


if __name__ == "__main__":
    main()
