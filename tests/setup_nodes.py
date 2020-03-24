import signal
from typing import Any, Dict, Optional
from pathlib import Path
import asyncio
import blspy
from secrets import token_bytes

from src.consensus.constants import constants
from src.full_node.blockchain import Blockchain
from src.full_node.mempool_manager import MempoolManager
from src.full_node.store import FullNodeStore
from src.full_node.full_node import FullNode
from src.server.connection import NodeType
from src.server.server import ChiaServer
from src.simulator.full_node_simulator import FullNodeSimulator
from src.timelord_launcher import spawn_process, kill_processes
from src.wallet.wallet_node import WalletNode
from src.types.full_block import FullBlock
from src.full_node.coin_store import CoinStore
from tests.block_tools import BlockTools
from src.types.hashable.BLSSignature import BLSPublicKey
from src.util.config import load_config
from src.pool import create_puzzlehash_for_pk
from src.harvester import Harvester
from src.farmer import Farmer
from src.introducer import Introducer
from src.timelord import Timelord
from src.server.connection import PeerInfo
from src.util.ints import uint16


bt = BlockTools()

test_constants: Dict[str, Any] = {
    "DIFFICULTY_STARTING": 1,
    "DISCRIMINANT_SIZE_BITS": 16,
    "BLOCK_TIME_TARGET": 10,
    "MIN_BLOCK_TIME": 2,
    "DIFFICULTY_EPOCH": 12,  # The number of blocks per epoch
    "DIFFICULTY_DELAY": 3,  # EPOCH / WARP_FACTOR
    "PROPAGATION_THRESHOLD": 10,
    "PROPAGATION_DELAY_THRESHOLD": 20,
    "TX_PER_SEC": 1,
    "MEMPOOL_BLOCK_BUFFER": 10,
    "MIN_ITERS_STARTING": 50 * 2,
}
test_constants["GENESIS_BLOCK"] = bytes(
    bt.create_genesis_block(test_constants, bytes([0] * 32), b"0")
)


async def setup_full_node_simulator(db_name, port, introducer_port=None, dic={}):
    # SETUP
    test_constants_copy = test_constants.copy()
    for k in dic.keys():
        test_constants_copy[k] = dic[k]

    store_1 = await FullNodeStore.create(Path(db_name))
    await store_1._clear_database()
    unspent_store_1 = await CoinStore.create(Path(db_name))
    await unspent_store_1._clear_database()
    mempool_1 = MempoolManager(unspent_store_1, test_constants_copy)

    b_1: Blockchain = await Blockchain.create(
        unspent_store_1, store_1, test_constants_copy
    )
    await mempool_1.new_tips(await b_1.get_full_tips())

    await store_1.add_block(FullBlock.from_bytes(test_constants_copy["GENESIS_BLOCK"]))

    config = load_config("config.yaml", "full_node")
    if introducer_port is not None:
        config["introducer_peer"]["host"] = "127.0.0.1"
        config["introducer_peer"]["port"] = introducer_port
    full_node_1 = FullNodeSimulator(
        store_1,
        b_1,
        config,
        mempool_1,
        unspent_store_1,
        f"full_node_{port}",
        test_constants_copy,
    )
    server_1 = ChiaServer(
        port, full_node_1, NodeType.FULL_NODE, name="full-node-simulator-server"
    )
    _ = await server_1.start_server(config["host"], full_node_1._on_connect)
    full_node_1._set_server(server_1)

    yield (full_node_1, server_1)

    # TEARDOWN
    full_node_1._shutdown()
    server_1.close_all()
    await server_1.await_closed()
    await store_1.close()
    await unspent_store_1.close()
    Path(db_name).unlink()


async def setup_full_node(db_name, port, introducer_port=None, dic={}):
    # SETUP
    test_constants_copy = test_constants.copy()
    for k in dic.keys():
        test_constants_copy[k] = dic[k]

    store_1 = await FullNodeStore.create(Path(db_name))
    await store_1._clear_database()
    unspent_store_1 = await CoinStore.create(Path(db_name))
    await unspent_store_1._clear_database()
    mempool_1 = MempoolManager(unspent_store_1, test_constants_copy)

    b_1: Blockchain = await Blockchain.create(
        unspent_store_1, store_1, test_constants_copy
    )
    await mempool_1.new_tips(await b_1.get_full_tips())

    await store_1.add_block(FullBlock.from_bytes(test_constants_copy["GENESIS_BLOCK"]))

    config = load_config("config.yaml", "full_node")
    if introducer_port is not None:
        config["introducer_peer"]["host"] = "127.0.0.1"
        config["introducer_peer"]["port"] = introducer_port
    full_node_1 = FullNode(
        store_1,
        b_1,
        config,
        mempool_1,
        unspent_store_1,
        f"full_node_{port}",
        test_constants_copy,
    )
    server_1 = ChiaServer(port, full_node_1, NodeType.FULL_NODE)
    _ = await server_1.start_server(config["host"], full_node_1._on_connect)
    full_node_1._set_server(server_1)

    yield (full_node_1, server_1)

    # TEARDOWN
    full_node_1._shutdown()
    server_1.close_all()
    await server_1.await_closed()
    await store_1.close()
    await unspent_store_1.close()
    Path(db_name).unlink()


