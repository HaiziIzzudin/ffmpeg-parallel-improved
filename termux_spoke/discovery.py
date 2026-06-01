"""
Zeroconf hub discovery for the Termux Spoke.

Uses ``python-zeroconf``'s ``ServiceBrowser`` to find ``_ffmpeg-hub._tcp.``
services on the local network (the inverse of what the PC Hub does in
``pc_hub/discovery.py``, which advertises the service).

Pure Python — no avahi or systemd dependency required.
"""
from __future__ import annotations

import logging
import socket
from typing import Callable, Optional

from zeroconf import IPVersion, ServiceBrowser, ServiceStateChange, Zeroconf

from termux_spoke import config

logger = logging.getLogger(__name__)


class HubBrowser:
    """
    Browse the local network for PC Hub instances advertising
    ``_ffmpeg-hub._tcp.`` services.

    When a hub is found, the ``on_hub_found`` callback is invoked with
    the resolved IP address (string) and port number (int).

    Usage::

        browser = HubBrowser(on_hub_found=my_callback)
        browser.start()
        # ... later ...
        browser.stop()
    """

    def __init__(
        self,
        on_hub_found: Callable[[str, int], None],
        on_hub_lost: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        """
        Args:
            on_hub_found: Called when a hub is discovered/resolved.
                Signature: ``(ip: str, port: int) -> None``
            on_hub_lost: Called when a previously-seen hub disappears.
                Signature: ``(ip: str, port: int) -> None``
        """
        self._on_hub_found = on_hub_found
        self._on_hub_lost = on_hub_lost
        self._zeroconf: Optional[Zeroconf] = None
        self._browser: Optional[ServiceBrowser] = None
        self._discovered_hubs: dict[str, tuple[str, int]] = {}  # name -> (ip, port)

    def start(self) -> None:
        """Start browsing for ``_ffmpeg-hub._tcp.local.`` services."""
        self.stop()

        logger.info("Starting Zeroconf browser for '%s'", config.SERVICE_TYPE_LOCAL)

        try:
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            self._browser = ServiceBrowser(
                self._zeroconf,
                config.SERVICE_TYPE_LOCAL,
                handlers=[self._on_service_state_change],
            )
            logger.info("Zeroconf browser started")
        except OSError as exc:
            logger.warning("Failed to start Zeroconf browser: %s", exc)
            # Network might not be available — this is non-fatal,
            # the user can still use manual IP.
            self._zeroconf = None
            self._browser = None

    def stop(self) -> None:
        """Stop browsing and close the Zeroconf instance."""
        if self._browser:
            try:
                self._browser.cancel()
            except Exception:
                pass
            self._browser = None

        if self._zeroconf:
            try:
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf = None

        self._discovered_hubs.clear()

    def _on_service_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Callback fired by ServiceBrowser on any service state change."""
        logger.debug("Service %s: %s", name, state_change)

        if state_change == ServiceStateChange.Added:
            self._resolve_service(zeroconf, name)
        elif state_change == ServiceStateChange.Removed:
            self._remove_service(name)
        elif state_change == ServiceStateChange.Updated:
            # Re-resolve in case IP/port changed
            self._resolve_service(zeroconf, name)

    def _resolve_service(self, zeroconf: Zeroconf, name: str) -> None:
        """Resolve a service info and fire the callback."""
        try:
            info = zeroconf.get_service_info(config.SERVICE_TYPE_LOCAL, name)
        except Exception as exc:
            logger.debug("Failed to get service info for %s: %s", name, exc)
            return

        if info is None:
            logger.debug("No service info returned for %s", name)
            return

        # Extract the first IPv4 address
        addresses = info.addresses_by_version(IPVersion.V4Only)
        if not addresses:
            logger.debug("No IPv4 address for service %s", name)
            return

        host = addresses[0].decode()
        port = info.port

        # Strip IPv6 zone index if present (matches NsdHelper.kt line 57)
        if "%" in host:
            host = host[: host.index("%")]

        self._discovered_hubs[name] = (host, port)
        logger.info("Discovered PC Hub at %s:%d (service: %s)", host, port, name)
        self._on_hub_found(host, port)

    def _remove_service(self, name: str) -> None:
        """Handle a service disappearing."""
        entry = self._discovered_hubs.pop(name, None)
        if entry and self._on_hub_lost:
            host, port = entry
            logger.info("PC Hub lost: %s:%d (service: %s)", host, port, name)
            self._on_hub_lost(host, port)

    @property
    def is_running(self) -> bool:
        """``True`` if the browser has been started and not stopped."""
        return self._browser is not None


def get_local_ip() -> str:
    """Get the active local IP address, for informational display."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.254.254.254", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()