// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "forge-std/console2.sol";

import {IERC20} from "../src/interfaces/IERC20.sol";
import {LodestarLiquidator} from "../src/LodestarLiquidator.sol";

interface ILodestarUnitroller {
    function getAccountLiquidity(address account) external view returns (uint256, uint256, uint256);
}

interface ILodestarOracle {
    function getUnderlyingPrice(address lToken) external view returns (uint256);
}

interface ILodestarMarket {
    function getAccountSnapshot(address account) external view returns (uint256, uint256, uint256, uint256);
}

contract LodestarLiquidatorTest is Test {
    address internal constant AAVE_V3_POOL = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;
    address internal constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address internal constant ARBITRUM_WETH = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;
    address internal constant ARBITRUM_USDC = 0xaf88d065e77c8cC2239327C5EDb3A432268e5831;

    address internal constant LODESTAR_UNITROLLER = 0xa86DD95c210dd186Fa7639F93E4177E97d057576;
    address internal constant LODESTAR_ORACLE = 0xcCf9393df2F656262FD79599175950faB4D4ec01;

    address internal constant LUSDC = 0x4C9aAed3b8c443b4b634D1A189a5e25C604768dE;
    address internal constant LETH = 0x2193c45244AF12C280941281c8aa67dD08be0a64;

    address internal constant TARGET_BORROWER = 0x63A3BB359Ec36656B0D1c242099483110CAAC839;

    uint24 internal constant WETH_USDC_POOL_FEE = 500;
    uint256 internal constant USDC_DECIMALS = 1e6;
    uint256 internal constant MOCK_LUSDC_PRICE = 4e26;
    uint256 internal constant MOCK_LETH_PRICE = 3e17;
    uint256 internal constant DEBT_TO_COVER = 150;

    LodestarLiquidator internal liquidator;

    function setUp() external {
        vm.createSelectFork(vm.envString("HTTP_RPC_URL"));

        liquidator = new LodestarLiquidator(
            AAVE_V3_POOL,
            UNISWAP_V3_ROUTER,
            address(this)
        );
    }

    function testLodestarLiquidation() external {
        (uint256 errorCode, uint256 collateralBalance, uint256 debtBalance,) =
            ILodestarMarket(LETH).getAccountSnapshot(TARGET_BORROWER);
        assertEq(errorCode, 0, "Snapshot error");
        assertGt(collateralBalance, 0, "Borrower should hold lETH collateral");

        (errorCode,, debtBalance,) = ILodestarMarket(LUSDC).getAccountSnapshot(TARGET_BORROWER);
        assertEq(errorCode, 0, "Debt snapshot error");
        assertGe(debtBalance, DEBT_TO_COVER, "Borrower debt too small");

        uint256 usdcBefore = IERC20(ARBITRUM_USDC).balanceOf(address(liquidator));
        (bool originalCheckOk, bytes memory originalData) = address(ILodestarUnitroller(LODESTAR_UNITROLLER)).staticcall(
            abi.encodeWithSelector(ILodestarUnitroller.getAccountLiquidity.selector, TARGET_BORROWER)
        );
        if (originalCheckOk) {
            (, uint256 liquidityBefore, uint256 shortfallBefore) = abi.decode(originalData, (uint256, uint256, uint256));
            console2.log("Lodestar liquidity before:", liquidityBefore);
            console2.log("Lodestar shortfall before:", shortfallBefore);
        } else {
            console2.log("Original account liquidity reverted before mocking oracle prices");
        }

        vm.mockCall(
            LODESTAR_ORACLE,
            abi.encodeWithSelector(ILodestarOracle.getUnderlyingPrice.selector, LUSDC),
            abi.encode(MOCK_LUSDC_PRICE)
        );
        vm.mockCall(
            LODESTAR_ORACLE,
            abi.encodeWithSelector(ILodestarOracle.getUnderlyingPrice.selector, LETH),
            abi.encode(MOCK_LETH_PRICE)
        );

        (, uint256 liquidityAfter, uint256 shortfallAfter) =
            ILodestarUnitroller(LODESTAR_UNITROLLER).getAccountLiquidity(TARGET_BORROWER);
        console2.log("Lodestar liquidity after mock:", liquidityAfter);
        console2.log("Lodestar shortfall after mock:", shortfallAfter);
        assertGt(shortfallAfter, 0, "Mocked account should have shortfall");

        liquidator.triggerLiquidation(
            TARGET_BORROWER,
            ARBITRUM_USDC,
            LUSDC,
            ARBITRUM_WETH,
            LETH,
            DEBT_TO_COVER,
            WETH_USDC_POOL_FEE,
            0,
            0,
            block.timestamp + 1 hours
        );

        uint256 usdcAfter = IERC20(ARBITRUM_USDC).balanceOf(address(liquidator));
        uint256 netProfit = usdcAfter - usdcBefore;

        assertGt(netProfit, 0, "Liquidator should retain positive USDC profit");
        console2.log("Net Profit (USDC):", _formatUsdc(netProfit));
    }

    function _formatUsdc(uint256 amount) internal pure returns (string memory) {
        uint256 whole = amount / USDC_DECIMALS;
        uint256 fractional = amount % USDC_DECIMALS;
        return string.concat(_toString(whole), ".", _leftPad6(_toString(fractional)));
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
