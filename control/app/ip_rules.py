from functools import lru_cache
from ipaddress import IPv6Address, IPv6Network, ip_address, ip_network


def client_address(value: str | None):
    if not value or "%" in value:
        return None
    try:
        address = ip_address(value)
    except ValueError:
        return None
    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def canonical_ip(value: str | None) -> str | None:
    address = client_address(value)
    return str(address) if address is not None else None


def canonical_rule(value: str) -> str:
    value = value.strip()
    address_text, slash, length = value.partition("/")
    if not slash:
        if address := canonical_ip(value):
            return address
        raise ValueError("请输入有效的 IPv4、IPv6 或 CIDR")

    if "%" in address_text or not (length.isascii() and length.isdecimal()):
        raise ValueError("请输入有效的 CIDR，前缀长度须为数字")
    try:
        address = ip_address(address_text)
        network = ip_network(value, strict=False)
    except ValueError:
        raise ValueError("请输入有效的 IPv4 或 IPv6 CIDR") from None

    network_address = network.network_address
    if isinstance(network, IPv6Network):
        mapped = network.network_address.ipv4_mapped
        if isinstance(address, IPv6Address) and address.ipv4_mapped is not None and mapped is None:
            raise ValueError("IPv4 映射地址的 CIDR 前缀长度须为 96 到 128")
        if mapped is not None:
            network = ip_network((mapped, network.prefixlen - 96))

    if address != network_address:
        raise ValueError(f"CIDR 包含主机位，请填写网络地址 {network}")
    if network.prefixlen == network.max_prefixlen:
        return str(network.network_address)
    return str(network)


@lru_cache(maxsize=2048)
def rule_network(rule: str):
    try:
        return ip_network(rule)
    except ValueError:
        return None


def rule_prefix(rule: str, value: str | None) -> int:
    network = rule_network(rule)
    address = client_address(value)
    if network is not None and address is not None and address in network:
        return network.prefixlen
    return -1
