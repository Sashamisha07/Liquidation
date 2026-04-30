// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {LodestarLiquidator} from "../src/LodestarLiquidator.sol";

contract DeployLodestarLiquidatorScript is Script {
    function run() external returns (LodestarLiquidator deployed) {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address aavePoolAddress = vm.envAddress("POOL_ADDRESS");
        address swapRouterAddress = vm.envAddress("SWAP_ROUTER_ADDRESS");
        address ownerAddress = vm.envAddress("OWNER_ADDRESS");

        vm.startBroadcast(deployerPrivateKey);
        deployed = new LodestarLiquidator(aavePoolAddress, swapRouterAddress, ownerAddress);
        vm.stopBroadcast();
    }
}
