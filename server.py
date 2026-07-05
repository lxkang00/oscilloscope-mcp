#!/usr/bin/env python3
"""
Siglent SDS2000X Plus Oscilloscope MCP Server
=============================================
A Model Context Protocol server for remote control of Siglent SDS2000X Plus
series oscilloscopes via TCP/IP (SCPI over socket on port 5024).

Based on: SDS2000X Plus Programming Guide CN11G
Protocol: IEEE 488.2 + SCPI over raw TCP socket (port 5024)

Compatible with: SDS1000X/E, SDS2000X/Plus/HD, SDS5000X, SDS6000A

Usage:
    pip install mcp
    python server.py
    (then configure in .mcp.json or Claude Code MCP settings)
"""

from __future__ import annotations

import socket
import time
import logging
import os
import re
from typing import Any, Callable, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

# ── Logging ──────────────────────────────────────────────────────────────────
LOG_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(LOG_DIR, "oscilloscope_mcp.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("oscilloscope_mcp")

# ── Constants ────────────────────────────────────────────────────────────────
SCPI_PORT = 5024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MB max for waveform data
READ_CHUNK = 4096
SAFE_FILENAME_RE = r"^[A-Za-z0-9 _-]{1,40}$"  # No path separators, quotes, or special chars
SAFE_LABEL_RE = r"^[A-Za-z0-9 _\-.+]{0,20}$"   # SCPI-safe characters, 20 char max

# Shared source enums for tool schemas (analog + math sources)
ANALOG_SOURCES = ["C1", "C2", "C3", "C4", "MATH", "F1", "F2", "F3", "F4"]
ALL_SOURCES = ANALOG_SOURCES + [
    "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7",
    "D8", "D9", "D10", "D11", "D12", "D13", "D14", "D15",
    "EXT", "EXT/5", "LINE",
]


# ── Input validation helpers ──────────────────────────────────────────────────

def _sanitize_label(text: str) -> str:
    """Escape double-quotes and enforce max 20 chars. Raises on SCPI delimiter."""
    if ";" in text:
        raise ValueError("SCPI delimiter ';' not allowed in label")
    sanitized = text.replace('"', "'").replace("\n", " ").replace("\r", "")
    if len(sanitized) > 20:
        raise ValueError(f"Label too long ({len(sanitized)} > 20 chars)")
    return sanitized or "CH"


def _validate_filename(name: str) -> str:
    """Validate and sanitize a filename. Raises ValueError if unsafe."""
    if not name:
        raise ValueError("Filename must not be empty")
    if not re.match(r"^[A-Za-z0-9 _-]{1,40}$", name):
        raise ValueError(
            f"Invalid filename '{name}'. Use only letters, numbers, "
            "spaces, hyphens, underscores (1-40 chars)."
        )
    return name


def _validate_host(host: str) -> str:
    """Basic host validation — rejects obviously dangerous inputs."""
    if not host or len(host) > 253:
        raise ValueError("Invalid host")
    # Reject raw IP with extra junk or protocol prefixes
    if host.startswith("http://") or host.startswith("https://"):
        raise ValueError("Host should be an IP address or hostname, not a URL")
    return host


# ═══════════════════════════════════════════════════════════════════════════════
# Oscilloscope Connection Manager
# ═══════════════════════════════════════════════════════════════════════════════
class OscilloscopeConnection:
    """Persistent TCP socket connection to the oscilloscope (SCPI port 5024)."""

    def __init__(self) -> None:
        self._sock: Optional[socket.socket] = None
        self._host: str = ""
        self._port: int = SCPI_PORT
        self._timeout: float = 10.0

    @property
    def connected(self) -> bool:
        return self._sock is not None

    @property
    def host(self) -> str:
        return self._host

    def connect(
        self, host: str, port: int = SCPI_PORT, timeout: float = 10.0
    ) -> str:
        """Establish TCP connection to oscilloscope."""
        self._dispose_socket()

        self._host = host
        self._port = port
        self._timeout = timeout

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((host, port))
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock

        # Drain any welcome banner
        banner = self._drain_banner()
        log.info("Connected to oscilloscope at %s:%d", host, port)
        msg = f"Connected to {host}:{port}"
        if banner:
            msg += f"\nBanner: {banner}"
        return msg

    def disconnect(self) -> str:
        """Close the connection."""
        if self._sock:
            self._dispose_socket()
            log.info("Disconnected from oscilloscope")
            return "Disconnected"
        return "Not connected"

    def send(self, command: str) -> str:
        """Send a SCPI command. Queries (with '?') return the response."""
        self._ensure_connected()
        cmd = command.strip()
        full = (cmd + "\n").encode("utf-8")
        log.debug("SEND: %s", cmd)
        self._sock.sendall(full)  # type: ignore[union-attr]
        if "?" in cmd:
            return self._read_response()
        return ""

    def query(self, command: str) -> str:
        """Send a SCPI query and return the response."""
        if not command.strip().endswith("?"):
            command = command.strip() + "?"
        return self.send(command)

    def safe_query(self, command: str, fallback: str = "N/A") -> str:
        """Query that never raises — returns fallback on error."""
        try:
            return self.query(command).strip()
        except Exception:
            return fallback

    # ── internals ───────────────────────────────────────────────────────────

    def _drain_banner(self) -> str:
        """Read any initial banner sent by the device on connect."""
        try:
            self._sock.settimeout(0.3)  # type: ignore[union-attr]
            data = self._sock.recv(READ_CHUNK)  # type: ignore[union-attr]
            self._sock.settimeout(self._timeout)  # type: ignore[union-attr]
            return data.decode("utf-8", errors="replace").strip()
        except (socket.timeout, OSError):
            self._sock.settimeout(self._timeout)  # type: ignore[union-attr]
            return ""

    def _read_response(self) -> str:
        """Read SCPI response from socket with max-size guard."""
        sock = self._sock
        sock.settimeout(self._timeout)  # type: ignore[union-attr]
        response = b""
        try:
            while len(response) < MAX_RESPONSE_BYTES:
                chunk = sock.recv(READ_CHUNK)  # type: ignore[union-attr]
                if not chunk:
                    break
                response += chunk
                if b"\n" in chunk:
                    break
                # After first chunk, shorten timeout for trailing data
                sock.settimeout(0.5)  # type: ignore[union-attr]
        except socket.timeout:
            pass
        finally:
            sock.settimeout(self._timeout)  # type: ignore[union-attr]

        result = response.decode("utf-8", errors="replace").strip()
        if len(result) > 200:
            log.debug("RECV: %d bytes", len(result))
        else:
            log.debug("RECV: %s", result)
        return result

    def _dispose_socket(self) -> None:
        """Safely close and clear the socket."""
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def _ensure_connected(self) -> None:
        if not self._sock:
            raise RuntimeError(
                "Not connected to oscilloscope. Use the 'connect' tool first."
            )


# ── Global connection ────────────────────────────────────────────────────────
conn = OscilloscopeConnection()


# ═══════════════════════════════════════════════════════════════════════════════
# Tool definitions — stored as a list of (name, description, schema) tuples
# so list_tools() and handler dispatch stay in sync via HANDLERS dict.
# ═══════════════════════════════════════════════════════════════════════════════

TOOLS: list[dict[str, Any]] = [
    # ── Connection ──────────────────────────────────────────────────────────
    {
        "name": "connect",
        "description": "Connect to oscilloscope via TCP/IP (SCPI port 5024). Call this FIRST.",
        "schema": {
            "type": "object",
            "properties": {
                "host": {"type": "string", "description": "IP address or hostname of oscilloscope"},
                "timeout": {"type": "number", "description": "Timeout in seconds (default 10)", "default": 10.0},
            },
            "required": ["host"],
        },
    },
    {
        "name": "disconnect",
        "description": "Disconnect from oscilloscope.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_command",
        "description": "Send any raw SCPI command. Queries (ending with ?) return response. Use this for commands not covered by other tools.",
        "schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "SCPI command, e.g. '*IDN?', ':CHANnel1:SCALe 0.5'"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "get_id",
        "description": "Get oscilloscope identity: manufacturer, model, serial, firmware.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "reset",
        "description": "Factory reset (*RST). Takes ~3 seconds.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Run control ─────────────────────────────────────────────────────────
    {
        "name": "run",
        "description": "Start (arm) the acquisition. Equivalent to pressing Run button.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop",
        "description": "Stop the acquisition. Equivalent to pressing Stop button.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Channel ─────────────────────────────────────────────────────────────
    {
        "name": "configure_channel",
        "description": "Configure an analog channel: display, scale, offset, coupling, BW limit, probe, invert, label, unit, skew.",
        "schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "integer", "description": "Channel 1-4", "minimum": 1, "maximum": 4},
                "display": {"type": "boolean", "description": "Show (true) or hide (false) channel"},
                "scale": {"type": "number", "description": "Vertical scale in V/div, e.g. 0.5 for 500mV/div"},
                "offset": {"type": "number", "description": "Vertical offset in Volts"},
                "coupling": {"type": "string", "enum": ["D1M", "A1M", "D50", "GND"],
                             "description": "D1M=1MΩ DC, A1M=1MΩ AC, D50=50Ω DC, GND=Ground"},
                "bandwidth_limit": {"type": "string", "enum": ["OFF", "20M", "200M", "350M"]},
                "probe": {"type": "number", "description": "Probe attenuation, e.g. 1, 10, 100"},
                "invert": {"type": "boolean"},
                "label": {"type": "string", "description": "Custom label, max 20 chars"},
                "unit": {"type": "string", "enum": ["V", "A"]},
                "skew": {"type": "number", "description": "Deskew in seconds, e.g. 1e-9"},
            },
            "required": ["channel"],
        },
    },
    {
        "name": "get_channel",
        "description": "Read current settings of an analog channel.",
        "schema": {
            "type": "object",
            "properties": {"channel": {"type": "integer", "description": "Channel 1-4", "minimum": 1, "maximum": 4}},
            "required": ["channel"],
        },
    },
    # ── Timebase ────────────────────────────────────────────────────────────
    {
        "name": "configure_timebase",
        "description": "Set horizontal timebase: scale (s/div), delay, mode (MAIN/WINDOW/ROLL/XY).",
        "schema": {
            "type": "object",
            "properties": {
                "scale": {"type": "number", "description": "Time/div in seconds, e.g. 1e-3 for 1ms/div"},
                "delay": {"type": "number", "description": "Horizontal offset in seconds"},
                "mode": {"type": "string", "enum": ["MAIN", "WINDOW", "ROLL", "XY"]},
            },
        },
    },
    {
        "name": "get_timebase",
        "description": "Read current timebase settings.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Trigger ─────────────────────────────────────────────────────────────
    {
        "name": "configure_trigger",
        "description": "Configure trigger: sweep mode, type, source, level, slope, coupling, holdoff.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["AUTO", "NORMal", "SINGle", "STOP"],
                         "description": "Sweep mode"},
                "trigger_type": {"type": "string",
                                 "enum": ["EDGE", "GLIT", "INTV", "DROP", "RUNT", "WIND",
                                          "PATT", "Qualified", "NthEdge", "SETUPHOLD", "VIDeo", "SERial"]},
                "source": {"type": "string",
                           "enum": ["C1","C2","C3","C4","EXT","EXT/5","LINE",
                                    "D0","D1","D2","D3","D4","D5","D6","D7",
                                    "D8","D9","D10","D11","D12","D13","D14","D15"]},
                "level": {"type": "number", "description": "Trigger level in Volts"},
                "slope": {"type": "string", "enum": ["POSitive", "NEGative", "EITHer"]},
                "coupling": {"type": "string", "enum": ["DC", "AC", "HFR", "LFR"]},
                "holdoff": {"type": "number", "description": "Holdoff time in seconds"},
            },
        },
    },
    {
        "name": "get_trigger",
        "description": "Read current trigger settings.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "force_trigger",
        "description": "Force immediate trigger event.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Acquisition ─────────────────────────────────────────────────────────
    {
        "name": "configure_acquisition",
        "description": "Configure acquisition: mode, memory depth, interpolation, averaging count.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["NORMal", "PEAK", "AVERage", "ERES"]},
                "memory_depth": {"type": "string",
                                 "enum": ["10K","100K","1M","10M","25M","50M","100M","200M"]},
                "interpolation": {"type": "string", "enum": ["LINEAR", "SINX"]},
                "average_count": {"type": "integer", "description": "Averages (4-1024), AVERage mode only"},
            },
        },
    },
    {
        "name": "get_acquisition",
        "description": "Read current acquisition settings including live sample rate.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Measurements ────────────────────────────────────────────────────────
    {
        "name": "measure",
        "description": "Take an automatic measurement. 50+ types: VPP, FREQ, VRMS, RISE, FALL, DUTY, PER, PWIDth, DELay, PHASe, ALL, etc.",
        "schema": {
            "type": "object",
            "properties": {
                "measurement": {
                    "type": "string",
                    "enum": [
                        "VPP","VMAX","VMIN","VAMP","VTOP","VBASe","VAVG","VRMS",
                        "OVSN","FPREshoot","PER","FREQ","RTIM","FTIM",
                        "PWIDth","NWIDth","DUTY","NDUTY","PDUTY",
                        "WIDth","RISE","FALL","DELay","PHASe","ALL",
                        "AMP","BASE","CMEAN","CRMS",
                        "CYCLE_MEAN","CYCLE_RMS","CYCLE_STDDEV",
                        "DELTA_DELAY","DELTA_TCCJITTER","DELTA_TCNJITTER",
                        "DELTA_TIEJITTER","DELTA_TPERIODJITTER",
                        "DELTA_TSKEW","DELTA_TTRIGLEVEL",
                        "FOV","FPOV","HPO","IREC","ISEC",
                        "LOW","MAX","MEAN","MIN","NCYC",
                        "NOV","NPO","NSEC","OVR","PHA",
                        "PKPK","PPUL","PREC","PWID","RPRE","STDDEV",
                    ],
                },
                "source1": {"type": "string", "description": "Primary source", "default": "C1",
                             "enum": ANALOG_SOURCES},
                "source2": {"type": "string", "description": "Secondary source (delay/phase)",
                             "enum": ANALOG_SOURCES},
            },
            "required": ["measurement"],
        },
    },
    {
        "name": "get_measure_stats",
        "description": "Get measurement statistics: count, mean, min, max, stddev.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "clear_measure_stats",
        "description": "Reset measurement statistics.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Waveform ────────────────────────────────────────────────────────────
    {
        "name": "get_waveform_preamble",
        "description": "Get waveform metadata (format, points, X/Y increments/origins) for a source.",
        "schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source", "default": "C1", "enum": ANALOG_SOURCES},
            },
            "required": ["source"],
        },
    },
    {
        "name": "get_waveform_data",
        "description": "Get waveform voltage data. Use max_points to decimate large datasets. Returns summary with min/max/mean and first/last values.",
        "schema": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Source", "default": "C1", "enum": ANALOG_SOURCES},
                "max_points": {"type": "integer", "description": "Max data points (decimate if less than total)"},
            },
            "required": ["source"],
        },
    },
    # ── Screenshot ──────────────────────────────────────────────────────────
    {
        "name": "get_screenshot",
        "description": "Capture the oscilloscope screen and return PNG data as base64-encoded string. Also saves to a local file.",
        "schema": {
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "Output filename (optional, auto-named if omitted)"},
                "invert": {"type": "boolean", "description": "Invert colors (white background). Default: false (dark background)"},
                "white_background": {"type": "boolean", "description": "Use white background for printing. Default: false"},
            },
        },
    },
    # ── Display ─────────────────────────────────────────────────────────────
    {
        "name": "configure_display",
        "description": "Configure display: grid, persistence, brightness, grid style, axes.",
        "schema": {
            "type": "object",
            "properties": {
                "grid": {"type": "string", "enum": ["FULL", "HALF", "OFF"]},
                "persistence": {"type": "string",
                                 "enum": ["OFF", "0.5", "1", "5", "10", "INF"],
                                 "description": "Persistence: OFF, time in seconds, or INF"},
                "intensity": {"type": "integer", "description": "Brightness 0-100", "minimum": 0, "maximum": 100},
                "grid_style": {"type": "string", "enum": ["LINE", "DOT"]},
                "axis_mode": {"type": "string", "enum": ["FULL", "MINImum"]},
            },
        },
    },
    # ── Cursors ─────────────────────────────────────────────────────────────
    {
        "name": "configure_cursors",
        "description": "Configure cursor measurements: mode, sources, type.",
        "schema": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["OFF", "MANual", "TRACk", "MEASure"]},
                "source1": {"type": "string", "description": "Primary source",
                             "enum": ANALOG_SOURCES},
                "source2": {"type": "string", "description": "Secondary source",
                             "enum": ANALOG_SOURCES},
                "cursor_type": {"type": "string", "enum": ["X", "Y", "XY"], "description": "Manual cursor type"},
            },
        },
    },
    {
        "name": "get_cursor_values",
        "description": "Read cursor delta X and delta Y values.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Math ────────────────────────────────────────────────────────────────
    {
        "name": "configure_math",
        "description": "Configure math trace: function (ADD/SUB/MULT/DIV/FFT/DIFF/INTG/SQRT/AVG/ERES/ABSS), sources, scale, FFT options.",
        "schema": {
            "type": "object",
            "properties": {
                "display": {"type": "boolean", "description": "Show/hide math trace"},
                "function": {"type": "string",
                             "enum": ["ADD","SUB","MULT","DIV","FFT","DIFF","INTG","SQRT","AVG","ERES","ABSS"]},
                "source1": {"type": "string", "description": "First operand",
                             "enum": ANALOG_SOURCES},
                "source2": {"type": "string", "description": "Second operand (ADD/SUB/MULT/DIV)",
                             "enum": ANALOG_SOURCES},
                "scale": {"type": "number", "description": "Math vertical scale"},
                "offset": {"type": "number", "description": "Math vertical offset"},
                "fft_window": {"type": "string", "enum": ["RECT","BLAC","HANN","HAMM","FLAT","KAIS"]},
                "fft_scale": {"type": "string", "enum": ["VRMS","DBVRMS","DBM"]},
            },
        },
    },
    # ── Digital channels (MSO models) ───────────────────────────────────────
    {
        "name": "configure_digital",
        "description": "Configure digital channels (MSO models only): display, position, height, threshold, labels.",
        "schema": {
            "type": "object",
            "properties": {
                "display": {"type": "boolean", "description": "Show/hide digital bus"},
                "position": {"type": "integer", "description": "Vertical screen position"},
                "height": {"type": "integer", "description": "Digital channel display height"},
                "label": {"type": "string", "description": "Label for a specific digital line, e.g. D0"},
                "label_text": {"type": "string", "description": "Label text value"},
                "threshold": {"type": "number", "description": "Logic threshold in Volts for a line, e.g. D0"},
                "threshold_line": {"type": "string", "description": "Digital line for threshold, e.g. D0"},
            },
        },
    },
    # ── Auto ────────────────────────────────────────────────────────────────
    {
        "name": "autoset",
        "description": "Auto-setup: automatically configure scope for connected signals.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Save / Recall ───────────────────────────────────────────────────────
    {
        "name": "save_setup",
        "description": "Save current setup to internal memory or USB.",
        "schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["INTERNAL", "USB"], "default": "INTERNAL"},
                "filename": {"type": "string", "description": "File name (no extension, saved as .xml)"},
            },
        },
    },
    {
        "name": "recall_setup",
        "description": "Recall saved setup from internal memory or USB.",
        "schema": {
            "type": "object",
            "properties": {
                "location": {"type": "string", "enum": ["INTERNAL", "USB"], "default": "INTERNAL"},
                "filename": {"type": "string", "description": "File name (no extension)"},
            },
        },
    },
    # ── Counter ─────────────────────────────────────────────────────────────
    {
        "name": "get_frequency_counter",
        "description": "Read hardware frequency counter: mode, source, frequency.",
        "schema": {"type": "object", "properties": {}},
    },
    # ── Status ──────────────────────────────────────────────────────────────
    {
        "name": "get_status",
        "description": "Get operational status: acquisition, trigger, OPC, event register.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_next_error",
        "description": "Read and clear next error from device error queue.",
        "schema": {"type": "object", "properties": {}},
    },
    {
        "name": "quick_snapshot",
        "description": "Take a quick overview snapshot: current VPP/FREQ on all active channels, timebase, trigger, and acquisition state.",
        "schema": {"type": "object", "properties": {}},
    },
]


