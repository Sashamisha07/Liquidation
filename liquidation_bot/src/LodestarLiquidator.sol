// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "./interfaces/IERC20.sol";
import {IPool} from "./interfaces/aave/IPool.sol";
import {IFlashLoanSimpleReceiver} from "./interfaces/aave/IFlashLoanSimpleReceiver.sol";
import {ILToken} from "./interfaces/lodestar/ILToken.sol";
import {ISwapRouter} from "./interfaces/uniswap/ISwapRouter.sol";
import {IWETH} from "./interfaces/IWETH.sol";

contract LodestarLiquidator is IFlashLoanSimpleReceiver {
    address internal constant WRAPPED_NATIVE_TOKEN = 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1;

    struct LiquidationParams {
        address user;
        address debtAsset;
        address debtLToken;
        address collateralAsset;
        address collateralLToken;
        uint256 debtToCover;
        uint24 uniswapPoolFee;
        uint256 minAmountOut;
        uint160 sqrtPriceLimitX96;
        uint256 swapDeadline;
    }

    address public owner;
    IPool public immutable aavePool;
    ISwapRouter public immutable swapRouter;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event LiquidationTriggered(address indexed user, address indexed debtAsset, uint256 amount);
    event LiquidationExecuted(
        address indexed user,
        address indexed collateralAsset,
        address indexed debtAsset,
        uint256 debtCovered,
        uint256 collateralRedeemed,
        uint256 amountRepaid,
        uint256 profit
    );
    event ProfitWithdrawn(address indexed token, address indexed to, uint256 amount);

    modifier onlyOwner() {
        require(msg.sender == owner, "ONLY_OWNER");
        _;
    }

    constructor(address aavePoolAddress, address swapRouterAddress, address ownerAddress) {
        require(aavePoolAddress != address(0), "INVALID_POOL");
        require(swapRouterAddress != address(0), "INVALID_ROUTER");
        require(ownerAddress != address(0), "INVALID_OWNER");

        aavePool = IPool(aavePoolAddress);
        swapRouter = ISwapRouter(swapRouterAddress);
        owner = ownerAddress;

        emit OwnershipTransferred(address(0), ownerAddress);
    }

    receive() external payable {}

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "INVALID_OWNER");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address initiator,
        bytes calldata params
    ) external override returns (bool) {
        require(msg.sender == address(aavePool), "CALLER_NOT_POOL");
        require(initiator == address(this), "INVALID_INITIATOR");

        LiquidationParams memory liq = abi.decode(params, (LiquidationParams));
        require(asset == liq.debtAsset, "UNEXPECTED_FLASH_ASSET");
        require(amount == liq.debtToCover, "UNEXPECTED_FLASH_AMOUNT");

        uint256 collateralUnderlyingBefore = IERC20(liq.collateralAsset).balanceOf(address(this));
        uint256 nativeBalanceBefore = address(this).balance;

        _forceApprove(liq.debtAsset, liq.debtLToken, amount);
        uint256 liquidationErrorCode = ILToken(liq.debtLToken).liquidateBorrow(
            liq.user,
            liq.debtToCover,
            liq.collateralLToken
        );
        require(liquidationErrorCode == 0, "LODESTAR_LIQUIDATION_FAILED");

        uint256 seizedLTokenBalance = ILToken(liq.collateralLToken).balanceOf(address(this));
        require(seizedLTokenBalance > 0, "NO_LTOKEN_RECEIVED");

        uint256 redeemErrorCode = ILToken(liq.collateralLToken).redeem(seizedLTokenBalance);
        require(redeemErrorCode == 0, "LODESTAR_REDEEM_FAILED");

        uint256 collateralRedeemed = IERC20(liq.collateralAsset).balanceOf(address(this)) - collateralUnderlyingBefore;
        if (collateralRedeemed == 0) {
            uint256 nativeCollateralRedeemed = address(this).balance - nativeBalanceBefore;
            require(liq.collateralAsset == WRAPPED_NATIVE_TOKEN, "UNEXPECTED_NATIVE_COLLATERAL");
            if (nativeCollateralRedeemed > 0) {
                IWETH(WRAPPED_NATIVE_TOKEN).deposit{value: nativeCollateralRedeemed}();
                collateralRedeemed = nativeCollateralRedeemed;
            }
        }

        require(collateralRedeemed > 0, "NO_COLLATERAL_REDEEMED");

        _forceApprove(liq.collateralAsset, address(swapRouter), collateralRedeemed);
        uint256 amountOut = swapRouter.exactInputSingle(
            ISwapRouter.ExactInputSingleParams({
                tokenIn: liq.collateralAsset,
                tokenOut: liq.debtAsset,
                fee: liq.uniswapPoolFee,
                recipient: address(this),
                deadline: liq.swapDeadline,
                amountIn: collateralRedeemed,
                amountOutMinimum: liq.minAmountOut,
                sqrtPriceLimitX96: liq.sqrtPriceLimitX96
            })
        );

        uint256 amountOwed = amount + premium;
        require(amountOut >= amountOwed, "INSUFFICIENT_SWAP_OUTPUT");

        _forceApprove(liq.debtAsset, address(aavePool), amountOwed);

        emit LiquidationExecuted(
            liq.user,
            liq.collateralAsset,
            liq.debtAsset,
            liq.debtToCover,
            collateralRedeemed,
            amountOwed,
            amountOut - amountOwed
        );

        return true;
    }

    function triggerLiquidation(
        address user,
        address debtAsset,
        address debtLToken,
        address collateralAsset,
        address collateralLToken,
        uint256 debtToCover,
        uint24 uniswapPoolFee,
        uint256 minAmountOut,
        uint160 sqrtPriceLimitX96,
        uint256 swapDeadline
    ) external onlyOwner {
        LiquidationParams memory liq = LiquidationParams({
            user: user,
            debtAsset: debtAsset,
            debtLToken: debtLToken,
            collateralAsset: collateralAsset,
            collateralLToken: collateralLToken,
            debtToCover: debtToCover,
            uniswapPoolFee: uniswapPoolFee,
            minAmountOut: minAmountOut,
            sqrtPriceLimitX96: sqrtPriceLimitX96,
            swapDeadline: swapDeadline
        });

        emit LiquidationTriggered(user, debtAsset, debtToCover);

        aavePool.flashLoanSimple(
            address(this),
            debtAsset,
            debtToCover,
            abi.encode(liq),
            0
        );
    }

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
