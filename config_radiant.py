from __future__ import annotations

import os

from config_aave import ProtocolConfig


RADIANT_CONFIG = ProtocolConfig(
    name="radiant",
    protocol_family="aave_v2_like",
    pool_address="0xE23B4AE3624fB6f7cDEF29bC8EAD912f1Ede6886",
    data_provider_address="0x596B0cc4c5094507C50b579a662FE7e7b094A2cC",
    price_oracle_address="0xC0cE5De939aaD880b0bdDcf9aB5750a53EDa454b",
    swap_router_address="0xE592427A0AEce92De3Edee1F18E0157C05861564",
    liquidator_artifact_path="D:/liquidations_Must/liquidation_bot/out/RadiantLiquidator.sol/RadiantLiquidator.json",
    liquidator_address=os.getenv("RADIANT_LIQUIDATOR_ADDRESS", ""),
    borrow_event_addresses=("0xE23B4AE3624fB6f7cDEF29bC8EAD912f1Ede6886",),
    borrow_event_signature="Borrow(address,address,address,uint256,uint256,uint256,uint16)",
)