# ═══════════════════════════════════════════════════════════════════════════════
# Helper: build format strings from args
# ═══════════════════════════════════════════════════════════════════════════════

def _apply_config(
    args: dict[str, Any],
    mapping: dict[str, str],
    *,
    prefix: str = "",
    suffix: str = "",
) -> list[str]:
    """Send SCPI commands for each key in `args` that matches `mapping`,
    returning a list of human-readable result lines.

    ``mapping`` maps arg keys to SCPI command suffixes.
    E.g. {"scale": "SCALe", "offset": "OFFSet"} with prefix=":CHANnel1:"
    sends ":CHANnel1:SCALe {value}", ":CHANnel1:OFFSet {value}".
    """
    results: list[str] = []
    for key, scpi in mapping.items():
        if key not in args:
            continue
        val = args[key]
        if isinstance(val, bool):
            val = "ON" if val else "OFF"
        cmd = f"{prefix}{scpi}{suffix} {val}"
        conn.send(cmd)
        results.append(f"{key}: {args[key]}")
    return results


# ═══════════════════════════════════════════════════════════════════════════════
# Tool handler functions
# ═══════════════════════════════════════════════════════════════════════════════

# ── Connection ──────────────────────────────────────────────────────────────

def h_connect(args: dict[str, Any]) -> str:
    host = _validate_host(args["host"])
    return conn.connect(host, SCPI_PORT, args.get("timeout", 10.0))


