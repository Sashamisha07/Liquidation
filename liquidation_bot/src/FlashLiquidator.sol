// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "./interfaces/IERC20.sol";
import {IPool} from "./interfaces/aave/IPool.sol";
import {IFlashLoanSimpleReceiver} from "./interfaces/aave/IFlashLoanSimpleReceiver.sol";
import {ISwapRouter} from "./interfaces/uniswap/ISwapRouter.sol";

contract FlashLiquidator is IFlashLoanSimpleReceiver {
    struct LiquidationParams {
        address user;
        address collateralAsset;
        address debtAsset;
        address collateralAToken;
        uint256 debtToCover;
        bool receiveAToken;
        uint24 uniswapPoolFee;
        uint256 minAmountOut;
        uint160 sqrtPriceLimitX96;
        uint256 swapDeadline;
    }

    address public owner;
    IPool public immutable pool;
    ISwapRouter public immutable swapRouter;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event LiquidationTriggered(address indexed user, address indexed debtAsset, uint256 amount);
    event LiquidationExecuted(
        address indexed user,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 debtCovered,
        uint256 collateralReceived,
        uint256 amountRepaid,
        uint256 profit
    );
    event ProfitWithdrawn(address indexed token, address indexed to, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "ONLY_OWNER");
        _;
    }

    constructor(address poolAddress, address swapRouterAddress, address ownerAddress) {
        require(poolAddress != address(0), "INVALID_POOL");
        require(swapRouterAddress != address(0), "INVALID_ROUTER");
        require(ownerAddress != address(0), "INVALID_OWNER");

        pool = IPool(poolAddress);
        swapRouter = ISwapRouter(swapRouterAddress);
        owner = ownerAddress;

        emit OwnershipTransferred(address(0), ownerAddress);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "INVALID_OWNER");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    /// @notice Aave flash-loan callback: repay bad debt, seize collateral, swap it back, return flash loan.
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(pool), "CALLER_NOT_POOL");
        require(initiator == address(this), "INVALID_INITIATOR");

        LiquidationParams memory liq = abi.decode(params, (LiquidationParams));
        require(asset == liq.debtAsset, "UNEXPECTED_FLASH_ASSET");
        require(amount == liq.debtToCover, "UNEXPECTED_FLASH_AMOUNT");

        uint256 collateralBefore = IERC20(liq.collateralAsset).balanceOf(address(this));
        uint256 aTokenBefore;
        if (liq.receiveAToken) {
            require(liq.collateralAToken != address(0), "INVALID_ATOKEN");
            aTokenBefore = IERC20(liq.collateralAToken).balanceOf(address(this));
        }

        _forceApprove(liq.debtAsset, address(pool), liq.debtToCover);
        pool.liquidationCall(
            liq.collateralAsset,
            liq.debtAsset,
            liq.user,
            type(uint256).max,
            liq.receiveAToken
        );

        uint256 collateralAmount;
        if (liq.receiveAToken) {
            uint256 aTokenReceived = IERC20(liq.collateralAToken).balanceOf(address(this)) - aTokenBefore;
            require(aTokenReceived > 0, "NO_ATOKEN_RECEIVED");
            collateralAmount = pool.withdraw(liq.collateralAsset, aTokenReceived, address(this));
        } else {
            collateralAmount = IERC20(liq.collateralAsset).balanceOf(address(this)) - collateralBefore;
        }

        require(collateralAmount > 0, "NO_COLLATERAL_RECEIVED");

        _forceApprove(liq.collateralAsset, address(swapRouter), collateralAmount);
        uint256 amountOut = swapRouter.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: liq.collateralAsset,
                tokenOut: liq.debtAsset,
                fee: liq.uniswapPoolFee,
                recipient: address(this),
                deadline: liq.swapDeadline,
                amountIn: collateralAmount,
                amountOutMinimum: liq.minAmountOut,
                sqrtPriceLimitX96: liq.sqrtPriceLimitX96
            })
        );

        uint256 amountOwed = amount + premium;
        require(amountOut >= amountOwed, "INSUFFICIENT_SWAP_OUTPUT");

        _forceApprove(liq.debtAsset, address(pool), amountOwed);

        emit LiquidationExecuted(
            liq.user,
            liq.collateralAsset,
            liq.debtAsset,
            liq.debtToCover,
            collateralAmount,
            amountOwed,
            amountOut - amountOwed
        );

        return true;
    }

    /// @notice Starts a flash loan on Aave and passes liquidation parameters to the callback.
    function triggerLiquidation(
        address user,
        address collateralAsset,
        address debtAsset,
        address collateralAToken,
        uint256 debtToCover,
        bool receiveAToken,
        uint24 uniswapPoolFee,
        uint256 minAmountOut,
        uint160 sqrtPriceLimitX96,
        uint256 swapDeadline
    ) external onlyOwner {
        LiquidationParams memory liq = LiquidationParams({
            user: user,
            collateralAsset: collateralAsset,
            debtAsset: debtAsset,
            collateralAToken: collateralAToken,
            debtToCover: debtToCover,
            receiveAToken: receiveAToken,
            uniswapPoolFee: uniswapPoolFee,
            minAmountOut: minAmountOut,
            sqrtPriceLimitX96: sqrtPriceLimitX96,
            swapDeadline: swapDeadline
        });

        emit LiquidationTriggered(user, debtAsset, debtToCover);

        pool.flashLoanSimple(
            address(this),
            debtAsset,
            debtToCover,
            abi.encode(liq),
            0
        );
    }

    /// @notice Withdraws accumulated profit in any ERC-20 token to the owner wallet.
    function withdrawProfit(address token, uint256 amount) external onlyOwner {
        require(token != address(0), "INVALID_TOKEN");
        require(IERC20(token).transfer(owner, amount), "TRANSFER_FAILED");
        emit ProfitWithdrawn(token, owner, amount);
    }

    function _forceApprove(address token, address spender, uint256 amount) internal {
        require(IERC20(token).approve(spender, 0), "APPROVE_RESET_FAILED");
        require(IERC20(token).approve(spender, amount), "APPROVE_FAILED");
    }
}
