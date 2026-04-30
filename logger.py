"""
CSV logger and colored console output for profitable liquidation candidates.
"""

from __future__ import annotations

import csv
import logging
import sys
from pathlib import Path
from typing import Any


CSV_FILE = "liquidations_log.csv"
CSV_COLUMNS = [
    "Timestamp",
    "UserAddress",
    "CollateralToken",
    "DebtToken",
    "DebtAmount",
    "GrossProfit",
    "EstimatedNetProfit",
]

RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )


def append_liquidation_row(record: dict[str, Any], csv_path: str | Path = CSV_FILE) -> None:
    output_path = Path(csv_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    file_exists = output_path.exists()

    row = {column: record.get(column, "") for column in CSV_COLUMNS}

    with output_path.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def log_profitable_liquidation(record: dict[str, Any], logger: logging.Logger | None = None) -> None:
    app_logger = logger or logging.getLogger(__name__)
    net_profit = float(record.get("EstimatedNetProfit", 0))
    color = GREEN if net_profit > 0 else YELLOW if net_profit == 0 else RED
    message = (
        f"PROFITABLE LIQUIDATION | user={record.get('UserAddress')} "
        f"collateral={record.get('CollateralToken')} debt={record.get('DebtToken')} "
        f"debt_amount=${record.get('DebtAmount')} gross=${record.get('GrossProfit')} "
        f"net=${record.get('EstimatedNetProfit')}"
    )

    if sys.stdout.isatty():
        print(f"{color}{message}{RESET}")
    else:
        app_logger.info(message)