def h_disconnect(args: dict[str, Any]) -> str:
    return conn.disconnect()


def h_send_command(args: dict[str, Any]) -> str:
    return conn.send(args["command"])


def h_get_id(args: dict[str, Any]) -> str:
    return conn.query("*IDN?")


def h_reset(args: dict[str, Any]) -> str:
    conn.send("*RST")
    time.sleep(3)
    return "Oscilloscope reset complete (*RST). Reconnect if needed."


# ── Run control ─────────────────────────────────────────────────────────────

def h_run(args: dict[str, Any]) -> str:
    conn.send(":RUN")
    return "Acquisition running"


def h_stop(args: dict[str, Any]) -> str:
    conn.send(":STOP")
    return "Acquisition stopped"


# ── Channel ─────────────────────────────────────────────────────────────────

_CH_MAP: dict[str, str] = {
    "scale": "SCALe",
    "offset": "OFFSet",
    "coupling": "COUPling",
    "bandwidth_limit": "BWLimit",
    "probe": "PROBe",
    "skew": "SKEW",
    "unit": "UNIT",
}


def h_configure_channel(args: dict[str, Any]) -> str:
    ch = args["channel"]
    prefix = f":CHANnel{ch}:"
    lines: list[str] = []

    # display
    if "display" in args:
        val = "ON" if args["display"] else "OFF"
        conn.send(f"{prefix}SWITch {val}")
        lines.append(f"Display: {val}")

    # invert
    if "invert" in args:
        val = "ON" if args["invert"] else "OFF"
        conn.send(f"{prefix}INVert {val}")
        lines.append(f"Invert: {val}")

    # numeric/text settings via mapping
    for key, scpi in _CH_MAP.items():
        if key in args:
            conn.send(f"{prefix}{scpi} {args[key]}")
            lines.append(f"{key}: {args[key]}")

    # label (two commands)
    if "label" in args:
        safe = _sanitize_label(args["label"])
        conn.send(f'{prefix}LABel:TEXT "{safe}"')
        conn.send(f"{prefix}LABel ON")
        lines.append(f"label: {safe}")

    return f"Channel {ch} configured:\n" + "\n".join(f"  - {l}" for l in lines)


