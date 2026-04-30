"""
Autonomous liquidation analytics pipeline.

Loop:
1. Index fresh users from the latest 1000 blocks
2. Check user health factors
3. Evaluate liquidation economics for risky users
4. Log profitable candidates to CSV
5. Sleep for 5 minutes
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from executor import LiquidationExecutionError, execute_liquidation
from evaluator import DATA_PROVIDER_ADDRESS, evaluate_liquidation, format_amount, is_profitable
from indexer import create_web3, load_config, scan_borrowers
from logger import append_liquidation_row, log_profitable_liquidation, setup_logging
from monitor import (
    COLD_INTERVAL_SECONDS,
    HOT_INTERVAL_SECONDS,
    WARM_INTERVAL_SECONDS,
    get_monitoring_interval,
    get_monitoring_tier,
    load_users,
    scan_user_statuses,
)


LOGGER = logging.getLogger(__name__)
POLL_INTERVAL_SECONDS = 10
INDEXER_INTERVAL_SECONDS = 900
LIQUIDATION_TRIGGER_THRESHOLD = Decimal("1.0")


def build_log_record(status: dict[str, object], evaluation: dict[str, object]) -> dict[str, str]:
    return {
        "Timestamp": datetime.now(timezone.utc).isoformat(),
        "UserAddress": str(status["address"]),
        "CollateralToken": str(evaluation["collateral_token"]),
        "DebtToken": str(evaluation["debt_token"]),
        "DebtAmount": format_amount(evaluation["max_liquidatable_debt"]),
        "GrossProfit": format_amount(evaluation["gross_profit"]),
        "EstimatedNetProfit": format_amount(evaluation["estimated_net_profit"]),
    }


def schedule_state(now: float, health_factor_value: Decimal | None) -> dict[str, object]:
    interval = get_monitoring_interval(health_factor_value)
    tier = get_monitoring_tier(health_factor_value)
    return {
        "tier": tier,
        "next_check_at": now + interval,
        "last_health_factor": str(health_factor_value) if health_factor_value is not None else None,
    }


def main() -> None:
    setup_logging("INFO")

    rpc_url, pool_address = load_config()
    web3 = create_web3(rpc_url)
    monitor_state: dict[str, dict[str, object]] = {}
    last_index_run = 0.0

    LOGGER.info("Pipeline started. Latest block: %s", web3.eth.block_number)
    LOGGER.info(
        "Tiered monitoring intervals | hot=%ss warm=%ss cold=%ss indexer=%ss",
        HOT_INTERVAL_SECONDS,
        WARM_INTERVAL_SECONDS,
        COLD_INTERVAL_SECONDS,
        INDEXER_INTERVAL_SECONDS,
    )

    while True:
        try:
            now = time.time()

            users = load_users()
            for user in users:
                monitor_state.setdefault(
                    str(user),
                    {
                        "tier": "cold",
                        "next_check_at": 0.0,
                        "last_health_factor": None,
                    },
                )

            if now - last_index_run >= INDEXER_INTERVAL_SECONDS:
                LOGGER.info("Step 1: indexing fresh borrowers")
                scan_borrowers(web3=web3, pool_address=pool_address)
                last_index_run = now
                users = load_users()
                for user in users:
                    monitor_state.setdefault(
                        str(user),
                        {
                            "tier": "cold",
                            "next_check_at": 0.0,
                            "last_health_factor": None,
                        },
                    )

            due_users = [
                str(user)
                for user in users
                if float(monitor_state.get(str(user), {}).get("next_check_at", 0.0)) <= now
            ]

            if not due_users:
                LOGGER.info("No users due for monitoring this cycle")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            LOGGER.info("Step 2: monitoring %s due users out of %s total", len(due_users), len(users))
            statuses = scan_user_statuses(web3, pool_address, due_users)

            for status in statuses:
                monitor_state[str(status["address"])] = schedule_state(
                    now=now,
                    health_factor_value=status["health_factor_value"],
                )

            LOGGER.info("Step 3: evaluating liquidatable users")
            for status in statuses:
                health_factor_value = status["health_factor_value"]
                if health_factor_value is None or Decimal(health_factor_value) >= LIQUIDATION_TRIGGER_THRESHOLD:
                    continue

                evaluation = evaluate_liquidation(
                    web3=web3,
                    user=str(status["address"]),
                    data_provider_address=DATA_PROVIDER_ADDRESS,
                )
                if evaluation is None:
                    continue

                # Dust Collection gate: skip if net profit < MIN_NET_PROFIT_USD ($0.50 default)
                if not is_profitable(evaluation):
                    LOGGER.debug(
                        "Skipping %s — net profit %.4f below dust threshold",
                        status["address"],
                        evaluation["estimated_net_profit"],
                    )
                    continue

                record = build_log_record(status, evaluation)
                append_liquidation_row(record)
                log_profitable_liquidation(record, LOGGER)

                try:
                    tx_hash, receipt = execute_liquidation(
                        target_user=str(status["address"]),
                        collateral_asset=str(evaluation["collateral_asset"]),
                        debt_asset=str(evaluation["debt_asset"]),
                        debt_to_cover=0,
                    )
                    LOGGER.info(
                        "Liquidation sent | user=%s | tx_hash=%s | block=%s | status=%s",
                        status["address"],
                        tx_hash,
                        receipt.blockNumber,
                        receipt.status,
                    )
                except LiquidationExecutionError as exc:
                    LOGGER.error(
                        "Liquidation execution failed | user=%s | tx_hash=%s | error=%s",
                        status["address"],
                        exc.tx_hash,
                        exc,
                    )
                except Exception as exc:  # noqa: BLE001
                    tx_hash = getattr(exc, "tx_hash", None)
                    LOGGER.exception(
                        "Unexpected liquidation error | user=%s | tx_hash=%s | error=%s",
                        status["address"],
                        tx_hash,
                        exc,
                    )

            hot_count = sum(1 for state in monitor_state.values() if state.get("tier") == "hot")
            warm_count = sum(1 for state in monitor_state.values() if state.get("tier") == "warm")
            cold_count = sum(1 for state in monitor_state.values() if state.get("tier") == "cold")
            LOGGER.info(
                "Cycle complete. Tiers | hot=%s warm=%s cold=%s. Sleeping for %s seconds",
                hot_count,
                warm_count,
                cold_count,
                POLL_INTERVAL_SECONDS,
            )
        except KeyboardInterrupt:
            LOGGER.info("Stopped by user")
            break
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Pipeline cycle failed: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
