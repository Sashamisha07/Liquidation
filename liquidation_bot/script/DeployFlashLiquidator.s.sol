// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {FlashLiquidator} from "../src/FlashLiquidator.sol";

contract DeployFlashLiquidatorScript is Script {
    function run() external returns (FlashLiquidator deployed) {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address poolAddress = vm.envAddress("POOL_ADDRESS");
        address swapRouterAddress = vm.envAddress("SWAP_ROUTER_ADDRESS");
        address ownerAddress = vm.envAddress("OWNER_ADDRESS");

        vm.startBroadcast(deployerPrivateKey);
        deployed = new FlashLiquidator(poolAddress, swapRouterAddress, ownerAddress);
        vm.stopBroadcast();
    }
}
