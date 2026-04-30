// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILendingPoolV2 {
    function flashLoan(
        address receiverAddress,
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata modes,
        address onBehalfOf,
        bytes calldata params,
        uint16 referralCode
    ) external;

    function liquidationCall(
        address collateralAsset,
        address debtAsset,
        address user,
        uint256 debtToCover,
        bool receiveAToken
    ) external;

    function withdraw(address asset, uint256 amount, address to) external returns (uint256);
}
