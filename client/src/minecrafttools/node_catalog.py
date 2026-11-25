from minecrafttools.config import Node

try:
    from _minecrafttools_build_nodes import NODES as built_nodes
except ModuleNotFoundError as error:
    if error.name != "_minecrafttools_build_nodes":
        raise
    built_nodes = ()

NODES: tuple[Node, ...] = built_nodes
