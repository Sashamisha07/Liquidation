// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "forge-std/console2.sol";

import {FlashLiquidator} from "../src/FlashLiquidator.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

interface IAavePool {
    function getUserAccountData(address user)
        external
        view
        returns (
            uint256 totalCollateralBase,
            uint256 totalDebtBase,
            uint256 availableBorrowsBase,
            uint256 currentLiquidationThreshold,
            uint256 ltv,
            uint256 healthFactor
        );
}

interface IAaveOracle {
    function getAssetPrice(address asset) external view returns (uint256);
}

contract ArbSysMock {
    function arbBlockNumber() external view returns (uint256) {
        return block.number;
    }

    function arbBlockHash(uint256 blockNumber) external view returns (bytes32) {
        return blockhash(blockNumber);
    }

    function arbChainID() external view returns (uint256) {
        return block.chainid;
    }
}

contract FlashLiquidatorTest is Test {
    address internal constant ARB_SYS = address(0x64);
    address internal constant AAVE_V3_POOL = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address internal constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address internal constant AAVE_ORACLE = 0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7;

    address internal constant TARGET = 0x7693517008eee395E93dFC8E784d48E667222FA6;
    address internal constant USDT = 0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9;
    address internal constant USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;

    uint24 internal constant USDC_USDT_POOL_FEE = 100;
    uint256 internal constant DEBT_TO_COVER = 250_000_000;
    uint256 internal constant USDT_DECIMALS = 1e6;

    FlashLiquidator internal liquidator;

    function setUp() external {
        vm.etch(ARB_SYS, address(new ArbSysMock()).code);

        liquidator = new FlashLiquidator(
            AAVE_V3_POOL,
            UNISWAP_V3_ROUTER,
            address(this)
        );
    }

    function testLiquidation() external {
        uint256 usdcBefore = IERC20(USDC).balanceOf(address(liquidator));
        uint256 usdtBefore = IERC20(USDT).balanceOf(address(liquidator));
        (, , , , , uint256 originalHealthFactor) = IAavePool(AAVE_V3_POOL).getUserAccountData(TARGET);

        assertEq(usdcBefore, 0, "FlashLiquidator should start with zero USDC");
        console2.log("Original health factor:", originalHealthFactor);

        vm.mockCall(
            AAVE_ORACLE,
            abi.encodeWithSelector(IAaveOracle.getAssetPrice.selector, USDC),
            abi.encode(uint256(50_000_000))
        );

        (, , , , , uint256 mockedHealthFactor) = IAavePool(AAVE_V3_POOL).getUserAccountData(TARGET);
        console2.log("Mocked health factor:", mockedHealthFactor);
        assertLt(mockedHealthFactor, 1e18, "Mocked health factor should be below 1.0");

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
        string memory profit = _formatUsdt(usdtAfter);

        assertGt(usdtAfter, usdtBefore, "FlashLiquidator should retain positive USDT profit");
        assertEq(usdcAfter, 0, "USDC collateral is swapped back into USDT by design");

        console2.log("Net Profit (USDT):", profit);
    }

    function _formatUsdt(uint256 amount) internal pure returns (string memory) {
        uint256 whole = amount / USDT_DECIMALS;
        uint256 fractional = amount % USDT_DECIMALS;
        bytes memory fractionalBytes = bytes(_leftPad6(_toString(fractional)));

        return string.concat(_toString(whole), ".", string(fractionalBytes));
    }

    function _leftPad6(string memory value) internal pure returns (string memory) {
        bytes memory raw = bytes(value);
        if (raw.length >= 6) {
            return value;
        }

        bytes memory padded = new bytes(6);
        uint256 offset = 6 - raw.length;

        for (uint256 i = 0; i < offset; i++) {
            padded[i] = bytes1("0");
        }

        for (uint256 i = 0; i < raw.length; i++) {
            padded[offset + i] = raw[i];
        }

        return string(padded);
    }

    function _toString(uint256 value) internal pure returns (string memory) {
        if (value == 0) {
            return "0";
        }

        uint256 temp = value;
        uint256 digits;
        while (temp != 0) {
            digits++;
            temp /= 10;
        }

        bytes memory buffer = new bytes(digits);
        while (value != 0) {
            digits -= 1;
            buffer[digits] = bytes1(uint8(48 + uint256(value % 10)));
            value /= 10;
        }

        return string(buffer);
    }
}