async def setup_wallet_node(port, introducer_port=None, key_seed=b"", dic={}):
    config = load_config("config.yaml", "wallet")
    if "starting_height" in dic:
        config["starting_height"] = dic["starting_height"]
    key_config = {
        "wallet_sk": bytes(blspy.ExtendedPrivateKey.from_seed(key_seed)).hex(),
    }
    test_constants_copy = test_constants.copy()
    for k in dic.keys():
        test_constants_copy[k] = dic[k]
    db_path = "test-wallet-db" + token_bytes(32).hex() + ".db"
    if Path(db_path).exists():
        Path(db_path).unlink()
    config["database_path"] = db_path;
    wallet = await WalletNode.create(
        config,
        key_config,
        override_constants=test_constants_copy,
        name="wallet1",
    )
    server = ChiaServer(port, wallet, NodeType.WALLET, name="wallet-server")
    wallet.set_server(server)

    yield (wallet, server)

    server.close_all()
    await wallet.wallet_state_manager.clear_all_stores()
    await wallet.wallet_state_manager.close_all_stores()
    wallet.wallet_state_manager.unlink_db()
    await server.await_closed()


async def setup_harvester(port, dic={}):
    config = load_config("config.yaml", "harvester")

    harvester = Harvester(config, bt.plot_config)
    server = ChiaServer(port, harvester, NodeType.HARVESTER)
    _ = await server.start_server(config["host"], None)

    yield (harvester, server)

    harvester._shutdown()
    server.close_all()
    await harvester._await_shutdown()
    await server.await_closed()


async def setup_farmer(port, dic={}):
    config = load_config("config.yaml", "farmer")
    pool_sk = bt.pool_sk
    pool_target = create_puzzlehash_for_pk(
        BLSPublicKey(bytes(pool_sk.get_public_key()))
    )
    wallet_sk = bt.wallet_sk
    wallet_target = create_puzzlehash_for_pk(
        BLSPublicKey(bytes(wallet_sk.get_public_key()))
    )

    key_config = {
        "wallet_sk": bytes(wallet_sk).hex(),
        "wallet_target": wallet_target.hex(),
        "pool_sks": [bytes(pool_sk).hex()],
        "pool_target": pool_target.hex(),
    }
    test_constants_copy = test_constants.copy()
    for k in dic.keys():
        test_constants_copy[k] = dic[k]

    farmer = Farmer(config, key_config, test_constants_copy)
    server = ChiaServer(port, farmer, NodeType.FARMER)
    _ = await server.start_server(config["host"], farmer._on_connect)

    yield (farmer, server)

    server.close_all()
    await server.await_closed()


async def setup_introducer(port, dic={}):
    config = load_config("config.yaml", "introducer")

    introducer = Introducer(config)
    server = ChiaServer(port, introducer, NodeType.INTRODUCER)
    _ = await server.start_server(port, None)

    yield (introducer, server)

    server.close_all()
    await server.await_closed()


async def setup_vdf_clients(port):
    vdf_task = asyncio.create_task(spawn_process("127.0.0.1", port, 1))

    yield vdf_task

    await kill_processes()


async def setup_timelord(port, dic={}):
    config = load_config("config.yaml", "timelord")
    test_constants_copy = test_constants.copy()
    for k in dic.keys():
        test_constants_copy[k] = dic[k]
    timelord = Timelord(config, test_constants_copy)
    server = ChiaServer(port, timelord, NodeType.TIMELORD)
    _ = await server.start_server(port, None, config)

    coro = asyncio.start_server(
        timelord._handle_client,
        config["vdf_server"]["host"],
        config["vdf_server"]["port"],
        loop=asyncio.get_running_loop(),
    )

    vdf_server = asyncio.ensure_future(coro)

    async def run_timelord():
        async for msg in timelord._manage_discriminant_queue():
            server.push_message(msg)

    timelord_task = asyncio.create_task(run_timelord())

    yield (timelord, server)

    vdf_server.cancel()
    server.close_all()
    await timelord._shutdown()
    await timelord_task
    await server.await_closed()


