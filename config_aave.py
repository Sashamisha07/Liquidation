from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProtocolConfig:
    name: str
    protocol_family: str
    pool_address: str
    data_provider_address: str
    price_oracle_address: str
    swap_router_address: str
    liquidator_artifact_path: str
    liquidator_address: str
    comptroller_address: str = ""
    lens_address: str = ""
    borrow_event_addresses: tuple[str, ...] = ()
    borrow_event_signature: str = ""
    markets: dict[str, dict[str, str]] = field(default_factory=dict)
    default_uniswap_pool_fee: int = 100
    default_min_amount_out: int = 0
    default_sqrt_price_limit_x96: int = 0
    default_deadline_seconds: int = 300


AAVE_CONFIG = ProtocolConfig(
    name="aave",
    protocol_family="aave_v3",
    pool_address="0x794a61358D6845594F94dc1DB02A252b5b4814aD",
    data_provider_address="0x69FA688f1Dc47d4B5d8029D5a35FB7a548310654",
    price_oracle_address="0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7",
    swap_router_address="0xE592427A0AEce92De3Edee1F18E0157C05861564",
    liquidator_artifact_path="D:/liquidations_Must/liquidation_bot/out/FlashLiquidator.sol/FlashLiquidator.json",
    liquidator_address=os.getenv("AAVE_LIQUIDATOR_ADDRESS", "0x7Bb352DcDb1C3d10e1279e7F8ea60200a6de3c12"),
    borrow_event_addresses=("0x794a61358D6845594F94dc1DB02A252b5b4814aD",),
    borrow_event_signature="Borrow(address,address,address,uint256,uint8,uint256,uint16)",
)
