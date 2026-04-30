from __future__ import annotations

import os

from config_aave import ProtocolConfig


LODESTAR_MARKETS: dict[str, dict[str, str]] = {
    "lUSDC": {
        "lToken": "0x4C9aAed3b8c443b4b634D1A189a5e25C604768dE",
        "underlying": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
    },
    "lETH": {
        "lToken": "0x2193c45244AF12C280941281c8aa67dD08be0a64",
        "underlying": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    },
    "lARB": {
        "lToken": "0x8991d64fe388fA79A4f7Aa7826E8dA09F0c3C96a",
        "underlying": "0x912CE59144191C1204E64559FE8253a0e49E6548",
    },
    "lUSDT": {
        "lToken": "0x9365181A7df82a1cC578eAE443EFd89f00dbb643",
        "underlying": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    },
    "lDAI": {
        "lToken": "0x4987782da9a63bC3ABace48648B15546D821c720",
        "underlying": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    },
    "lWBTC": {
        "lToken": "0xC37896BF3EE5a2c62Cdbd674035069776f721668",
        "underlying": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    },
    "lGMX": {
        "lToken": "0x79B6c5e1A7C0aD507E1dB81eC7cF269062BAb4Eb",
        "underlying": "0xfc5A1A6EB076a2C7A3Af2D4eB8a7E827E25567C3",
    },
    "lwstETH": {
        "lToken": "0xfECe754D92bd956F681A941Cef4632AB65710495",
        "underlying": "0x5979D7b546E38E414F7E9822514be443A4800529",
    },
}


LODESTAR_CONFIG = ProtocolConfig(
    name="lodestar",
    protocol_family="compound_v2",
    pool_address="0xa86DD95c210dd186Fa7639F93E4177E97d057576",
    data_provider_address="",
    price_oracle_address="0xcCf9393df2F656262FD79599175950faB4D4ec01",
    swap_router_address="0xE592427A0AEce92De3Edee1F18E0157C05861564",
    liquidator_artifact_path="liquidation_bot/out/LodestarLiquidator.sol/LodestarLiquidator.json",
    liquidator_address=os.getenv("LODESTAR_LIQUIDATOR_ADDRESS", "0xd0946A0002F0Ee3760938f2bAA2a4D69903D367e"),
    comptroller_address="0xa86DD95c210dd186Fa7639F93E4177E97d057576",
    lens_address="0x24C25910aF4068B5F6C3b75252a36c4810849135",
    borrow_event_addresses=tuple(market["lToken"] for market in LODESTAR_MARKETS.values()),
    borrow_event_signature="Borrow(address,uint256,uint256,uint256)",
    markets=LODESTAR_MARKETS,
)
