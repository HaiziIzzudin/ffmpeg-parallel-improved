"""
Unit tests for the FFmpeg runner — progress parsing, command building,
and split_args utility.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import pytest

from termux_spoke.ffmpeg_runner import (
    FfmpegProgress,
    FfmpegRunner,
    FfmpegResult,
    _split_args,
)


class TestSplitArgs:
    """Tests for the arg-splitting utility matching EncodingService.kt."""

    def test_simple_args(self) -> None:
        assert _split_args("-c:v libx264 -preset fast -an") == [
            "-c:v", "libx264", "-preset", "fast", "-an",
        ]

    def test_double_quoted_args(self) -> None:
        assert _split_args('-vf "scale=1280:720"') == [
            "-vf", "scale=1280:720",
        ]

    def test_single_quoted_args(self) -> None:
        assert _split_args("-vf 'scale=1280:720'") == [
            "-vf", "scale=1280:720",
        ]

    def test_mixed_quoted_and_unquoted(self) -> None:
        result = _split_args('-c:v libx264 -vf "scale=1280:720" -an')
        assert result == ["-c:v", "libx264", "-vf", "scale=1280:720", "-an"]

    def test_empty_string(self) -> None:
        assert _split_args("") == []

    def test_no_args(self) -> None:
        assert _split_args("-an") == ["-an"]


class TestFfmpegRunnerBuildCommand:
    """Tests for the command-building logic."""

    def test_basic_command(self) -> None:
        runner = FfmpegRunner(
            task_id="chunk_00001",
            input_path="/tmp/input.mp4",
            output_path="/tmp/out.mp4",
            start_time=5.120,
            duration=4.760,
            ffmpeg_args="-c:v libx264 -preset fast -an",
            binary="ffmpeg",
        )
        cmd = runner._build_command()
        # shutil.which may resolve to a full path (e.g. /usr/bin/ffmpeg or .../ffmpeg.EXE)
        assert "ffmpeg" in cmd[0].lower()
        assert "-y" in cmd
        assert "-ss" in cmd
        assert cmd[cmd.index("-ss") + 1] == "5.120"
        assert "-i" in cmd
        i_idx = cmd.index("-i")
        # The path gets os.path.abspath'd, which on Windows adds C:\ prefix
        assert "input.mp4" in cmd[i_idx + 1]
        assert "-t" in cmd
        assert cmd[cmd.index("-t") + 1] == "4.760"
        assert "-c:v" in cmd
        assert "libx264" in cmd
        assert "-progress" in cmd
        assert "pipe:1" in cmd
        # Output path is os.path.abspath'd (Windows adds C:\ prefix)
        assert "out.mp4" in cmd[-1]

    def test_command_with_quoted_args(self) -> None:
        runner = FfmpegRunner(
            task_id="t1",
            input_path="/tmp/i.mp4",
            output_path="/tmp/o.mp4",
            start_time=0,
            duration=10,
            ffmpeg_args='-vf "scale=1280:720" -c:v libx264',
            binary="ffmpeg",
        )
        cmd = runner._build_command()
        vf_idx = cmd.index("-vf")
        assert cmd[vf_idx + 1] == "scale=1280:720"


class TestFfmpegRunnerProgressParsing:
    """Tests for the progress line parser."""

    def test_out_time_us(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        runner._parse_progress_line("out_time_us=5120000\n", state)
        assert state["time_us"] == 5120000

    def test_speed_parse(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        runner._parse_progress_line("speed=  2.45x\n", state)
        assert abs(state["speed"] - 2.45) < 0.01

    def test_speed_negative_zero(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        runner._parse_progress_line("speed=-0.000x\n", state)
        assert state["speed"] == 0.0

    def test_fps_parse(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        runner._parse_progress_line("fps=45.7\n", state)
        assert abs(state["fps"] - 45.7) < 0.01

    def test_progress_end(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        is_end = runner._parse_progress_line("progress=end\n", state)
        assert is_end is True

    def test_progress_continue(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        is_end = runner._parse_progress_line("progress=continue\n", state)
        assert is_end is False

    def test_bitrate_parse(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}
        # bitrate from ffmpeg is often "1234.5kbits/s" — float() fails, so stays 0
        runner._parse_progress_line("bitrate=1234.5kbits/s\n", state)
        assert state["bitrate"] == 0.0

    def test_percent_computation(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        # 5 seconds out of 10 = 50%
        pct = runner._compute_percent(5_000_000)
        assert abs(pct - 50.0) < 0.01

    def test_percent_zero_duration(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 0, "-an", binary="ffmpeg")
        pct = runner._compute_percent(5_000_000)
        assert pct == 0.0

    def test_percent_capped_at_100(self) -> None:
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        pct = runner._compute_percent(15_000_000)  # 15s > 10s duration
        assert abs(pct - 100.0) < 0.01


class TestFfmpegRunnerEdgeCases:
    """Tests for edge cases."""

    def test_input_not_found(self) -> None:
        """Runner should fail gracefully when input doesn't exist."""
        runner = FfmpegRunner(
            task_id="t1",
            input_path="/nonexistent/input.mp4",
            output_path="/tmp/out.mp4",
            start_time=0,
            duration=10,
            ffmpeg_args="-an",
            binary="ffmpeg",
        )

        result = asyncio.run(runner.run())
        assert not result.success
        assert "not found" in result.error_message

    def test_ffmpeg_not_found(self) -> None:
        """Runner should fail gracefully when ffmpeg binary doesn't exist."""
        runner = FfmpegRunner(
            task_id="t1",
            input_path="/tmp/input.mp4",
            output_path="/tmp/out.mp4",
            start_time=0,
            duration=10,
            ffmpeg_args="-an",
            binary="ffmpeg_does_not_exist_xyz",
        )

        # Create a temp file for the input
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            input_path = f.name
            f.write(b"fake mp4 data")

        runner.input_path = input_path
        result = asyncio.run(runner.run())
        assert not result.success
        assert "not found" in result.error_message or "ffmpeg" in result.error_message.lower()

        os.unlink(input_path)

    def test_abort_before_start(self) -> None:
        """Aborting before running should not crash."""
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")
        runner.abort()  # Should not raise

    def test_sparse_stdout_lines(self) -> None:
        """Runner should handle empty lines and non-progress lines gracefully."""
        runner = FfmpegRunner("t1", "/i.mp4", "/o.mp4", 0, 10, "-an", binary="ffmpeg")

        state: dict = {"time_us": 0, "speed": 0.0, "fps": 0.0, "bitrate": 0.0, "total_size": 0}

        # Empty line
        runner._parse_progress_line("", state)
        assert state["time_us"] == 0

        # Non-matching line
        runner._parse_progress_line("frame=  120\n", state)
        assert state["time_us"] == 0  # frame is not in our parser

        # Malformed line
        runner._parse_progress_line("=garbage\n", state)
        assert state["time_us"] == 0


