import asyncio
from typing import List, Union

from . import rpc
from .config import asize, ksize, this_node
from .node import ID, Node
from .routing import RoutingTable


class ValueFound(Exception):
    pass


class Server:
    def __init__(self):
        self.routing_table = RoutingTable()
        self.storage = {}

    async def start(self, known_nodes: List[Node] = None):
        # setup the RPC
        s = rpc.Server()

        @s.register
        def ping() -> str:
            return 'pong'

        @s.register
        def store(key, value) -> None:
            self.storage[key] = value

        @s.register
        def find_node(id: ID) -> List[Node]:
            return self.routing_table.get_nodes_nearby(id)

        @s.register
        def find_value(id: ID) -> Union[List[Node], bytes]:
            try:
                return self.storage[id]
            except KeyError:
                return find_node(id)

        def update(node: Node):
            self.routing_table.update(node)
        s.on_rpc = update

        await s.start()

        # join the network
        if known_nodes is None:
            return
        tasks = (node.rpc.find_node(this_node) for node in known_nodes)
        res = await asyncio.gather(*tasks, return_exceptions=True)
        for idx, new_nodes in res:
            if isinstance(new_nodes, rpc.NetworkError):
                print(f'{known_nodes[idx]} failed to connect.')
            else:
                self.routing_table.update(known_nodes[idx])
                for node in new_nodes:
                    self.routing_table.update(node)

    async def _query(self, nodes: List[Node], id: ID,
                     rpc_func: str, sem: asyncio.Semaphore) -> List[Node]:
        """Query the given k nodes, then merge their results and
        return the k nodes that are closest to id.
        """
        async def query_one_node(node):
            nonlocal nodes
            try:
                with (await sem):
                    res = await getattr(node, rpc_func)(id)
            except rpc.NetworkError:
                nodes.remove(node)
            else:
                if isinstance(res, bytes):
                    raise ValueFound(res)
                nodes += res

        fs = (asyncio.ensure_future(query_one_node(node)) for node in nodes)
        try:
            await asyncio.gather(*fs)
        except ValueFound:
            for f in fs:
                f.cancel()
            raise
        else:
            nodes.sort(key=lambda n: n.id ^ id)
            return nodes[:ksize]

    async def set(self, key: bytes, value: bytes) -> None:
        nodes = self.routing_table.get_nodes_nearby(id)
        await asyncio.gather(*(node.rpc.store(key, value) for node in nodes))

    async def get(self, key: ID) -> bytes:
        nodes = self.routing_table.get_nodes_nearby(key)
        sem = asyncio.Semaphore(asize)
        while True:
            try:
                res = await self._query(nodes.copy(), key, 'find_value', sem)
            except ValueFound as exc:
                return exc.args[0]
            else:
                if res == nodes:
                    break
                nodes = res
        return b'not found'

    async def lookup_node(self, id: ID) -> Node:
        nodes = self.routing_table.get_nodes_nearby(id)
        sem = asyncio.Semaphore(asize)
        while True:
            res = await self._query(nodes.copy(), id, 'find_node', sem)
            if res == nodes:
                break
            nodes = res
        return nodes