def h_get_channel(args: dict[str, Any]) -> str:
    ch = args["channel"]
    p = f":CHANnel{ch}:"
    props = {
        "Display": conn.safe_query(f"{p}SWITch?"),
        "Scale (V/div)": conn.safe_query(f"{p}SCALe?"),
        "Offset (V)": conn.safe_query(f"{p}OFFSet?"),
        "Coupling": conn.safe_query(f"{p}COUPling?"),
        "BW Limit": conn.safe_query(f"{p}BWLimit?"),
        "Probe": conn.safe_query(f"{p}PROBe?"),
        "Invert": conn.safe_query(f"{p}INVert?"),
        "Unit": conn.safe_query(f"{p}UNIT?"),
    }
    return f"Channel {ch}:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())


# ── Timebase ────────────────────────────────────────────────────────────────

def h_configure_timebase(args: dict[str, Any]) -> str:
    lines = _apply_config(args, {"scale": "SCALe", "delay": "DELay", "mode": "MODE"},
                          prefix=":TIMebase:")
    return "Timebase configured:\n" + "\n".join(f"  - {l}" for l in lines)


def h_get_timebase(args: dict[str, Any]) -> str:
    props = {
        "Scale (s/div)": conn.safe_query(":TIMebase:SCALe?"),
        "Delay (s)": conn.safe_query(":TIMebase:DELay?"),
        "Mode": conn.safe_query(":TIMebase:MODE?"),
    }
    return "Timebase:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())


