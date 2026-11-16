#!/usr/bin/env python3
"""Publish the existing Home Assistant mDNS service for the active interface."""

import signal
import socket
import sys
import time

from zeroconf import IPVersion, ServiceInfo, Zeroconf

signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
signal.signal(signal.SIGINT, lambda *_: sys.exit(0))


def get_primary_ip() -> str:
    """Return the source address selected by the existing route probe."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(1)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


local_ip = get_primary_ip()

zc = Zeroconf(ip_version=IPVersion.V4Only)


def make_service_info(ip: str, port: int = 8123) -> ServiceInfo:
    """Build the unchanged Home Assistant service announcement."""
    return ServiceInfo(
        "_home-assistant._tcp.local.",
        "Home Assistant._home-assistant._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=port,
        properties={},
        server="homeassistant.local.",
    )


info = make_service_info(local_ip)
zc.register_service(info)

while True:
    time.sleep(30)
    new_ip = get_primary_ip()
    if new_ip != local_ip:
        zc.unregister_service(info)
        local_ip = new_ip
        info = make_service_info(local_ip)
        zc.register_service(info)
