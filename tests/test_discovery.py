"""
Tests for the discovery module — Zeroconf service browsing.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from termux_spoke.discovery import HubBrowser, get_local_ip


class TestHubBrowser:
    """Tests for the HubBrowser class."""

    def test_init(self) -> None:
        """Browser should initialize without starting."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)
        assert not browser.is_running

    def test_start_stop(self) -> None:
        """Browser should handle start/stop lifecycle."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)

        with patch("zeroconf.Zeroconf") as mock_zc_class:
            mock_zc = MagicMock()
            mock_zc_class.return_value = mock_zc

            browser.start()
            assert browser._zeroconf is not None
            assert browser._browser is not None
            assert browser.is_running

            browser.stop()
            assert not browser.is_running

    def test_service_found_callback(self) -> None:
        """When a service is found and resolved, callback should fire."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)

        # Simulate the resolve callback with a mock zeroconf
        # whose get_service_info returns a properly shaped MagicMock
        mock_zc = MagicMock()
        mock_info = MagicMock()
        mock_info.addresses_by_version.return_value = ["192.168.1.100"]
        mock_info.port = 8000
        mock_zc.get_service_info.return_value = mock_info

        browser._resolve_service(mock_zc, "FFmpegHub._ffmpeg-hub._tcp.local.")

        # With a proper mock, the service should be discovered
        assert "FFmpegHub._ffmpeg-hub._tcp.local." in browser._discovered_hubs
        callback.assert_called_once_with("192.168.1.100", 8000)

    def test_service_found_callback_none_info(self) -> None:
        """When get_service_info returns None, callback should not fire."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)

        mock_zc = MagicMock()
        mock_zc.get_service_info.return_value = None

        browser._resolve_service(mock_zc, "TestService._ffmpeg-hub._tcp.local.")

        # Service should NOT be added
        assert len(browser._discovered_hubs) == 0
        callback.assert_not_called()

    def test_stop_multiple_safe(self) -> None:
        """Calling stop multiple times should not raise."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)
        browser.stop()
        browser.stop()  # second call should be safe

    def test_double_start(self) -> None:
        """Calling start twice should be safe (stops first)."""
        callback = MagicMock()
        browser = HubBrowser(on_hub_found=callback)

        with patch("zeroconf.Zeroconf") as mock_zc_class:
            mock_zc = MagicMock()
            mock_zc_class.return_value = mock_zc

            browser.start()
            browser.start()  # should stop previous and start new

    def test_service_remove(self) -> None:
        """Removed service should fire the lost callback."""
        lost_callback = MagicMock()
        browser = HubBrowser(on_hub_found=MagicMock(), on_hub_lost=lost_callback)

        browser._discovered_hubs["TestHub"] = ("192.168.1.100", 8000)
        browser._remove_service("TestHub")
        lost_callback.assert_called_once_with("192.168.1.100", 8000)

    def test_service_remove_no_callback(self) -> None:
        """Removed service without lost callback should not crash."""
        browser = HubBrowser(on_hub_found=MagicMock())
        browser._discovered_hubs["TestHub"] = ("192.168.1.100", 8000)
        browser._remove_service("TestHub")  # Should not raise


class TestGetLocalIp:
    """Tests for the get_local_ip utility."""

    def test_get_local_ip_returns_string(self) -> None:
        """Should return a string (may be 127.0.0.1 on machines without network)."""
        ip = get_local_ip()
        assert isinstance(ip, str)
        assert len(ip) > 0
        # Should be a valid IP format with dots
        assert "." in ip