# ── Trigger ─────────────────────────────────────────────────────────────────

def h_configure_trigger(args: dict[str, Any]) -> str:
    lines: list[str] = []

    if "mode" in args:
        conn.send(f":TRIGger:SWEep {args['mode']}")
        lines.append(f"Sweep: {args['mode']}")

    # Determine trigger type — set first so later commands use the right subsystem
    trig_type = args.get("trigger_type", "EDGE")

    if "trigger_type" in args:
        conn.send(f":TRIGger:TYPE {args['trigger_type']}")
        lines.append(f"Type: {args['trigger_type']}")
        trig_type = args["trigger_type"]

    # Source/level/slope/coupling go under the current trigger type subsystem
    trig_prefix = f":TRIGger:{trig_type}:"

    trig_map = {
        "source": "SOURce",
        "level": "LEVel",
        "slope": "SLOPe",
        "coupling": "COUPling",
    }
    for key, scpi in trig_map.items():
        if key in args:
            conn.send(f"{trig_prefix}{scpi} {args[key]}")
            lines.append(f"{key}: {args[key]}")

    if "holdoff" in args:
        conn.send(f":TRIGger:HOLDoff {args['holdoff']}")
        lines.append(f"Holdoff: {args['holdoff']} s")

    return "Trigger configured:\n" + "\n".join(f"  - {l}" for l in lines)


def h_get_trigger(args: dict[str, Any]) -> str:
    trig_type = conn.safe_query(":TRIGger:TYPE?", "EDGE")
    props = {
        "Type": trig_type,
        "Sweep": conn.safe_query(":TRIGger:SWEep?"),
        "Source": conn.safe_query(f":TRIGger:{trig_type}:SOURce?"),
        "Level": conn.safe_query(f":TRIGger:{trig_type}:LEVel?"),
        "Slope": conn.safe_query(f":TRIGger:{trig_type}:SLOPe?"),
    }
    return "Trigger:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())


def h_force_trigger(args: dict[str, Any]) -> str:
    conn.send("*TRG")
    return "Trigger forced"


# ── Acquisition ─────────────────────────────────────────────────────────────

def h_configure_acquisition(args: dict[str, Any]) -> str:
    acq_map = {
        "mode": "MODE",
        "memory_depth": "MDEPth",
        "interpolation": "INTerpolation",
    }
    lines = _apply_config(args, acq_map, prefix=":ACQuire:")
    if "average_count" in args:
        conn.send(f":ACQuire:AVERage {args['average_count']}")
        lines.append(f"average_count: {args['average_count']}")
    return "Acquisition configured:\n" + "\n".join(f"  - {l}" for l in lines)


def h_get_acquisition(args: dict[str, Any]) -> str:
    props = {
        "Mode": conn.safe_query(":ACQuire:MODE?"),
        "Memory Depth": conn.safe_query(":ACQuire:MDEPth?"),
        "Sample Rate": conn.safe_query(":ACQuire:SRATe?"),
        "Interpolation": conn.safe_query(":ACQuire:INTerpolation?"),
    }
    return "Acquisition:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())


# ── Measurements ────────────────────────────────────────────────────────────

def h_measure(args: dict[str, Any]) -> str:
    meas = args["measurement"]
    src1 = args.get("source1", "C1")
    src2 = args.get("source2")

    if meas == "ALL":
        result = conn.query(f":MEASure:ALL? {src1}")
        return f"All measurements for {src1}:\n{result}"

    src = f"{src1},{src2}" if src2 else src1
    value = conn.query(f":MEASure:{meas}? {src}")
    label = f"{meas}({src})"
    return f"{label} = {value}"