class TestFfmpegProgressDataclass:
    """Tests for the dataclass."""

    def test_default_values(self) -> None:
        p = FfmpegProgress()
        assert p.time_us == 0
        assert p.speed == 0.0
        assert p.fps == 0.0
        assert p.percent == 0.0
        assert p.bitrate == 0.0
        assert p.total_size == 0

    def test_custom_values(self) -> None:
        p = FfmpegProgress(
            time_us=5_000_000,
            speed=2.5,
            fps=30.0,
            percent=50.0,
            bitrate=1000.0,
            total_size=1024,
        )
        assert p.time_us == 5_000_000
        assert p.speed == 2.5


class TestFfmpegResultDataclass:
    """Tests for the result dataclass."""

    def test_success_result(self) -> None:
        r = FfmpegResult(
            task_id="chunk_1",
            success=True,
            return_code=0,
            final_speed=2.5,
            final_fps=30.0,
            output_path="/tmp/out.mp4",
        )
        assert r.success
        assert r.final_speed == 2.5
        assert r.output_path == "/tmp/out.mp4"

    def test_failure_result(self) -> None:
        r = FfmpegResult(
            task_id="chunk_1",
            success=False,
            return_code=1,
            error_message="FFmpeg exited with code 1",
        )
        assert not r.success
        assert "code 1" in r.error_message
        assert r.output_path == ""