async def setup_two_nodes(dic={}):
    """
    Setup and teardown of two full nodes, with blockchains and separate DBs.
    """
    node_iters = [
        setup_full_node("blockchain_test.db", 21234, dic=dic),
        setup_full_node("blockchain_test_2.db", 21235, dic=dic),
    ]

    fn1, s1 = await node_iters[0].__anext__()
    fn2, s2 = await node_iters[1].__anext__()

    yield (fn1, fn2, s1, s2)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_node_and_wallet(dic={}):
    node_iters = [
        setup_full_node_simulator("blockchain_test.db", 21234, dic=dic),
        setup_wallet_node(21235, dic=dic),
    ]

    full_node, s1 = await node_iters[0].__anext__()
    wallet, s2 = await node_iters[1].__anext__()

    yield (full_node, wallet, s1, s2)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_node_simulator_and_wallet(dic={}):
    node_iters = [
        setup_full_node_simulator("blockchain_test.db", 21234, dic=dic),
        setup_wallet_node(21235, dic=dic),
    ]

    full_node, s1 = await node_iters[0].__anext__()
    wallet, s2 = await node_iters[1].__anext__()

    yield (full_node, wallet, s1, s2)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_node_and_two_wallets(dic={}):
    node_iters = [
        setup_full_node("blockchain_test.db", 21234, dic=dic),
        setup_wallet_node(21235, key_seed=b"Test node 1", dic=dic),
        setup_wallet_node(21236, key_seed=b"Test node 2", dic=dic),
    ]

    full_node, s1 = await node_iters[0].__anext__()
    wallet, s2 = await node_iters[1].__anext__()
    wallet_2, s3 = await node_iters[2].__anext__()

    yield (full_node, wallet, wallet_2, s1, s2, s3)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_node_simulator_and_two_wallets(dic={}):
    node_iters = [
        setup_full_node_simulator("blockchain_test.db", 21234, dic=dic),
        setup_wallet_node(21235, key_seed=b"Test node 1", dic=dic),
        setup_wallet_node(21236, key_seed=b"Test node 2", dic=dic),
    ]

    full_node, s1 = await node_iters[0].__anext__()
    wallet, s2 = await node_iters[1].__anext__()
    wallet_2, s3 = await node_iters[2].__anext__()

    yield (full_node, wallet, wallet_2, s1, s2, s3)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_three_simulators_and_two_wallets(dic={}):
    node_iters = [
        setup_full_node_simulator("blockchain_test0.db", 21234, dic=dic),
        setup_full_node_simulator("blockchain_test1.db", 21235, dic=dic),
        setup_full_node_simulator("blockchain_test2.db", 21236, dic=dic),
        setup_wallet_node(21237, key_seed=b"Test node 1", dic=dic),
        setup_wallet_node(21238, key_seed=b"Test node 2", dic=dic),
    ]

    full_node0, s0 = await node_iters[0].__anext__()
    full_node1, s1 = await node_iters[1].__anext__()
    full_node2, s2 = await node_iters[2].__anext__()

    wallet_0, s3 = await node_iters[3].__anext__()
    wallet_1, s4 = await node_iters[4].__anext__()

    full_nodes = [(full_node0, s0), (full_node1, s1), (full_node2, s2)]
    wallets = [(wallet_0, s3), (wallet_1, s4)]
    yield (full_nodes, wallets)

    for node_iter in node_iters:
        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass


async def setup_full_system(dic={}):
    node_iters = [
        setup_introducer(21233),
        setup_harvester(21234),
        setup_farmer(21235),
        setup_timelord(21236),
        setup_vdf_clients(8000),
        setup_full_node("blockchain_test.db", 21237, 21233, dic),
        setup_full_node("blockchain_test_2.db", 21238, 21233, dic),
    ]

    introducer, introducer_server = await node_iters[0].__anext__()
    harvester, harvester_server = await node_iters[1].__anext__()
    farmer, farmer_server = await node_iters[2].__anext__()
    timelord, timelord_server = await node_iters[3].__anext__()
    vdf = await node_iters[4].__anext__()
    node1, node1_server = await node_iters[5].__anext__()
    node2, node2_server = await node_iters[6].__anext__()

    await harvester_server.start_client(
        PeerInfo(farmer_server._host, uint16(farmer_server._port)), None
    )
    await farmer_server.start_client(
        PeerInfo(node1_server._host, uint16(node1_server._port)), None
    )

    await timelord_server.start_client(
        PeerInfo(node1_server._host, uint16(node1_server._port)), None
    )

    yield (node1, node2)

    for node_iter in node_iters:

        try:
            await node_iter.__anext__()
        except StopAsyncIteration:
            pass