def h_get_measure_stats(args: dict[str, Any]) -> str:
    stats = {
        "Count": conn.safe_query(":MEASure:STATistics:COUNt?"),
        "Mean": conn.safe_query(":MEASure:STATistics:MEAN?"),
        "Min": conn.safe_query(":MEASure:STATistics:MIN?"),
        "Max": conn.safe_query(":MEASure:STATistics:MAX?"),
        "StdDev": conn.safe_query(":MEASure:STATistics:STDD?"),
    }
    return "Measurement Statistics:\n" + "\n".join(f"  {k}: {v}" for k, v in stats.items())


def h_clear_measure_stats(args: dict[str, Any]) -> str:
    conn.send(":MEASure:STATistics:RESet")
    return "Measurement statistics cleared"


# ── Waveform ────────────────────────────────────────────────────────────────

def h_get_waveform_preamble(args: dict[str, Any]) -> str:
    src = args["source"]
    conn.send(f":WAVeform:SOURce {src}")
    preamble = conn.query(":WAVeform:PREamble?").strip()
    parts = preamble.split(",")
    if len(parts) >= 10:
        return (
            f"Waveform Preamble for {src}:\n"
            f"  Format: {parts[0]}\n"
            f"  Type: {parts[1]}\n"
            f"  Points: {parts[2]}\n"
            f"  Count: {parts[3]}\n"
            f"  X Increment: {parts[4]} s\n"
            f"  X Origin: {parts[5]} s\n"
            f"  X Reference: {parts[6]}\n"
            f"  Y Increment: {parts[7]} V\n"
            f"  Y Origin: {parts[8]} V\n"
            f"  Y Reference: {parts[9]}"
        )
    return f"Raw preamble: {preamble}"


