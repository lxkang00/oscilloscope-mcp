#!/usr/bin/env python3
"""
Siglent SDS2000X Plus Oscilloscope MCP Server
=============================================
A Model Context Protocol server for remote control of Siglent SDS2000X Plus
series oscilloscopes via TCP/IP (SCPI over socket on port 5024).

Based on: SDS2000X Plus Programming Guide CN11G
Protocol: IEEE 488.2 + SCPI over raw TCP socket
Default port: 5024

Usage:
    python server.py
    (configure in .mcp.json or Claude Code MCP settings)
"""

import socket
import time
import logging
import os
from typing import Optional, Any

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

# ── Oscilloscope Connection Manager ──────────────────────────────────────────
class OscilloscopeConnection:
    """Manages a persistent TCP socket connection to the oscilloscope."""

    def __init__(self):
        self._sock: Optional[socket.socket] = None
        self._host: Optional[str] = None
        self._port: int = 5024
        self._timeout: float = 10.0

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self, host: str, port: int = 5024, timeout: float = 10.0) -> str:
        """Establish socket connection to oscilloscope."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        self._host = host
        self._port = port
        self._timeout = timeout

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect((host, port))
        self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # Read initial banner if any
        try:
            self._sock.settimeout(0.3)
            banner = self._sock.recv(4096).decode("utf-8", errors="replace").strip()
            self._sock.settimeout(timeout)
        except socket.timeout:
            banner = ""
            self._sock.settimeout(timeout)

        log.info(f"Connected to oscilloscope at {host}:{port}")
        return f"Connected to {host}:{port}" + (f"\nBanner: {banner}" if banner else "")

    def disconnect(self) -> str:
        """Close the socket connection."""
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            log.info("Disconnected from oscilloscope")
            return "Disconnected"
        return "Not connected"

    def send(self, command: str) -> str:
        """Send a SCPI command. Returns empty string for non-query commands."""
        self._ensure_connected()
        full_cmd = command.strip() + "\n"
        log.debug(f"SEND: {command.strip()}")
        self._sock.sendall(full_cmd.encode("utf-8"))
        if "?" in command:
            return self._read_response()
        return ""

    def query(self, command: str) -> str:
        """Send a SCPI query and return the response."""
        self._ensure_connected()
        if not command.strip().endswith("?"):
            command = command.strip() + "?"
        return self.send(command)

    def _read_response(self) -> str:
        """Read response from socket until newline."""
        self._sock.settimeout(self._timeout)
        response = b""
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    break
                response += chunk
                if b"\n" in chunk:
                    break
                # After first chunk, use shorter timeout for remaining
                self._sock.settimeout(0.5)
        except socket.timeout:
            pass
        finally:
            self._sock.settimeout(self._timeout)

        result = response.decode("utf-8", errors="replace").strip()
        log.debug(f"RECV: {result[:200]}")
        return result

    def _ensure_connected(self):
        if not self._sock:
            raise RuntimeError(
                "Not connected to oscilloscope. Use the 'connect' tool first."
            )


# ── Global connection instance ───────────────────────────────────────────────
conn = OscilloscopeConnection()

# ── MCP Server ───────────────────────────────────────────────────────────────
app = Server("oscilloscope-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """Return the list of available tools."""
    return [
        # ── Connection ───────────────────────────────────────────────────────
        Tool(
            name="connect",
            description="Connect to the oscilloscope via TCP/IP (SCPI on port 5024). Must be called before any other tool.",
            inputSchema={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "IP address or hostname of the oscilloscope",
                    },
                    "port": {
                        "type": "integer",
                        "description": "TCP port number (default: 5024)",
                        "default": 5024,
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Connection timeout in seconds (default: 10)",
                        "default": 10.0,
                    },
                },
                "required": ["host"],
            },
        ),
        Tool(
            name="disconnect",
            description="Disconnect from the oscilloscope.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="send_command",
            description="Send a raw SCPI command to the oscilloscope and return the response. Use for any command not covered by dedicated tools.",
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "SCPI command string (e.g. '*IDN?', ':CHANnel1:SCALe 1.0', ':MEASure:VPP? CHANnel1'). Queries (ending with ?) return a response.",
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="get_id",
            description="Get the oscilloscope's identification string (manufacturer, model, serial, firmware version).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="reset",
            description="Reset the oscilloscope to factory default settings (*RST).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Channel Configuration ───────────────────────────────────────────
        Tool(
            name="configure_channel",
            description="Configure an analog channel (display on/off, vertical scale, offset, coupling, bandwidth limit, probe attenuation, invert, label, unit, deskew).",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "Channel number (1-4)",
                        "minimum": 1,
                        "maximum": 4,
                    },
                    "display": {
                        "type": "boolean",
                        "description": "Turn channel display on (true) or off (false)",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Vertical scale in Volts/div (e.g. 0.5 for 500mV/div)",
                    },
                    "offset": {
                        "type": "number",
                        "description": "Vertical offset in Volts",
                    },
                    "coupling": {
                        "type": "string",
                        "description": "Input coupling: D1M=1MΩ DC, A1M=1MΩ AC, D50=50Ω DC, GND=Ground",
                        "enum": ["D1M", "A1M", "D50", "GND"],
                    },
                    "bandwidth_limit": {
                        "type": "string",
                        "description": "Bandwidth limit",
                        "enum": ["OFF", "20M", "200M", "350M"],
                    },
                    "probe": {
                        "type": "number",
                        "description": "Probe attenuation factor (e.g. 1, 10, 100)",
                    },
                    "invert": {
                        "type": "boolean",
                        "description": "Invert the waveform",
                    },
                    "label": {
                        "type": "string",
                        "description": "Custom label text for the channel (max 20 chars)",
                    },
                    "unit": {
                        "type": "string",
                        "description": "Display unit (V for voltage, A for current)",
                        "enum": ["V", "A"],
                    },
                    "skew": {
                        "type": "number",
                        "description": "Deskew value in seconds (e.g. 1e-9 for 1ns)",
                    },
                },
                "required": ["channel"],
            },
        ),
        Tool(
            name="get_channel",
            description="Get the current settings of an analog channel (display, scale, offset, coupling, BW limit, probe, invert, unit).",
            inputSchema={
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "integer",
                        "description": "Channel number (1-4)",
                        "minimum": 1,
                        "maximum": 4,
                    },
                },
                "required": ["channel"],
            },
        ),
        # ── Timebase ─────────────────────────────────────────────────────────
        Tool(
            name="configure_timebase",
            description="Configure the horizontal timebase: scale (s/div), delay/position, and mode (MAIN, WINDOW, ROLL, XY).",
            inputSchema={
                "type": "object",
                "properties": {
                    "scale": {
                        "type": "number",
                        "description": "Horizontal scale in seconds/div (e.g. 1e-3 for 1ms/div)",
                    },
                    "delay": {
                        "type": "number",
                        "description": "Horizontal delay/position in seconds",
                    },
                    "mode": {
                        "type": "string",
                        "description": "Timebase mode",
                        "enum": ["MAIN", "WINDOW", "ROLL", "XY"],
                    },
                },
            },
        ),
        Tool(
            name="get_timebase",
            description="Get the current timebase settings (scale, delay, mode).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Trigger ──────────────────────────────────────────────────────────
        Tool(
            name="configure_trigger",
            description="Configure the trigger system: sweep mode, trigger type, source, level, slope, coupling, and holdoff.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Trigger sweep mode",
                        "enum": ["AUTO", "NORMal", "SINGle", "STOP"],
                    },
                    "trigger_type": {
                        "type": "string",
                        "description": "Trigger type",
                        "enum": ["EDGE", "GLIT", "INTV", "DROP", "RUNT", "WIND", "PATT", "Qualified", "NthEdge", "SETUPHOLD", "VIDeo", "SERial"],
                    },
                    "source": {
                        "type": "string",
                        "description": "Trigger source (e.g. C1-C4, EXT, EXT/5, LINE, D0-D15)",
                        "enum": ["C1", "C2", "C3", "C4", "EXT", "EXT/5", "LINE", "D0", "D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8", "D9", "D10", "D11", "D12", "D13", "D14", "D15"],
                    },
                    "level": {
                        "type": "number",
                        "description": "Trigger level in Volts",
                    },
                    "slope": {
                        "type": "string",
                        "description": "Trigger slope",
                        "enum": ["POSitive", "NEGative", "EITHer"],
                    },
                    "coupling": {
                        "type": "string",
                        "description": "Trigger coupling",
                        "enum": ["DC", "AC", "HFR", "LFR"],
                    },
                    "holdoff": {
                        "type": "number",
                        "description": "Trigger holdoff time in seconds",
                    },
                },
            },
        ),
        Tool(
            name="get_trigger",
            description="Get the current trigger settings (type, sweep, source, level, slope).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="force_trigger",
            description="Force a trigger event immediately.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Acquisition ──────────────────────────────────────────────────────
        Tool(
            name="configure_acquisition",
            description="Configure the acquisition system: mode (normal/peak/average/eres), memory depth, interpolation, and averaging count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Acquisition mode",
                        "enum": ["NORMal", "PEAK", "AVERage", "ERES"],
                    },
                    "memory_depth": {
                        "type": "string",
                        "description": "Maximum memory depth",
                        "enum": ["10K", "100K", "1M", "10M", "25M", "50M", "100M", "200M"],
                    },
                    "interpolation": {
                        "type": "string",
                        "description": "Interpolation method",
                        "enum": ["LINEAR", "SINX"],
                    },
                    "average_count": {
                        "type": "integer",
                        "description": "Number of averages for AVERage mode (4-1024)",
                    },
                },
            },
        ),
        Tool(
            name="get_acquisition",
            description="Get the current acquisition settings (mode, memory depth, sample rate, interpolation).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Measurements ─────────────────────────────────────────────────────
        Tool(
            name="measure",
            description="Perform automatic measurements on a waveform. Supports 50+ measurement types including VPP, FREQ, RMS, rise/fall time, duty cycle, etc. Returns the measured value.",
            inputSchema={
                "type": "object",
                "properties": {
                    "measurement": {
                        "type": "string",
                        "description": "Measurement type (e.g. VPP, FREQ, VMAX, VMIN, VRMS, PER, RTIM, FTIM, PWIDth, DUTY, RISE, FALL, DELay, PHASe, ALL, and more)",
                        "enum": [
                            "VPP", "VMAX", "VMIN", "VAMP", "VTOP", "VBASe", "VAVG",
                            "VRMS", "OVSN", "FPREshoot", "PER", "FREQ", "RTIM",
                            "FTIM", "PWIDth", "NWIDth", "DUTY", "NDUTY", "PDUTY",
                            "WIDth", "RISE", "FALL", "DELay", "PHASe",
                            "ALL", "AMP", "BASE", "CMEAN", "CRMS",
                            "CYCLE_MEAN", "CYCLE_RMS", "CYCLE_STDDEV",
                            "DELTA_DELAY", "DELTA_TCCJITTER", "DELTA_TCNJITTER",
                            "DELTA_TIEJITTER", "DELTA_TPERIODJITTER",
                            "DELTA_TSKEW", "DELTA_TTRIGLEVEL",
                            "FOV", "FPOV", "HPO", "IREC", "ISEC",
                            "LOW", "MAX", "MEAN", "MIN", "NCYC",
                            "NOV", "NPO", "NSEC", "OVR", "PHA",
                            "PKPK", "PPUL", "PREC", "PWID",
                            "RPRE", "STDDEV",
                        ],
                    },
                    "source1": {
                        "type": "string",
                        "description": "Primary measurement source (e.g. C1, C2, C3, C4, MATH, F1)",
                        "default": "C1",
                    },
                    "source2": {
                        "type": "string",
                        "description": "Secondary measurement source (for delay/phase measurements)",
                    },
                },
                "required": ["measurement"],
            },
        ),
        Tool(
            name="get_measure_stats",
            description="Get measurement statistics (mean, min, max, stddev, count) for currently enabled measurements.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="clear_measure_stats",
            description="Clear/reset measurement statistics.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Waveform Data ────────────────────────────────────────────────────
        Tool(
            name="get_waveform_preamble",
            description="Get waveform preamble metadata (format, points, X/Y increments and origins) for a source. Useful before reading waveform data to understand scaling.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Waveform source (e.g. C1, C2, C3, C4, MATH, F1, D0-D15)",
                        "default": "C1",
                    },
                },
                "required": ["source"],
            },
        ),
        Tool(
            name="get_waveform_data",
            description="Get waveform data as voltage values from the specified source. Supports decimation via max_points for large datasets.",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Waveform source (e.g. C1, C2, C3, C4, MATH, F1)",
                        "default": "C1",
                    },
                    "max_points": {
                        "type": "integer",
                        "description": "Maximum number of data points to return. If not specified, returns all available points (may be slow for large memory depths).",
                    },
                },
                "required": ["source"],
            },
        ),
        # ── Display ──────────────────────────────────────────────────────────
        Tool(
            name="configure_display",
            description="Configure display settings: grid mode, persistence, intensity, grid style, and axis mode.",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid": {
                        "type": "string",
                        "description": "Grid display mode",
                        "enum": ["FULL", "HALF", "OFF"],
                    },
                    "persistence": {
                        "type": "string",
                        "description": "Persistence mode (OFF or time value like 0.5, 1, 5, 10, INF for infinite)",
                    },
                    "intensity": {
                        "type": "integer",
                        "description": "Grid brightness (0-100)",
                        "minimum": 0,
                        "maximum": 100,
                    },
                    "grid_style": {
                        "type": "string",
                        "description": "Grid line style",
                        "enum": ["LINE", "DOT"],
                    },
                    "axis_mode": {
                        "type": "string",
                        "description": "Axes display mode",
                        "enum": ["FULL", "MINImum"],
                    },
                },
            },
        ),
        # ── Cursors ──────────────────────────────────────────────────────────
        Tool(
            name="configure_cursors",
            description="Configure cursor measurements: mode (off/manual/track/measure), sources, and cursor type.",
            inputSchema={
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "Cursor mode",
                        "enum": ["OFF", "MANual", "TRACk", "MEASure"],
                    },
                    "source1": {
                        "type": "string",
                        "description": "First cursor source (e.g. C1, C2, C3, C4, MATH)",
                    },
                    "source2": {
                        "type": "string",
                        "description": "Second cursor source",
                    },
                    "cursor_type": {
                        "type": "string",
                        "description": "Cursor type for manual mode",
                        "enum": ["X", "Y", "XY"],
                    },
                },
            },
        ),
        Tool(
            name="get_cursor_values",
            description="Get current cursor measurement values (delta X, delta Y).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Math ─────────────────────────────────────────────────────────────
        Tool(
            name="configure_math",
            description="Configure the math trace: function (ADD/SUB/MULT/DIV/FFT/DIFF/INTG/SQRT/AVG/ERES/ABSS), sources, scale, offset, and FFT-specific settings.",
            inputSchema={
                "type": "object",
                "properties": {
                    "display": {
                        "type": "boolean",
                        "description": "Turn math trace on/off",
                    },
                    "function": {
                        "type": "string",
                        "description": "Math function",
                        "enum": ["ADD", "SUB", "MULT", "DIV", "FFT", "DIFF", "INTG", "SQRT", "AVG", "ERES", "ABSS"],
                    },
                    "source1": {
                        "type": "string",
                        "description": "First operand source (e.g. C1, C2, C3, C4)",
                    },
                    "source2": {
                        "type": "string",
                        "description": "Second operand source (for ADD/SUB/MULT/DIV)",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Vertical scale of the math trace",
                    },
                    "offset": {
                        "type": "number",
                        "description": "Vertical offset of the math trace",
                    },
                    "fft_window": {
                        "type": "string",
                        "description": "FFT window type (only for FFT function)",
                        "enum": ["RECT", "BLAC", "HANN", "HAMM", "FLAT", "KAIS"],
                    },
                    "fft_scale": {
                        "type": "string",
                        "description": "FFT vertical scale unit",
                        "enum": ["VRMS", "DBVRMS", "DBM"],
                    },
                },
            },
        ),
        # ── Auto Setup ───────────────────────────────────────────────────────
        Tool(
            name="autoset",
            description="Perform auto-setup to automatically configure the oscilloscope for the connected input signals.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Save / Recall ────────────────────────────────────────────────────
        Tool(
            name="save_setup",
            description="Save the current oscilloscope setup to internal memory or USB drive.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Storage location: INTERNAL (scope memory) or USB (external drive)",
                        "enum": ["INTERNAL", "USB"],
                        "default": "INTERNAL",
                    },
                    "filename": {
                        "type": "string",
                        "description": "File name for saving setup (without extension, saved as .xml)",
                    },
                },
            },
        ),
        Tool(
            name="recall_setup",
            description="Recall a previously saved oscilloscope setup from internal memory or USB.",
            inputSchema={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Storage location: INTERNAL or USB",
                        "enum": ["INTERNAL", "USB"],
                        "default": "INTERNAL",
                    },
                    "filename": {
                        "type": "string",
                        "description": "File name of the saved setup (without extension)",
                    },
                },
            },
        ),
        # ── Frequency Counter ────────────────────────────────────────────────
        Tool(
            name="get_frequency_counter",
            description="Get the built-in hardware frequency counter reading (mode, source, and frequency).",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        # ── Status / Error ───────────────────────────────────────────────────
        Tool(
            name="get_status",
            description="Get the oscilloscope's operational status: acquisition state, trigger state, operation complete, and event status register.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="get_next_error",
            description="Get and clear the next error from the oscilloscope's error queue.",
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Handle tool invocations."""
    try:
        result = await _handle_tool(name, arguments)
        return [TextContent(type="text", text=str(result))]
    except Exception as e:
        log.error(f"Tool '{name}' failed: {e}")
        return [TextContent(type="text", text=f"ERROR: {e}")]


