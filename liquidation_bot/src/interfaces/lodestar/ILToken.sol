// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface ILToken {
    function balanceOf(address account) external view returns (uint256);

    function liquidateBorrow(address borrower, uint256 repayAmount, address lTokenCollateral) external returns (uint256);

    function redeem(uint256 redeemTokens) external returns (uint256);

    function redeemUnderlying(uint256 redeemAmount) external returns (uint256);
}