def h_get_waveform_data(args: dict[str, Any]) -> str:
    src = args["source"]
    max_points = args.get("max_points")

    conn.send(f":WAVeform:SOURce {src}")
    conn.send(":WAVeform:FORMat ASCII")

    preamble = conn.query(":WAVeform:PREamble?").strip()
    parts = preamble.split(",")
    if len(parts) >= 10:
        yinc = float(parts[7])
        yorigin = float(parts[8])
        yref = float(parts[9])
        total_points = int(float(parts[2]))
    else:
        yinc = yorigin = yref = 1.0
        total_points = 0

    if max_points and 0 < max_points < total_points:
        step = max(total_points // max_points, 1)
        conn.send(":WAVeform:STARt 1")
        conn.send(f":WAVeform:SPOints {max_points}")
        conn.send(f":WAVeform:INTerval {step}")

    raw_data = conn.query(":WAVeform:DATA?")
    values = raw_data.split(",")

    voltages: list[float] = []
    for val in values:
        try:
            v = float(val.strip())
            voltages.append(yorigin + yinc * (v - yref))
        except (ValueError, IndexError):
            pass

    summary = (
        f"Waveform Data for {src}:\n"
        f"  Total points in memory: {total_points}\n"
        f"  Points returned: {len(voltages)}\n"
        f"  Y Increment: {yinc} V\n"
    )
    if voltages:
        summary += (
            f"  Min: {min(voltages):.4f} V\n"
            f"  Max: {max(voltages):.4f} V\n"
            f"  Mean: {sum(voltages) / len(voltages):.4f} V\n"
            f"  First 10: {[round(v, 4) for v in voltages[:10]]}\n"
            f"  Last 10: {[round(v, 4) for v in voltages[-10:]]}"
        )
    return summary


# ── Screenshot ──────────────────────────────────────────────────────────────

def h_get_screenshot(args: dict[str, Any]) -> str:
    """Capture screen via SCDP command, save as PNG, return base64."""
    import base64
    from datetime import datetime

    # Configure screenshot
    conn.send(":SCDP:FORMAT PNG")

    # Color mode
    invert = args.get("invert", False)
    white_bg = args.get("white_background", False)
    if white_bg:
        conn.send(":SCDP:NORMal")
    elif invert:
        conn.send(":SCDP:INVert")
    else:
        conn.send(":SCDP:NORMal")  # default dark background

    # Capture
    conn.send(":SCDP")
    time.sleep(1)  # Wait for capture to complete

    # Read the image data
    conn.send(":SCDP:DATA?")
    # SCDP:DATA? returns: #<digit><length><binary data>\n
    sock = conn._sock
    sock.settimeout(conn._timeout)  # type: ignore[union-attr]

    # Read header: '#'
    header_start = sock.recv(1)  # type: ignore[union-attr]
    if header_start != b"#":
        # Maybe plain response — try reading as ASCII
        rest = b""
        try:
            sock.settimeout(0.5)  # type: ignore[union-attr]
            while True:
                c = sock.recv(1)  # type: ignore[union-attr]
                if not c:
                    break
                rest += c
                if c == b"\n":
                    break
        except socket.timeout:
            pass
        sock.settimeout(conn._timeout)  # type: ignore[union-attr]
        return f"Screenshot command response: {(header_start + rest).decode('utf-8', errors='replace')[:500]}"

    # Read digit count
    digit_byte = sock.recv(1)  # type: ignore[union-attr]
    try:
        num_digits = int(digit_byte.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        sock.settimeout(conn._timeout)  # type: ignore[union-attr]
        return f"Failed to parse SCDP header: got {header_start!r}{digit_byte!r}"

    # Read length
    length_bytes = b""
    while len(length_bytes) < num_digits:
        c = sock.recv(1)  # type: ignore[union-attr]
        if not c:
            break
        length_bytes += c
    try:
        data_length = int(length_bytes.decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        sock.settimeout(conn._timeout)  # type: ignore[union-attr]
        return f"Failed to parse SCDP data length: {length_bytes!r}"

    # Read the binary image data
    img_data = b""
    sock.settimeout(10.0)  # type: ignore[union-attr]
    while len(img_data) < data_length:
        chunk = sock.recv(min(READ_CHUNK, data_length - len(img_data)))  # type: ignore[union-attr]
        if not chunk:
            break
        img_data += chunk

    # Drain trailing newline
    try:
        sock.settimeout(0.2)  # type: ignore[union-attr]
        sock.recv(1)  # type: ignore[union-attr]
    except (socket.timeout, OSError):
        pass
    sock.settimeout(conn._timeout)  # type: ignore[union-attr]

    # Save to file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = args.get("filename", f"screenshot_{ts}.png")
    if not fname.endswith(".png"):
        fname += ".png"
    filepath = os.path.join(LOG_DIR, fname)
    with open(filepath, "wb") as f:
        f.write(img_data)

    b64 = base64.b64encode(img_data).decode("ascii")
    log.info("Screenshot saved to %s (%d bytes)", filepath, len(img_data))

    return (
        f"Screenshot captured ({len(img_data)} bytes)\n"
        f"Saved to: {filepath}\n"
        f"Base64 (first 200 chars): {b64[:200]}..."
    )


# ── Display ─────────────────────────────────────────────────────────────────

def h_configure_display(args: dict[str, Any]) -> str:
    disp_map = {
        "grid": "GRIDstyle",
        "persistence": "PERSistence",
        "intensity": "INTensity",
        "grid_style": "GRATicule",
    }
    lines = _apply_config(args, disp_map, prefix=":DISPlay:")
    if "axis_mode" in args:
        conn.send(f":DISPlay:AXIS:MODE {args['axis_mode']}")
        lines.append(f"axis_mode: {args['axis_mode']}")
    return "Display configured:\n" + "\n".join(f"  - {l}" for l in lines)


# ── Cursors ─────────────────────────────────────────────────────────────────

def h_configure_cursors(args: dict[str, Any]) -> str:
    cursor_map = {
        "mode": "MODE",
        "source1": "SOURce1",
        "source2": "SOURce2",
        "cursor_type": "TYPE",
    }
    lines = _apply_config(args, cursor_map, prefix=":CURSor:")
    return "Cursors configured:\n" + "\n".join(f"  - {l}" for l in lines)


def h_get_cursor_values(args: dict[str, Any]) -> str:
    mode = conn.safe_query(":CURSor:MODE?", "OFF")
    if mode == "OFF":
        return "Cursors are OFF"

    values: dict[str, str] = {}
    for key, cmd in [("Delta X", ":CURSor:XDELta?"), ("Delta Y", ":CURSor:YDELta?")]:
        try:
            values[key] = conn.query(cmd).strip()
        except Exception:
            values[key] = "N/A"
    return f"Cursor Values (Mode: {mode}):\n" + "\n".join(f"  {k}: {v}" for k, v in values.items())


# ── Math ────────────────────────────────────────────────────────────────────

def h_configure_math(args: dict[str, Any]) -> str:
    lines: list[str] = []

    if "display" in args:
        val = "ON" if args["display"] else "OFF"
        conn.send(f":MATH:SWITch {val}")
        lines.append(f"display: {args['display']}")

    math_map: dict[str, str] = {
        "function": "FUNCtion",
        "source1": "SOURce1",
        "source2": "SOURce2",
        "scale": "SCALe",
        "offset": "OFFSet",
    }
    lines.extend(_apply_config(args, math_map, prefix=":MATH:"))

    if "fft_window" in args:
        conn.send(f":MATH:FFT:WINDow {args['fft_window']}")
        lines.append(f"fft_window: {args['fft_window']}")
    if "fft_scale" in args:
        conn.send(f":MATH:FFT:SCALe {args['fft_scale']}")
        lines.append(f"fft_scale: {args['fft_scale']}")

    return "Math configured:\n" + "\n".join(f"  - {l}" for l in lines)


# ── Digital ─────────────────────────────────────────────────────────────────

def h_configure_digital(args: dict[str, Any]) -> str:
    lines: list[str] = []

    if "display" in args:
        val = "ON" if args["display"] else "OFF"
        conn.send(f":DIGital:DISPlay {val}")
        lines.append(f"display: {args['display']}")

    if "position" in args:
        conn.send(f":DIGital:POSition {args['position']}")
        lines.append(f"position: {args['position']}")

    if "height" in args:
        conn.send(f":DIGital:HEIGht {args['height']}")
        lines.append(f"height: {args['height']}")

    if "label" in args and "label_text" in args:
        safe_label = _sanitize_label(args["label"])
        safe_text = _sanitize_label(args["label_text"])
        conn.send(f':DIGital:LABel {safe_label},"{safe_text}"')
        lines.append(f"label {safe_label}: {safe_text}")

    if "threshold" in args and "threshold_line" in args:
        conn.send(f':DIGital:THReshold {args["threshold_line"]},{args["threshold"]}')
        lines.append(f"threshold {args['threshold_line']}: {args['threshold']}V")

    return "Digital channels configured:\n" + "\n".join(f"  - {l}" for l in lines)


# ── Auto ────────────────────────────────────────────────────────────────────

def h_autoset(args: dict[str, Any]) -> str:
    conn.send(":AUToset")
    time.sleep(2)
    return "Auto-setup complete"


# ── Save / Recall ───────────────────────────────────────────────────────────

def _make_path(location: str, filename: str) -> str:
    """Build a safe SCPI filesystem path. Validates the filename component."""
    fname = _validate_filename(filename)
    if not fname.endswith(".xml"):
        fname += ".xml"
    if location == "USB":
        return f"/usb0/{fname}"
    return f"/SDS2000X+/internal/{fname}"


def h_save_setup(args: dict[str, Any]) -> str:
    loc = args.get("location", "INTERNAL")
    fname = args.get("filename", "SETUP")
    path = _make_path(loc, fname)
    conn.send(f':STORe:SETup "{path}"')
    return f"Setup saved to {loc}: {fname}.xml"


def h_recall_setup(args: dict[str, Any]) -> str:
    loc = args.get("location", "INTERNAL")
    fname = args.get("filename", "SETUP")
    path = _make_path(loc, fname)
    conn.send(f':RECAll:SETup "{path}"')
    return f"Setup recalled from {loc}: {fname}.xml"


# ── Counter ─────────────────────────────────────────────────────────────────

def h_get_frequency_counter(args: dict[str, Any]) -> str:
    freq = conn.safe_query(":COUNter:CURRent?")
    mode = conn.safe_query(":COUNter:MODE?")
    source = conn.safe_query(":COUNter:SOURce?")
    return (
        f"Frequency Counter:\n"
        f"  Mode: {mode}\n"
        f"  Source: {source}\n"
        f"  Frequency: {freq} Hz"
    )


# ── Status ──────────────────────────────────────────────────────────────────

def h_get_status(args: dict[str, Any]) -> str:
    status = {
        "Acquisition State": conn.safe_query(":ACQuire:STATE?"),
        "Trigger State": conn.safe_query(":TRIGger:STATus?"),
        "Operation Complete": conn.safe_query("*OPC?"),
        "Event Status Register": conn.safe_query("*ESR?"),
    }
    return "Oscilloscope Status:\n" + "\n".join(f"  {k}: {v}" for k, v in status.items())


def h_get_next_error(args: dict[str, Any]) -> str:
    error = conn.safe_query(":SYSTem:ERRor?", "Could not read error queue")
    return f"Error: {error}"


# ── Quick Snapshot ──────────────────────────────────────────────────────────

def h_quick_snapshot(args: dict[str, Any]) -> str:
    """Take a quick overview: VPP+FREQ on all visible channels, + status."""
    lines: list[str] = ["=== Oscilloscope Quick Snapshot ===", ""]

    # ID
    lines.append(f"Device: {conn.safe_query('*IDN?', 'Unknown')}")
    lines.append(f"Connected to: {conn.host}")

    # Timebase
    tdiv = conn.safe_query(":TIMebase:SCALe?")
    lines.append(f"Timebase: {tdiv} s/div")

    # Trigger
    trig_type = conn.safe_query(":TRIGger:TYPE?", "EDGE")
    trig_source = conn.safe_query(f":TRIGger:{trig_type}:SOURce?")
    trig_level = conn.safe_query(f":TRIGger:{trig_type}:LEVel?")
    trig_sweep = conn.safe_query(":TRIGger:SWEep?")
    lines.append(f"Trigger: {trig_type} | Source: {trig_source} | Level: {trig_level}V | Sweep: {trig_sweep}")

    # Sample rate
    srate = conn.safe_query(":ACQuire:SRATe?")
    lines.append(f"Sample Rate: {srate} Sa/s")

    # Check each channel
    lines.append("")
    lines.append("--- Channel Measurements ---")
    for ch in range(1, 5):
        display = conn.safe_query(f":CHANnel{ch}:SWITch?", "OFF")
        if display.upper() == "ON":
            vpp = conn.safe_query(f":MEASure:VPP? C{ch}", "N/A")
            freq = conn.safe_query(f":MEASure:FREQ? C{ch}", "N/A")
            scale = conn.safe_query(f":CHANnel{ch}:SCALe?")
            coupling = conn.safe_query(f":CHANnel{ch}:COUPling?")
            lines.append(f"  C{ch}: {vpp}Vpp, {freq}Hz | {scale}V/div, {coupling}")
        else:
            lines.append(f"  C{ch}: OFF")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Handler dispatch table — maps tool name → handler function
# ═══════════════════════════════════════════════════════════════════════════════

HANDLERS: dict[str, Callable[[dict[str, Any]], str]] = {
    # Connection
    "connect": h_connect,
    "disconnect": h_disconnect,
    "send_command": h_send_command,
    "get_id": h_get_id,
    "reset": h_reset,
    # Run control
    "run": h_run,
    "stop": h_stop,
    # Channel
    "configure_channel": h_configure_channel,
    "get_channel": h_get_channel,
    # Timebase
    "configure_timebase": h_configure_timebase,
    "get_timebase": h_get_timebase,
    # Trigger
    "configure_trigger": h_configure_trigger,
    "get_trigger": h_get_trigger,
    "force_trigger": h_force_trigger,
    # Acquisition
    "configure_acquisition": h_configure_acquisition,
    "get_acquisition": h_get_acquisition,
    # Measurements
    "measure": h_measure,
    "get_measure_stats": h_get_measure_stats,
    "clear_measure_stats": h_clear_measure_stats,
    # Waveform
    "get_waveform_preamble": h_get_waveform_preamble,
    "get_waveform_data": h_get_waveform_data,
    # Screenshot
    "get_screenshot": h_get_screenshot,
    # Display
    "configure_display": h_configure_display,
    # Cursors
    "configure_cursors": h_configure_cursors,
    "get_cursor_values": h_get_cursor_values,
    # Math
    "configure_math": h_configure_math,
    # Digital
    "configure_digital": h_configure_digital,
    # Auto
    "autoset": h_autoset,
    # Save/Recall
    "save_setup": h_save_setup,
    "recall_setup": h_recall_setup,
    # Counter
    "get_frequency_counter": h_get_frequency_counter,
    # Status
    "get_status": h_get_status,
    "get_next_error": h_get_next_error,
    # Snapshot
    "quick_snapshot": h_quick_snapshot,
}


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Server setup
# ═══════════════════════════════════════════════════════════════════════════════

app = Server("oscilloscope-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools."""
    return [
        Tool(name=t["name"], description=t["description"], inputSchema=t["schema"])
        for t in TOOLS
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch tool call to the appropriate handler."""
    handler = HANDLERS.get(name)
    if handler is None:
        return [TextContent(type="text", text=f"ERROR: Unknown tool '{name}'")]

    try:
        result = handler(arguments)
        return [TextContent(type="text", text=str(result))]
    except Exception as e:
        log.error("Tool '%s' failed: %s", name, e, exc_info=True)
        return [TextContent(type="text", text=f"ERROR [{name}]: {e}")]


# ═══════════════════════════════════════════════════════════════════════════════
# Entry Point
# ═══════════════════════════════════════════════════════════════════════════════

async def main() -> None:
    """Run the MCP server over stdio."""
    log.info("Starting Oscilloscope MCP Server (Siglent SDS2000X Plus)")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
