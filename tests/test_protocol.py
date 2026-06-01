"""
Unit tests for the WebSocket protocol messages and state management.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from termux_spoke.client import SpokeClient, SpokeState


class TestSpokeState:
    """Tests for the state dataclass."""

    def test_default_state(self) -> None:
        state = SpokeState()
        assert state.connection_status == "Disconnected"
        assert state.worker_id == ""
        assert state.tasks_completed == 0
        assert state.tasks_failed == 0
        assert state.progress_percent == 0.0
        assert len(state.logs) == 0

    def test_to_dict(self) -> None:
        state = SpokeState(
            hub_host="192.168.1.100",
            hub_port=8000,
            connection_status="Registered",
            worker_id="abc-123",
            tasks_completed=5,
            progress_percent=67.3,
            speed=2.45,
            fps=30.0,
        )
        d = state.to_dict()
        assert d["hub_host"] == "192.168.1.100"
        assert d["connection_status"] == "Registered"
        assert d["tasks_completed"] == 5

    def test_logs_append(self) -> None:
        state = SpokeState()
        state.logs.append(("10:00:00", "CONNECT", "Connected to hub"))
        state.logs.append(("10:00:01", "REGISTER", "Registered as abc"))
        assert len(state.logs) == 2
        assert state.logs[0][1] == "CONNECT"
        assert state.logs[1][2] == "Registered as abc"


class TestSpokeClientInitialState:
    """Tests for initial client state."""

    def test_default_state(self) -> None:
        client = SpokeClient()
        state = client.get_state()
        assert state.connection_status == "Disconnected"
        assert state.worker_id == ""
        assert not client.is_running()

    def test_add_log(self) -> None:
        client = SpokeClient()
        client.add_log("TEST", "Test message")
        state = client.get_state()
        assert len(state.logs) == 1
        assert state.logs[0][1] == "TEST"
        assert state.logs[0][2] == "Test message"

    def test_add_log_format(self) -> None:
        """Log entries should have (timestamp, tag, message) format."""
        client = SpokeClient()
        client.add_log("CONNECT", "Connected")
        state = client.get_state()
        timestamp, tag, msg = state.logs[0]
        assert len(timestamp) == 8  # HH:MM:SS
        assert ":" in timestamp
        assert tag == "CONNECT"
        assert msg == "Connected"

    def test_log_bounded(self) -> None:
        """Log buffer should not exceed max size."""
        client = SpokeClient()
        for i in range(600):
            client.add_log("TEST", f"Message {i}")
        state = client.get_state()
        assert len(state.logs) <= 500

    def test_state_change_callback(self) -> None:
        """Setting a state change callback should not crash."""
        client = SpokeClient()
        calls = []

        def callback():
            calls.append(1)

        client.set_on_state_change(callback)
        client.add_log("TEST", "Hello")
        assert len(calls) >= 1


class TestSpokeStateChanges:
    """Tests for state transitions."""

    def test_uptime_increases(self) -> None:
        client = SpokeClient()
        state1 = client.get_state()
        time.sleep(0.1)
        state2 = client.get_state()
        assert state2.uptime_seconds > state1.uptime_seconds

    def test_progress_values(self) -> None:
        """Test that the live progress tracking fields work."""
        client = SpokeClient()

        # Simulate progress update via internal state
        client._live_progress_percent = 50.0
        client._live_speed = 2.5
        client._live_fps = 30.0

        state = client.get_state()
        assert state.progress_percent == 50.0
        assert state.speed == 2.5
        assert state.fps == 30.0

    def test_tasks_tracking(self) -> None:
        """Task counters should track correctly."""
        client = SpokeClient()
        client._tasks_completed = 3
        client._tasks_failed = 1

        state = client.get_state()
        assert state.tasks_completed == 3
        assert state.tasks_failed == 1