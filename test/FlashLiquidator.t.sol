// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";

import {FlashLiquidator} from "../contracts/FlashLiquidator.sol";
import {IERC20} from "../contracts/interfaces/IERC20.sol";

contract FlashLiquidatorTest is Test {
    address internal constant AAVE_V3_POOL = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address internal constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;

    address internal constant TARGET = 0x7693517008eee395E93dFC8E784d48E667222FA6;
    address internal constant USDT = 0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9;
    address internal constant USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;

    uint24 internal constant USDC_USDT_POOL_FEE = 100;
    uint256 internal constant DEBT_TO_COVER = 1e6; // 1 USDT (6 decimals)

    FlashLiquidator internal liquidator;

    function setUp() external {
        vm.createSelectFork(vm.envString("RPC_URL"));

        liquidator = new FlashLiquidator(
            AAVE_V3_POOL,
            UNISWAP_V3_ROUTER,
            address(this)
        );
    }

    function testLiquidation() external {
        uint256 usdcBefore = IERC20(USDC).balanceOf(address(liquidator));
        uint256 usdtBefore = IERC20(USDT).balanceOf(address(liquidator));

        assertEq(usdcBefore, 0, "FlashLiquidator should start with zero USDC");

        liquidator.triggerLiquidation(
            TARGET,
            USDC,
            USDT,
            address(0),
            DEBT_TO_COVER,
            false,
            USDC_USDT_POOL_FEE,
            0,
            0,
            block.timestamp + 1 hours
        );

        uint256 usdcAfter = IERC20(USDC).balanceOf(address(liquidator));
        uint256 usdtAfter = IERC20(USDT).balanceOf(address(liquidator));

        // With the current FlashLiquidator implementation, seized USDC is swapped back to USDT,
        // so realized profit remains on the contract in the debt asset, not in the collateral asset.
        assertGt(usdtAfter, usdtBefore, "FlashLiquidator should retain positive USDT profit");

        // Keep the collateral balance check explicit for observability:
        // it is expected to remain zero after the full exact-input swap path completes.
        assertEq(usdcAfter, 0, "USDC collateral is swapped back into USDT by design");
    }
}
