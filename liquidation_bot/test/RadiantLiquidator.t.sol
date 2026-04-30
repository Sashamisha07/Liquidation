// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Test.sol";
import "forge-std/console2.sol";

import {RadiantLiquidator} from "../src/RadiantLiquidator.sol";
import {IERC20} from "../src/interfaces/IERC20.sol";

interface IRadiantLendingPool {
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

interface IRadiantOracle {
    function getAssetPrice(address asset) external view returns (uint256);
}

interface IRadiantProtocolDataProvider {
    struct TokenData {
        string symbol;
        address tokenAddress;
    }

    function getAllReservesTokens() external view returns (TokenData[] memory);

    function getUserReserveData(address asset, address user)
        external
        view
        returns (
            uint256 currentATokenBalance,
            uint256 currentStableDebt,
            uint256 currentVariableDebt,
            uint256 principalStableDebt,
            uint256 scaledVariableDebt,
            uint256 stableBorrowRate,
            uint256 liquidityRate,
            uint40 stableRateLastUpdated,
            bool usageAsCollateralEnabled
        );
}

contract RadiantLiquidatorTest is Test {
    address internal constant RADIANT_LENDING_POOL = 0xE23B4AE3624fB6f7cDEF29bC8EAD912f1Ede6886;
    address internal constant RADIANT_PROTOCOL_DATA_PROVIDER = 0x596B0cc4c5094507C50b579a662FE7e7b094A2cC;
    address internal constant RADIANT_ORACLE = 0xC0cE5De939aaD880b0bdDcf9aB5750a53EDa454b;
    address internal constant UNISWAP_V3_ROUTER = 0xE592427A0AEce92De3Edee1F18E0157C05861564;

    uint24 internal constant DEFAULT_UNISWAP_POOL_FEE = 100;

    address[] internal candidateUsers;
    RadiantLiquidator internal liquidator;

    function setUp() external {
        liquidator = new RadiantLiquidator(
            RADIANT_LENDING_POOL,
            UNISWAP_V3_ROUTER,
            address(this)
        );

        candidateUsers.push(0xE10997B8d5C6e8b660451f61accF4BBA00bc901f);
        candidateUsers.push(0xdA9e9EB631f1694209D05e8087be9cb9Cf10088E);
    }

    function testRadiantLiquidation() external {
        (
            address targetUser,
            address collateralAsset,
            address debtAsset,
            uint256 debtToCover
        ) = _findLiquidationCandidate();

        uint256 debtBefore = IERC20(debtAsset).balanceOf(address(liquidator));
        (, , , , , uint256 originalHealthFactor) = IRadiantLendingPool(RADIANT_LENDING_POOL).getUserAccountData(targetUser);
        console2.log("Original health factor:", originalHealthFactor);

        vm.mockCall(
            RADIANT_ORACLE,
            abi.encodeWithSelector(IRadiantOracle.getAssetPrice.selector, collateralAsset),
            abi.encode(uint256(50_000_000))
        );

        (, , , , , uint256 mockedHealthFactor) = IRadiantLendingPool(RADIANT_LENDING_POOL).getUserAccountData(targetUser);
        console2.log("Mocked health factor:", mockedHealthFactor);
        assertLt(mockedHealthFactor, 1e18, "Mocked health factor should be below 1.0");

        liquidator.triggerLiquidation(
            targetUser,
            collateralAsset,
            debtAsset,
            address(0),
            debtToCover,
            false,
            DEFAULT_UNISWAP_POOL_FEE,
            0,
            0,
            block.timestamp + 1 hours
        );

        uint256 debtAfter = IERC20(debtAsset).balanceOf(address(liquidator));
        assertGt(debtAfter, debtBefore, "Radiant liquidator should retain profit in debt asset");
    }

    function _findLiquidationCandidate()
        internal
        view
        returns (address targetUser, address collateralAsset, address debtAsset, uint256 debtToCover)
    {
        IRadiantProtocolDataProvider.TokenData[] memory reserves =
            IRadiantProtocolDataProvider(RADIANT_PROTOCOL_DATA_PROVIDER).getAllReservesTokens();

        for (uint256 userIndex = 0; userIndex < candidateUsers.length; userIndex++) {
            address candidate = candidateUsers[userIndex];
            address foundCollateral;
            address foundDebt;
            uint256 foundDebtAmount;

            for (uint256 reserveIndex = 0; reserveIndex < reserves.length; reserveIndex++) {
                (
                    uint256 currentATokenBalance,
                    ,
                    uint256 currentVariableDebt,
                    ,
                    ,
                    ,
                    ,
                    ,
                    bool usageAsCollateralEnabled
                ) = IRadiantProtocolDataProvider(RADIANT_PROTOCOL_DATA_PROVIDER).getUserReserveData(
                    reserves[reserveIndex].tokenAddress,
                    candidate
                );

                if (foundCollateral == address(0) && currentATokenBalance > 0 && usageAsCollateralEnabled) {
                    foundCollateral = reserves[reserveIndex].tokenAddress;
                }

                if (foundDebt == address(0) && currentVariableDebt > 0) {
                    foundDebt = reserves[reserveIndex].tokenAddress;
                    foundDebtAmount = currentVariableDebt;
                }
            }

            if (foundCollateral != address(0) && foundDebt != address(0) && foundDebtAmount > 0) {
                return (candidate, foundCollateral, foundDebt, foundDebtAmount);
            }
        }

        revert("NO_RADIANT_CANDIDATE_FOUND");
    }
}
