// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import "forge-std/Script.sol";

import {RadiantLiquidator} from "../src/RadiantLiquidator.sol";

contract DeployRadiantLiquidatorScript is Script {
    function run() external returns (RadiantLiquidator deployed) {
        uint256 deployerPrivateKey = vm.envUint("PRIVATE_KEY");
        address lendingPoolAddress = vm.envAddress("RADIANT_POOL_ADDRESS");
        address swapRouterAddress = vm.envAddress("SWAP_ROUTER_ADDRESS");
        address ownerAddress = vm.envAddress("OWNER_ADDRESS");

        vm.startBroadcast(deployerPrivateKey);
        deployed = new RadiantLiquidator(lendingPoolAddress, swapRouterAddress, ownerAddress);
        vm.stopBroadcast();
    }
}