async def _handle_tool(name: str, args: dict[str, Any]) -> str:
    """Dispatch to the appropriate handler."""

    # ── Connection ──────────────────────────────────────────────────────────
    if name == "connect":
        host = args["host"]
        port = args.get("port", 5024)
        timeout = args.get("timeout", 10.0)
        return conn.connect(host, port, timeout)

    elif name == "disconnect":
        return conn.disconnect()

    elif name == "send_command":
        return conn.send(args["command"])

    elif name == "get_id":
        return conn.query("*IDN?")

    elif name == "reset":
        conn.send("*RST")
        time.sleep(3)
        return "Oscilloscope reset complete (*RST)"

    # ── Channel ─────────────────────────────────────────────────────────────
    elif name == "configure_channel":
        ch = args["channel"]
        results = []
        if "display" in args:
            val = "ON" if args["display"] else "OFF"
            conn.send(f":CHANnel{ch}:SWITch {val}")
            results.append(f"Display: {val}")
        if "scale" in args:
            conn.send(f":CHANnel{ch}:SCALe {args['scale']}")
            results.append(f"Scale: {args['scale']} V/div")
        if "offset" in args:
            conn.send(f":CHANnel{ch}:OFFSet {args['offset']}")
            results.append(f"Offset: {args['offset']} V")
        if "coupling" in args:
            conn.send(f":CHANnel{ch}:COUPling {args['coupling']}")
            results.append(f"Coupling: {args['coupling']}")
        if "bandwidth_limit" in args:
            conn.send(f":CHANnel{ch}:BWLimit {args['bandwidth_limit']}")
            results.append(f"BW Limit: {args['bandwidth_limit']}")
        if "probe" in args:
            conn.send(f":CHANnel{ch}:PROBe {args['probe']}")
            results.append(f"Probe: {args['probe']}x")
        if "invert" in args:
            val = "ON" if args["invert"] else "OFF"
            conn.send(f":CHANnel{ch}:INVert {val}")
            results.append(f"Invert: {val}")
        if "label" in args:
            conn.send(f':CHANnel{ch}:LABel:TEXT "{args["label"]}"')
            conn.send(f":CHANnel{ch}:LABel ON")
            results.append(f"Label: {args['label']}")
        if "unit" in args:
            conn.send(f":CHANnel{ch}:UNIT {args['unit']}")
            results.append(f"Unit: {args['unit']}")
        if "skew" in args:
            conn.send(f":CHANnel{ch}:SKEW {args['skew']}")
            results.append(f"Skew: {args['skew']}s")
        return f"Channel {ch} configured:\n" + "\n".join(f"  - {r}" for r in results)

    elif name == "get_channel":
        ch = args["channel"]
        props = {
            "Display": conn.query(f":CHANnel{ch}:SWITch?"),
            "Scale (V/div)": conn.query(f":CHANnel{ch}:SCALe?"),
            "Offset (V)": conn.query(f":CHANnel{ch}:OFFSet?"),
            "Coupling": conn.query(f":CHANnel{ch}:COUPling?"),
            "BW Limit": conn.query(f":CHANnel{ch}:BWLimit?"),
            "Probe": conn.query(f":CHANnel{ch}:PROBe?"),
            "Invert": conn.query(f":CHANnel{ch}:INVert?"),
            "Unit": conn.query(f":CHANnel{ch}:UNIT?"),
        }
        return f"Channel {ch}:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())

    # ── Timebase ────────────────────────────────────────────────────────────
    elif name == "configure_timebase":
        results = []
        if "scale" in args:
            conn.send(f":TIMebase:SCALe {args['scale']}")
            results.append(f"Scale: {args['scale']} s/div")
        if "delay" in args:
            conn.send(f":TIMebase:DELay {args['delay']}")
            results.append(f"Delay: {args['delay']} s")
        if "mode" in args:
            conn.send(f":TIMebase:MODE {args['mode']}")
            results.append(f"Mode: {args['mode']}")
        return "Timebase configured:\n" + "\n".join(f"  - {r}" for r in results)

    elif name == "get_timebase":
        props = {
            "Scale (s/div)": conn.query(":TIMebase:SCALe?"),
            "Delay (s)": conn.query(":TIMebase:DELay?"),
            "Mode": conn.query(":TIMebase:MODE?"),
        }
        return "Timebase:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())

    # ── Trigger ─────────────────────────────────────────────────────────────
    elif name == "configure_trigger":
        results = []
        if "mode" in args:
            conn.send(f":TRIGger:SWEep {args['mode']}")
            results.append(f"Sweep: {args['mode']}")
        if "trigger_type" in args:
            conn.send(f":TRIGger:TYPE {args['trigger_type']}")
            results.append(f"Type: {args['trigger_type']}")
        if "source" in args:
            conn.send(f":TRIGger:EDGE:SOURce {args['source']}")
            results.append(f"Source: {args['source']}")
        if "level" in args:
            conn.send(f":TRIGger:EDGE:LEVel {args['level']}")
            results.append(f"Level: {args['level']} V")
        if "slope" in args:
            conn.send(f":TRIGger:EDGE:SLOPe {args['slope']}")
            results.append(f"Slope: {args['slope']}")
        if "coupling" in args:
            conn.send(f":TRIGger:EDGE:COUPling {args['coupling']}")
            results.append(f"Coupling: {args['coupling']}")
        if "holdoff" in args:
            conn.send(f":TRIGger:HOLDoff {args['holdoff']}")
            results.append(f"Holdoff: {args['holdoff']} s")
        return "Trigger configured:\n" + "\n".join(f"  - {r}" for r in results)

    elif name == "get_trigger":
        try:
            trig_type = conn.query(":TRIGger:TYPE?").strip()
        except Exception:
            trig_type = "EDGE"
        props = {
            "Type": trig_type,
            "Sweep": conn.query(":TRIGger:SWEep?").strip(),
            "Source": conn.query(f":TRIGger:{trig_type}:SOURce?").strip(),
            "Level": conn.query(f":TRIGger:{trig_type}:LEVel?").strip(),
            "Slope": conn.query(f":TRIGger:{trig_type}:SLOPe?").strip(),
        }
        return "Trigger:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())

    elif name == "force_trigger":
        conn.send("*TRG")
        return "Trigger forced"

    # ── Acquisition ─────────────────────────────────────────────────────────
    elif name == "configure_acquisition":
        results = []
        if "mode" in args:
            conn.send(f":ACQuire:MODE {args['mode']}")
            results.append(f"Mode: {args['mode']}")
        if "memory_depth" in args:
            conn.send(f":ACQuire:MDEPth {args['memory_depth']}")
            results.append(f"Memory Depth: {args['memory_depth']}")
        if "interpolation" in args:
            conn.send(f":ACQuire:INTerpolation {args['interpolation']}")
            results.append(f"Interpolation: {args['interpolation']}")
        if "average_count" in args:
            conn.send(f":ACQuire:AVERage {args['average_count']}")
            results.append(f"Averages: {args['average_count']}")
        return "Acquisition configured:\n" + "\n".join(f"  - {r}" for r in results)

    elif name == "get_acquisition":
        props = {
            "Mode": conn.query(":ACQuire:MODE?").strip(),
            "Memory Depth": conn.query(":ACQuire:MDEPth?").strip(),
            "Sample Rate": conn.query(":ACQuire:SRATe?").strip(),
            "Interpolation": conn.query(":ACQuire:INTerpolation?").strip(),
        }
        return "Acquisition:\n" + "\n".join(f"  {k}: {v}" for k, v in props.items())

    # ── Measurements ────────────────────────────────────────────────────────
    elif name == "measure":
        meas = args["measurement"]
        src1 = args.get("source1", "C1")
        src2 = args.get("source2")

        if meas == "ALL":
            result = conn.query(f":MEASure:ALL? {src1}")
            return f"All measurements for {src1}:\n{result}"

        if src2:
            cmd = f":MEASure:{meas}? {src1},{src2}"
        else:
            cmd = f":MEASure:{meas}? {src1}"

        value = conn.query(cmd)
        return f"{meas}({src1}{', ' + src2 if src2 else ''}) = {value}"

    elif name == "get_measure_stats":
        stats = {
            "Count": conn.query(":MEASure:STATistics:COUNt?").strip(),
            "Mean": conn.query(":MEASure:STATistics:MEAN?").strip(),
            "Min": conn.query(":MEASure:STATistics:MIN?").strip(),
            "Max": conn.query(":MEASure:STATistics:MAX?").strip(),
            "StdDev": conn.query(":MEASure:STATistics:STDD?").strip(),
        }
        return "Measurement Statistics:\n" + "\n".join(
            f"  {k}: {v}" for k, v in stats.items()
        )

    elif name == "clear_measure_stats":
        conn.send(":MEASure:STATistics:RESet")
        return "Measurement statistics cleared"

    # ── Waveform ────────────────────────────────────────────────────────────
    elif name == "get_waveform_preamble":
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

    elif name == "get_waveform_data":
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
            total_points = int(parts[2])
        else:
            yinc, yorigin, yref, total_points = 1.0, 0.0, 0.0, 0

        points_to_read = total_points
        if max_points and max_points < total_points:
            step = total_points // max_points
            conn.send(":WAVeform:STARt 1")
            conn.send(f":WAVeform:SPOints {max_points}")
            conn.send(f":WAVeform:INTerval {step}")
            points_to_read = max_points

        raw_data = conn.query(":WAVeform:DATA?")
        values = raw_data.split(",")

        voltages = []
        for val in values:
            try:
                v = float(val.strip())
                voltage = yorigin + yinc * (v - yref)
                voltages.append(voltage)
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
                f"  Mean: {sum(voltages)/len(voltages):.4f} V\n"
                f"  First 10: {[round(v, 4) for v in voltages[:10]]}\n"
                f"  Last 10: {[round(v, 4) for v in voltages[-10:]]}"
            )

        return summary

    # ── Display ─────────────────────────────────────────────────────────────
    elif name == "configure_display":
        results = []
        if "grid" in args:
            conn.send(f":DISPlay:GRIDstyle {args['grid']}")
            results.append(f"Grid: {args['grid']}")
        if "persistence" in args:
            conn.send(f":DISPlay:PERSistence {args['persistence']}")
            results.append(f"Persistence: {args['persistence']}")
        if "intensity" in args:
            conn.send(f":DISPlay:INTensity {args['intensity']}")
            results.append(f"Intensity: {args['intensity']}")
        if "grid_style" in args:
            conn.send(f":DISPlay:GRATicule {args['grid_style']}")
            results.append(f"Grid Style: {args['grid_style']}")
        if "axis_mode" in args:
            conn.send(f":DISPlay:AXIS:MODE {args['axis_mode']}")
            results.append(f"Axis Mode: {args['axis_mode']}")
        return "Display configured:\n" + "\n".join(f"  - {r}" for r in results)

    # ── Cursors ─────────────────────────────────────────────────────────────
    elif name == "configure_cursors":
        results = []
        if "mode" in args:
            conn.send(f":CURSor:MODE {args['mode']}")
            results.append(f"Mode: {args['mode']}")
        if "source1" in args:
            conn.send(f":CURSor:SOURce1 {args['source1']}")
            results.append(f"Source1: {args['source1']}")
        if "source2" in args:
            conn.send(f":CURSor:SOURce2 {args['source2']}")
            results.append(f"Source2: {args['source2']}")
        if "cursor_type" in args:
            conn.send(f":CURSor:TYPE {args['cursor_type']}")
            results.append(f"Type: {args['cursor_type']}")
        return "Cursors configured:\n" + "\n".join(f"  - {r}" for r in results)

    elif name == "get_cursor_values":
        mode = conn.query(":CURSor:MODE?").strip()
        if mode == "OFF":
            return "Cursors are OFF"

        values = {}
        try:
            values["Delta X"] = conn.query(":CURSor:XDELta?").strip()
        except Exception:
            pass
        try:
            values["Delta Y"] = conn.query(":CURSor:YDELta?").strip()
        except Exception:
            pass
        return f"Cursor Values (Mode: {mode}):\n" + "\n".join(
            f"  {k}: {v}" for k, v in values.items()
        )

    # ── Math ────────────────────────────────────────────────────────────────
    elif name == "configure_math":
        results = []
        if "display" in args:
            val = "ON" if args["display"] else "OFF"
            conn.send(f":MATH:SWITch {val}")
            results.append(f"Display: {val}")
        if "function" in args:
            conn.send(f":MATH:FUNCtion {args['function']}")
            results.append(f"Function: {args['function']}")
        if "source1" in args:
            conn.send(f":MATH:SOURce1 {args['source1']}")
            results.append(f"Source1: {args['source1']}")
        if "source2" in args:
            conn.send(f":MATH:SOURce2 {args['source2']}")
            results.append(f"Source2: {args['source2']}")
        if "scale" in args:
            conn.send(f":MATH:SCALe {args['scale']}")
            results.append(f"Scale: {args['scale']}")
        if "offset" in args:
            conn.send(f":MATH:OFFSet {args['offset']}")
            results.append(f"Offset: {args['offset']}")
        if "fft_window" in args:
            conn.send(f":MATH:FFT:WINDow {args['fft_window']}")
            results.append(f"FFT Window: {args['fft_window']}")
        if "fft_scale" in args:
            conn.send(f":MATH:FFT:SCALe {args['fft_scale']}")
            results.append(f"FFT Scale: {args['fft_scale']}")
        return "Math configured:\n" + "\n".join(f"  - {r}" for r in results)

    # ── Auto Setup ──────────────────────────────────────────────────────────
    elif name == "autoset":
        conn.send(":AUToset")
        time.sleep(2)
        return "Auto-setup complete"

    # ── Save / Recall ───────────────────────────────────────────────────────
    elif name == "save_setup":
        loc = args.get("location", "INTERNAL")
        fname = args.get("filename", "SETUP")
        if loc == "USB":
            path = f"/usb0/{fname}.xml"
        else:
            path = f"/SDS2000X+/internal/{fname}.xml"
        conn.send(f':STORe:SETup "{path}"')
        return f"Setup saved to {loc} as {fname}.xml"

    elif name == "recall_setup":
        loc = args.get("location", "INTERNAL")
        fname = args.get("filename", "SETUP")
        if loc == "USB":
            path = f"/usb0/{fname}.xml"
        else:
            path = f"/SDS2000X+/internal/{fname}.xml"
        conn.send(f':RECAll:SETup "{path}"')
        return f"Setup recalled from {loc}: {fname}.xml"

    # ── Frequency Counter ───────────────────────────────────────────────────
    elif name == "get_frequency_counter":
        freq = conn.query(":COUNter:CURRent?").strip()
        mode = conn.query(":COUNter:MODE?").strip()
        source = conn.query(":COUNter:SOURce?").strip()
        return (
            f"Frequency Counter:\n"
            f"  Mode: {mode}\n"
            f"  Source: {source}\n"
            f"  Frequency: {freq} Hz"
        )

    # ── Status ──────────────────────────────────────────────────────────────
    elif name == "get_status":
        status = {}
        try:
            status["Acquisition State"] = conn.query(":ACQuire:STATE?").strip()
        except Exception:
            pass
        try:
            status["Trigger State"] = conn.query(":TRIGger:STATus?").strip()
        except Exception:
            pass
        try:
            status["Operation Complete"] = conn.query("*OPC?").strip()
        except Exception:
            pass
        try:
            status["Event Status Register"] = conn.query("*ESR?").strip()
        except Exception:
            pass
        return "Oscilloscope Status:\n" + "\n".join(
            f"  {k}: {v}" for k, v in status.items()
        )

    elif name == "get_next_error":
        try:
            error = conn.query(":SYSTem:ERRor?").strip()
        except Exception:
            error = "Could not read error queue"
        return f"Error: {error}"

    else:
        return f"Unknown tool: {name}"


# ── Entry Point ──────────────────────────────────────────────────────────────
async def main():
    """Run the MCP server via stdio."""
    log.info("Starting Oscilloscope MCP Server (Siglent SDS2000X Plus)")
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
