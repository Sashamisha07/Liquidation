from __future__ import annotations

import os

from config_aave import AAVE_CONFIG, ProtocolConfig
from config_lodestar import LODESTAR_CONFIG
from config_radiant import RADIANT_CONFIG


def get_active_protocol_name() -> str:
    return os.getenv("LENDING_PROTOCOL", "aave").strip().lower()


def get_active_protocol_config() -> ProtocolConfig:
    protocol = get_active_protocol_name()
    if protocol == "lodestar":
        return LODESTAR_CONFIG
    if protocol == "radiant":
        return RADIANT_CONFIG
    if protocol == "aave":
        return AAVE_CONFIG
    raise ValueError(f"Unsupported LENDING_PROTOCOL: {protocol}")
