"""
FoxESS Tray Monitor
A Windows system tray + popup window app showing live solar, grid, and home
consumption data from the FoxESS Cloud OpenAPI.

Author: Built for Roge
"""

import hashlib
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def resource_path(rel: str) -> Path:
    """
    Return the absolute path to a bundled resource.
    When packaged with PyInstaller, files are unpacked to sys._MEIPASS at runtime.
    When running from source, they live next to this script.
    """
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).parent
    return Path(base) / rel

import requests
from PIL import Image, ImageDraw, ImageFont
import pystray
from pystray import MenuItem as item, Menu

import tkinter as tk
from tkinter import ttk, messagebox

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

APP_NAME = "FoxESS Tray Monitor"
CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "FoxessTray"
CONFIG_FILE = CONFIG_DIR / "config.json"
API_DOMAIN = "https://www.foxesscloud.com"
REFRESH_SECONDS = 5
REQUEST_TIMEOUT = 15

# Variables we ask the API for. Empty list = all variables; we pass an explicit
# list to keep the response small and predictable.
WANTED_VARIABLES = [
    "pvPower",              # Total solar power (kW)
    "feedinPower",          # Exporting to grid (kW)
    "gridConsumptionPower", # Importing from grid (kW)
    "loadsPower",           # Total home consumption (kW)
    "SoC",                  # Battery state of charge (%)
    "batChargePower",       # Battery charging (kW)
    "batDischargePower",    # Battery discharging (kW)
    "generationPower",      # System AC output (kW)
    "todayYield",           # Today's generation (kWh) — primary
    "pvEnergyToday",        # Alternative name used on some models
    "ambientTemperation",   # Ambient temperature (°C)
    "invTemperation",       # Inverter temperature (°C)
]


# ----------------------------------------------------------------------------
# Device static info (model, firmware, alarms) — fetched separately
# ----------------------------------------------------------------------------

class DeviceInfo:
    def __init__(self):
        self.model: str = "—"
        self.firmware: str = "—"
        self.alarms: list = []


# ----------------------------------------------------------------------------
# Config persistence
# ----------------------------------------------------------------------------

def load_config() -> dict:
    """Load config from disk, or return an empty template."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"api_key": "", "device_sn": ""}


def save_config(cfg: dict) -> None:
    """Persist config to disk under %APPDATA%\\FoxessTray\\config.json."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ----------------------------------------------------------------------------
# FoxESS API client
# ----------------------------------------------------------------------------

class FoxessClient:
    """Thin wrapper over the FoxESS OpenAPI real-time data endpoint."""

    def __init__(self, api_key: str, device_sn: str):
        self.api_key = api_key
        self.device_sn = device_sn

    def _signed_headers(self, path: str) -> dict:
        """
        Build the request headers required by FoxESS:
          - token: API key
          - timestamp: ms since epoch
          - signature: md5(path \r\n token \r\n timestamp)
          - lang: en
          - User-Agent: required (docs say to set a custom UA for scripts)

        IMPORTANT: The FoxESS docs use a Python *raw* string for the signed
        text: `fr'{path}\r\n{token}\r\n{timestamp}'`. In a raw string, `\r\n`
        stays as the four LITERAL characters: backslash, r, backslash, n.
        It is NOT the CR+LF byte pair. Getting this wrong returns error
        40256 ("illegal signature").
        """
        timestamp = str(round(time.time() * 1000))
        # Use \\r\\n in a normal Python string so the actual content is the
        # four literal characters \r\n (not a real carriage return + newline).
        sig_input = f"{path}\\r\\n{self.api_key}\\r\\n{timestamp}"
        signature = hashlib.md5(sig_input.encode("utf-8")).hexdigest()
        return {
            "token": self.api_key,
            "timestamp": timestamp,
            "signature": signature,
            "lang": "en",
            "Content-Type": "application/json",
            "User-Agent": "FoxessTrayMonitor/1.0",
        }

    def get_realtime(self) -> dict:
        """
        Query real-time data. Returns a dict like:
          {"pvPower": 0.991, "loadsPower": 1.53, "gridConsumptionPower": 0.541, ...}

        Raises RuntimeError on API or network failure with a human-readable message.
        """
        path = "/op/v0/device/real/query"
        url = API_DOMAIN + path
        payload = {"sn": self.device_sn, "variables": WANTED_VARIABLES}

        try:
            resp = requests.post(
                url,
                headers=self._signed_headers(path),
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Network error: {e}") from e

        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")

        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"Bad JSON in response: {resp.text[:200]}")

        # FoxESS uses errno=0 for success and a non-zero code with a message otherwise
        if body.get("errno", -1) != 0:
            raise RuntimeError(
                f"API error {body.get('errno')}: {body.get('msg', 'unknown')}"
            )

        # result is a list of devices; each has a 'datas' array of {variable, value, unit}
        result = body.get("result") or []
        if not result:
            raise RuntimeError("API returned no result for this device SN")

        device_data = result[0].get("datas", [])
        flat = {}
        for entry in device_data:
            var = entry.get("variable")
            val = entry.get("value")
            if var is not None:
                flat[var] = val
        # Include the inverter's own timestamp so the UI shows the real data age
        flat["_inverter_time"] = result[0].get("time")
        return flat


    def get_device_detail(self) -> dict:
        """Fetch model and firmware info. Returns the result dict."""
        path = "/op/v0/device/detail"
        url = API_DOMAIN + path
        try:
            resp = requests.get(
                url,
                headers=self._signed_headers(path),
                params={"sn": self.device_sn},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Network error: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"Bad JSON: {resp.text[:200]}")
        if body.get("errno", -1) != 0:
            raise RuntimeError(f"API error {body.get('errno')}: {body.get('msg', 'unknown')}")
        return body.get("result") or {}

    def get_alarms(self) -> list:
        """Fetch alarms raised in the past 24 hours."""
        path = "/op/v0/device/alarm/query"
        url = API_DOMAIN + path
        now_ms = int(time.time() * 1000)
        payload = {
            "sn": self.device_sn,
            "date": {"begin": now_ms - 86_400_000, "end": now_ms},
            "pageIndex": 1,
        }
        try:
            resp = requests.post(
                url,
                headers=self._signed_headers(path),
                json=payload,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"Network error: {e}") from e
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(f"Bad JSON: {resp.text[:200]}")
        if body.get("errno", -1) != 0:
            raise RuntimeError(f"API error {body.get('errno')}: {body.get('msg', 'unknown')}")
        return (body.get("result") or {}).get("alarms", [])

    def get_energy_report(self, year: int, month: int, day: int, dimension: str = "day") -> list:
        """Fetch hourly (dimension='day') or daily (dimension='month') energy data."""
        path = "/op/v0/device/report/query"
        url = API_DOMAIN + path
        # Start with the four variables every inverter supports; try to add
        # battery variables only when the device has a battery.
        base_vars = ["generation", "feedin", "gridConsumption", "loads"]
        battery_vars = ["chargeEnergy", "dischargeEnergy"]
        payload = {
            "sn": self.device_sn,
            "dimension": dimension,
            "variables": base_vars + battery_vars,
            "queryDate": {"year": year, "month": month, "day": day},
        }
        for attempt in range(2):
            try:
                resp = requests.post(
                    url,
                    headers=self._signed_headers(path),
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as e:
                raise RuntimeError(f"Network error: {e}") from e
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            try:
                body = resp.json()
            except ValueError:
                raise RuntimeError(f"Bad JSON: {resp.text[:200]}")
            errno = body.get("errno", -1)
            if errno == 0:
                return body.get("result") or []
            # 40257 = parameter validation — battery vars may be unsupported;
            # retry once with only the base variables.
            if attempt == 0 and errno == 40257:
                payload = {**payload, "variables": base_vars}
                continue
            raise RuntimeError(f"API error {errno}: {body.get('msg', 'unknown')}")
        return []


# ----------------------------------------------------------------------------
# State derived from the raw API response
# ----------------------------------------------------------------------------

class DataState:
    """Holds the latest reading + a tiny bit of derived state for the UI."""

    def __init__(self):
        self.solar_w = 0.0
        self.home_w = 0.0
        self.grid_import_w = 0.0   # positive = importing FROM grid
        self.grid_export_w = 0.0   # positive = exporting TO grid
        self.battery_soc = None    # % or None if no battery
        self.battery_charge_w = 0.0
        self.battery_discharge_w = 0.0
        self.today_yield_kwh = None  # None = not returned by API
        self.inverter_temp = None    # °C or None
        self.ambient_temp = None     # °C or None
        self.last_update = None    # datetime or None
        self.last_error = None     # str or None

    @classmethod
    def from_api(cls, data: dict) -> "DataState":
        """Convert the kW dict from the API into watts for display."""
        s = cls()
        # The API returns powers in kW; convert to W for nicer small-number display
        s.solar_w = float(data.get("pvPower", 0) or 0) * 1000
        s.home_w = float(data.get("loadsPower", 0) or 0) * 1000
        s.grid_import_w = float(data.get("gridConsumptionPower", 0) or 0) * 1000
        s.grid_export_w = float(data.get("feedinPower", 0) or 0) * 1000
        s.battery_charge_w = float(data.get("batChargePower", 0) or 0) * 1000
        s.battery_discharge_w = float(data.get("batDischargePower", 0) or 0) * 1000
        # Use key-existence check so a null value is treated as 0, not "missing"
        s.today_yield_kwh = None
        for _yield_key in ("todayYield", "pvEnergyToday", "generation"):
            if _yield_key in data:
                _raw = data[_yield_key]
                s.today_yield_kwh = float(_raw) if _raw is not None else 0.0
                break
        soc = data.get("SoC")
        s.battery_soc = float(soc) if soc is not None else None
        raw_inv = data.get("invTemperation")
        s.inverter_temp = float(raw_inv) if raw_inv is not None else None
        raw_amb = data.get("ambientTemperation")
        s.ambient_temp = float(raw_amb) if raw_amb is not None else None
        # Use the inverter's own timestamp — this is when the FoxESS cloud last
        # received data from the inverter, not when we polled.
        time_str = data.get("_inverter_time")
        if time_str:
            try:
                s.last_update = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                s.last_update = datetime.now()
        else:
            s.last_update = datetime.now()
        return s

    def status(self) -> str:
        """Short human-readable status used in the tray tooltip."""
        if self.last_error:
            return f"Error: {self.last_error}"
        if self.grid_export_w > 10:
            return f"Exporting {self.grid_export_w:.0f} W"
        if self.grid_import_w > 10:
            return f"Importing {self.grid_import_w:.0f} W"
        if self.battery_discharge_w > 10:
            return f"Battery {self.battery_discharge_w:.0f} W"
        return "Balanced"

    def color(self) -> tuple:
        """RGBA color of the tray dot based on current power flow."""
        if self.last_error:
            return (200, 50, 50, 255)         # red
        if self.grid_export_w > 10:
            return (40, 180, 60, 255)         # green: exporting
        if self.grid_import_w > 10:
            return (230, 170, 30, 255)        # amber: importing
        if self.battery_discharge_w > 10:
            return (60, 130, 220, 255)        # blue: on battery
        return (150, 150, 150, 255)           # gray: idle/balanced


# ----------------------------------------------------------------------------
# Tray icon image
# ----------------------------------------------------------------------------

import math
from PIL import ImageFilter


def _hexagon_points(cx, cy, radius, rotation_deg=0):
    rot = math.radians(rotation_deg)
    return [
        (cx + radius * math.cos(rot + i * math.pi / 3),
         cy + radius * math.sin(rot + i * math.pi / 3))
        for i in range(6)
    ]


def _bolt_points(cx, cy, w, h):
    """A clean Z-shaped lightning bolt fitting inside a rectangle."""
    unit = [
        ( 0.10, -0.50), (-0.30, -0.05), (-0.05, -0.05),
        (-0.10,  0.50), ( 0.30,  0.05), ( 0.05,  0.05),
    ]
    return [(cx + x * w, cy + y * h) for x, y in unit]


def make_tray_image(color: tuple) -> Image.Image:
    """
    Render the app icon: a rounded hexagon (whose color reflects status)
    with a yellow lightning bolt in the center. Drawn at 4x then downsampled
    so the edges stay smooth at small tray sizes.
    """
    size = 64
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx, cy = s / 2, s / 2
    hex_r = s * 0.47
    hex_pts = _hexagon_points(cx, cy, hex_r, rotation_deg=30)

    # Soft drop shadow
    shadow_img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_img)
    sd.polygon(_hexagon_points(cx, cy + s * 0.015, hex_r, rotation_deg=30),
               fill=(0, 0, 0, 90))
    shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(radius=s * 0.015))
    img.paste(shadow_img, (0, 0), shadow_img)

    # Main hexagon fill (status color)
    d.polygon(hex_pts, fill=color)

    # Glassy highlight on the upper half — soft, masked to stay inside the hex
    r, g, b, _ = color
    light = (min(255, r + 40), min(255, g + 40), min(255, b + 40), 80)
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).polygon(hex_pts, fill=255)
    overlay = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(overlay).ellipse(
        (cx - hex_r * 1.1, cy - hex_r * 1.5,
         cx + hex_r * 1.1, cy - hex_r * 0.05),
        fill=light,
    )
    overlay = overlay.filter(ImageFilter.GaussianBlur(radius=s * 0.025))
    img.paste(
        overlay, (0, 0),
        Image.composite(overlay.split()[3], Image.new("L", (s, s), 0), mask),
    )

    # Darker rim outline for definition at small sizes
    rim = (max(0, r - 60), max(0, g - 60), max(0, b - 60), 255)
    for i in range(len(hex_pts)):
        d.line([hex_pts[i], hex_pts[(i + 1) % len(hex_pts)]],
               fill=rim, width=max(2, int(s * 0.018)))

    # Lightning bolt — warm yellow with a darker outline
    bolt_w = hex_r * 1.1
    bolt_h = hex_r * 1.4
    bolt_pts = _bolt_points(cx, cy, bolt_w, bolt_h)

    # Bolt shadow
    shadow_pts = [(x + s * 0.012, y + s * 0.012) for x, y in bolt_pts]
    d.polygon(shadow_pts, fill=(0, 0, 0, 100))

    # Bolt fill
    d.polygon(bolt_pts, fill=(255, 215, 70, 255))

    # Bolt outline
    for i in range(len(bolt_pts)):
        d.line([bolt_pts[i], bolt_pts[(i + 1) % len(bolt_pts)]],
               fill=(160, 110, 10, 255), width=max(1, int(s * 0.01)))

    return img.resize((size, size), Image.LANCZOS)


# ----------------------------------------------------------------------------
# Main popup window
# ----------------------------------------------------------------------------

class DashboardWindow:
    """The tk window shown when you click 'Show' on the tray icon."""

    # FoxESS-ish color palette
    BG = "#1f1f2e"
    CARD = "#2a2a3d"
    TEXT = "#ffffff"
    SUB = "#a0a0b8"
    ACCENT = "#8b5cf6"          # the purple from the FoxESS header
    GREEN = "#28b43c"
    AMBER = "#e6aa1e"
    BLUE = "#3c82dc"

    def __init__(self, app: "FoxessApp"):
        self.app = app
        self.root = None
        self.root_ready_callback = None
        self.labels = {}
        self.updated_label = None
        self.info_model_label = None
        self.info_temp_label = None
        self.info_fault_label = None

    def run_main_loop(self):
        """Create the Tk root on the main thread and start the event loop."""
        self.root = tk.Tk()
        self.root.title("FoxESS Monitor")
        self.root.configure(bg=self.BG)
        self.root.geometry("660x440")
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._build_ui()
        self._schedule_refresh()
        if self.root_ready_callback is not None:
            self.root.after(0, self.root_ready_callback)
        self.root.mainloop()

    def show(self):
        """Thread-safe: schedule the window to appear on the Tk event loop."""
        if self.root is not None:
            self.root.after(0, self._do_show)

    def _do_show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _build_ui(self):
        INFO_BG = "#6d28d9"

        # ── Info bar: 3 columns (model/fw | temps | faults) ─────────────────
        info_bar = tk.Frame(self.root, bg=INFO_BG)
        info_bar.pack(fill="x")
        info_bar.columnconfigure(0, weight=1)
        info_bar.columnconfigure(1, weight=1)
        info_bar.columnconfigure(2, weight=1)

        self.info_model_label = tk.Label(
            info_bar, text="Model: —\nFW: —",
            bg=INFO_BG, fg=self.TEXT, font=("Segoe UI", 8), justify="left",
        )
        self.info_model_label.grid(row=0, column=0, padx=14, pady=6, sticky="w")

        self.info_temp_label = tk.Label(
            info_bar, text="Inv: —°C  |  Amb: —°C",
            bg=INFO_BG, fg=self.TEXT, font=("Segoe UI", 8), justify="center",
        )
        self.info_temp_label.grid(row=0, column=1, padx=14, pady=6)

        self.info_fault_label = tk.Label(
            info_bar, text="Faults: —",
            bg=INFO_BG, fg=self.TEXT, font=("Segoe UI", 8), justify="right",
        )
        self.info_fault_label.grid(row=0, column=2, padx=14, pady=6, sticky="e")

        # ── 2-column bento grid ──────────────────────────────────────────────
        grid = tk.Frame(self.root, bg=self.BG)
        grid.pack(fill="both", expand=True, padx=10, pady=10)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1)
        grid.rowconfigure(1, weight=1)
        grid.rowconfigure(2, weight=1)

        cards = [
            ("solar",   "Solar",               self.GREEN),
            ("home",    "Total Home",           self.TEXT),
            ("grid",    "Grid",                 self.AMBER),
            ("battery", "Battery",              self.GREEN),
            ("today",   "Electricity Generated Today",   self.SUB),
            ("soc",     "State of Charge",      self.BLUE),
        ]
        for idx, (key, title, color) in enumerate(cards):
            row, col = divmod(idx, 2)
            self._add_card(grid, key, title, color, row, col)

        # ── Footer ───────────────────────────────────────────────────────────
        footer = tk.Frame(self.root, bg=self.BG)
        footer.pack(fill="x", padx=12, pady=(0, 10))
        self.updated_label = tk.Label(footer, text="", bg=self.BG, fg=self.SUB,
                                      font=("Segoe UI", 8))
        self.updated_label.pack(side="left")
        tk.Button(footer, text="Refresh now", command=self.app.refresh_now,
                  bg=self.CARD, fg=self.TEXT, bd=0, padx=12, pady=4,
                  activebackground=self.ACCENT, activeforeground=self.TEXT,
                  font=("Segoe UI", 9)).pack(side="right")

    def _add_card(self, parent, key: str, title: str, value_color: str, row: int, col: int):
        card = tk.Frame(parent, bg=self.CARD)
        card.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)

        tk.Label(card, text=title, bg=self.CARD, fg=self.SUB,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=12, pady=(10, 0))
        value = tk.Label(card, text="—", bg=self.CARD, fg=value_color,
                         font=("Segoe UI", 20, "bold"))
        value.pack(anchor="w", padx=12)
        sub = tk.Label(card, text="", bg=self.CARD, fg=self.SUB,
                       font=("Segoe UI", 9))
        sub.pack(anchor="w", padx=12, pady=(0, 10))
        self.labels[key] = (value, sub)

    def _refresh_labels(self):
        if self.root is None:
            return
        s = self.app.state
        d = self.app.device_info

        def fmt_w(w: float) -> str:
            return f"{w / 1000:.2f} kW" if abs(w) >= 1000 else f"{w:.0f} W"

        # Solar
        self.labels["solar"][0].config(text=fmt_w(s.solar_w))
        self.labels["solar"][1].config(text="Live PV output")

        # Home
        self.labels["home"][0].config(text=fmt_w(s.home_w))
        self.labels["home"][1].config(text="Live consumption")

        # Grid
        grid_text, grid_sub, grid_color = self._grid_display(s)
        self.labels["grid"][0].config(text=grid_text, fg=grid_color)
        self.labels["grid"][1].config(text=grid_sub)

        # Battery (power flow)
        if s.battery_charge_w > 10:
            self.labels["battery"][0].config(text=fmt_w(s.battery_charge_w), fg=self.GREEN)
            self.labels["battery"][1].config(text="Charging")
        elif s.battery_discharge_w > 10:
            self.labels["battery"][0].config(text=fmt_w(s.battery_discharge_w), fg=self.AMBER)
            self.labels["battery"][1].config(text="Discharging")
        else:
            self.labels["battery"][0].config(text="0 W", fg=self.SUB)
            self.labels["battery"][1].config(text="Idle")

        # Today's yield
        if s.today_yield_kwh is not None:
            self.labels["today"][0].config(text=f"{s.today_yield_kwh:.3f} kWh")
            self.labels["today"][1].config(text="Since midnight")
        else:
            self.labels["today"][0].config(text="N/A")
            self.labels["today"][1].config(text="Not reported by inverter")

        # State of Charge
        if s.battery_soc is not None:
            self.labels["soc"][0].config(text=f"{s.battery_soc:.0f} %", fg=self.BLUE)
            self.labels["soc"][1].config(text="Battery level")
        else:
            self.labels["soc"][0].config(text="—", fg=self.SUB)
            self.labels["soc"][1].config(text="No battery")

        # Info bar — model / firmware
        self.info_model_label.config(text=f"Model: {d.model}\nFW: {d.firmware}")

        # Info bar — temperatures
        inv_t = f"{s.inverter_temp:.1f}°C" if s.inverter_temp is not None else "—"
        amb_t = f"{s.ambient_temp:.1f}°C" if s.ambient_temp is not None else "—"
        self.info_temp_label.config(text=f"Inv: {inv_t}  |  Amb: {amb_t}")

        # Info bar — faults
        if d.alarms:
            self.info_fault_label.config(text=f"⚠ {len(d.alarms)} fault(s)", fg="#ef4444")
        elif d.model != "—":
            self.info_fault_label.config(text="✓ No faults", fg=self.GREEN)
        else:
            self.info_fault_label.config(text="Faults: —", fg=self.SUB)

        # Footer timestamp
        if s.last_update:
            self.updated_label.config(text=f"Updated: {s.last_update.strftime('%H:%M:%S')}")

    def _grid_display(self, s: "DataState"):
        if s.grid_export_w > 10:
            return self._fmt_w(s.grid_export_w), "Exporting to grid", self.GREEN
        if s.grid_import_w > 10:
            return self._fmt_w(s.grid_import_w), "Importing from grid", self.AMBER
        return "0 W", "No grid flow", self.SUB

    @staticmethod
    def _fmt_w(w: float) -> str:
        return f"{w / 1000:.2f} kW" if abs(w) >= 1000 else f"{w:.0f} W"

    def _on_close(self):
        """Hide the window but keep the tray icon alive."""
        if self.root is not None:
            self.root.withdraw()

    def _schedule_refresh(self):
        """Drive label updates from the Tk event loop to avoid cross-thread issues."""
        try:
            self._refresh_labels()
        except Exception:
            pass
        self.root.after(1000, self._schedule_refresh)

    def notify_data_changed(self):
        pass


# ----------------------------------------------------------------------------
# Historical energy logs window
# ----------------------------------------------------------------------------

class LogsWindow:
    """Toplevel window showing hourly energy report for a selected date."""

    _COLS = [
        ("hour",       "Hour",        60),
        ("generation", "Generation",  90),
        ("feedin",     "Feed-in",     80),
        ("grid",       "Grid",        80),
        ("loads",      "Home",        80),
        ("charge",     "Charge",      80),
        ("discharge",  "Discharge",   90),
    ]

    def __init__(self, master: tk.Tk, api_key: str, device_sn: str):
        self._client = FoxessClient(api_key, device_sn)
        self.dlg = tk.Toplevel(master)
        self.dlg.title("Historical Energy Data")
        self.dlg.geometry("720x460")
        self.dlg.resizable(True, True)
        self.dlg.configure(bg="#1f1f2e")
        self.dlg.transient(master)
        self._build()

    def _build(self):
        today = datetime.now()
        BG, CARD, SUB = "#1f1f2e", "#2a2a3d", "#a0a0b8"

        # ── Date picker row ──────────────────────────────────────────────────
        top = tk.Frame(self.dlg, bg=BG)
        top.pack(fill="x", padx=16, pady=(16, 8))
        tk.Label(top, text="Date:", bg=BG, fg="white", font=("Segoe UI", 9)).pack(side="left")

        self._year  = tk.IntVar(value=today.year)
        self._month = tk.IntVar(value=today.month)
        self._day   = tk.IntVar(value=today.day)

        spin_kw = dict(bg=CARD, fg="white", buttonbackground=CARD,
                       insertbackground="white", relief="flat")
        tk.Spinbox(top, from_=2020, to=2099, textvariable=self._year,
                   width=6, **spin_kw).pack(side="left", padx=(8, 2))
        tk.Label(top, text="-", bg=BG, fg="white").pack(side="left")
        tk.Spinbox(top, from_=1, to=12, textvariable=self._month,
                   width=4, **spin_kw).pack(side="left", padx=2)
        tk.Label(top, text="-", bg=BG, fg="white").pack(side="left")
        tk.Spinbox(top, from_=1, to=31, textvariable=self._day,
                   width=4, **spin_kw).pack(side="left", padx=2)

        tk.Button(top, text="Fetch", command=self._fetch,
                  bg="#8b5cf6", fg="white", bd=0, padx=12, pady=3,
                  font=("Segoe UI", 9)).pack(side="left", padx=(12, 0))

        self._status = tk.StringVar(value="Select a date and click Fetch.")
        tk.Label(top, textvariable=self._status, bg=BG, fg=SUB,
                 font=("Segoe UI", 8)).pack(side="right")

        # ── Data table ───────────────────────────────────────────────────────
        frame = tk.Frame(self.dlg, bg=BG)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        style = ttk.Style(self.dlg)
        style.theme_use("default")
        style.configure("Logs.Treeview",
                        background=CARD, foreground="white",
                        fieldbackground=CARD, rowheight=24,
                        font=("Segoe UI", 9))
        style.configure("Logs.Treeview.Heading",
                        background=BG, foreground=SUB,
                        font=("Segoe UI", 9, "bold"))
        style.map("Logs.Treeview", background=[("selected", "#8b5cf6")])

        col_ids = [c[0] for c in self._COLS]
        self._tree = ttk.Treeview(frame, columns=col_ids,
                                  show="headings", style="Logs.Treeview")
        for col_id, label, width in self._COLS:
            self._tree.heading(col_id, text=label)
            self._tree.column(col_id, width=width, anchor="center", stretch=True)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

    def _fetch(self):
        self._status.set("Fetching…")
        self.dlg.update_idletasks()
        try:
            data = self._client.get_energy_report(
                self._year.get(), self._month.get(), self._day.get()
            )
        except Exception as exc:
            self._status.set(f"Error: {exc}")
            return

        # Build {variable: {index: value}} lookup
        by_var: dict[str, dict] = {}
        for entry in data:
            by_var[entry.get("variable", "")] = {
                v["index"]: v.get("value") for v in entry.get("values", [])
            }

        for row in self._tree.get_children():
            self._tree.delete(row)

        def _f(v) -> str:
            return "—" if v is None else f"{float(v):.3f}"

        for h in range(24):
            self._tree.insert("", "end", values=(
                f"{h:02d}:00",
                _f(by_var.get("generation",      {}).get(h)),
                _f(by_var.get("feedin",          {}).get(h)),
                _f(by_var.get("gridConsumption", {}).get(h)),
                _f(by_var.get("loads",           {}).get(h)),
                _f(by_var.get("chargeEnergy",    {}).get(h)),
                _f(by_var.get("dischargeEnergy", {}).get(h)),
            ))
        self._status.set(
            f"Showing {self._year.get()}-{self._month.get():02d}-{self._day.get():02d}  "
            f"(all values in kWh)"
        )


# ----------------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------------

def show_settings_dialog(current: dict, master: tk.Tk | None = None) -> dict | None:
    """
    Modal settings dialog. When master is supplied it opens as a Toplevel.
    Returns the new config dict, or None if cancelled.
    """
    result = {}
    if master is not None:
        dlg = tk.Toplevel(master)
        dlg.transient(master)
    else:
        dlg = tk.Tk()
    dlg.title("FoxESS Settings")
    dlg.geometry("420x240")
    dlg.resizable(False, False)
    dlg.configure(bg="#1f1f2e")

    tk.Label(dlg, text="API Key", bg="#1f1f2e", fg="white",
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(16, 2))
    api_var = tk.StringVar(value=current.get("api_key", ""))
    tk.Entry(dlg, textvariable=api_var, width=50, show="•").pack(padx=16, fill="x")

    tk.Label(dlg, text="Device SN (inverter serial number)",
             bg="#1f1f2e", fg="white",
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(12, 2))
    sn_var = tk.StringVar(value=current.get("device_sn", ""))
    tk.Entry(dlg, textvariable=sn_var, width=50).pack(padx=16, fill="x")

    btns = tk.Frame(dlg, bg="#1f1f2e")
    btns.pack(fill="x", padx=16, pady=16)

    saved = {"ok": False}

    def on_save():
        if not api_var.get().strip() or not sn_var.get().strip():
            messagebox.showerror("Missing data", "Both fields are required.", parent=dlg)
            return
        result["api_key"] = api_var.get().strip()
        result["device_sn"] = sn_var.get().strip()
        saved["ok"] = True
        dlg.destroy()

    def on_cancel():
        dlg.destroy()

    def on_view_logs():
        key = api_var.get().strip()
        sn = sn_var.get().strip()
        if not key or not sn:
            messagebox.showwarning("Missing data",
                                   "Enter API key and Device SN first.", parent=dlg)
            return
        LogsWindow(master or dlg, key, sn)

    tk.Button(btns, text="Save", command=on_save,
              bg="#8b5cf6", fg="white", bd=0, padx=16, pady=4).pack(side="right")
    tk.Button(btns, text="Cancel", command=on_cancel,
              bg="#2a2a3d", fg="white", bd=0, padx=16, pady=4
              ).pack(side="right", padx=(0, 8))
    tk.Button(btns, text="View Logs", command=on_view_logs,
              bg="#2a2a3d", fg="white", bd=0, padx=16, pady=4).pack(side="left")

    if master is not None:
        master.wait_window(dlg)
    else:
        dlg.mainloop()
    return result if saved["ok"] else None


# ----------------------------------------------------------------------------
# The app — wires the tray icon, window, and polling thread together
# ----------------------------------------------------------------------------

class FoxessApp:
    def __init__(self):
        self.config = load_config()
        self.state = DataState()
        self.device_info = DeviceInfo()
        self.window = DashboardWindow(self)
        self.icon: pystray.Icon | None = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._poll_count = 0

    # ---- API + polling ----

    def _client(self) -> FoxessClient | None:
        if not self.config.get("api_key") or not self.config.get("device_sn"):
            return None
        return FoxessClient(self.config["api_key"], self.config["device_sn"])

    def _poll_once(self):
        """One refresh cycle — fetch, update state, update tray + window."""
        self._poll_count += 1
        # Refresh device info (model, firmware, alarms) on first poll and every ~60 s
        if self._poll_count % 12 == 1:
            threading.Thread(target=self._fetch_device_info, daemon=True).start()

        client = self._client()
        if client is None:
            self.state.last_error = "Not configured. Right-click → Settings."
        else:
            try:
                data = client.get_realtime()
                new_state = DataState.from_api(data)
                self.state = new_state
            except Exception as e:  # noqa: BLE001 — surface any failure to UI
                self.state.last_error = str(e)
                self.state.last_update = datetime.now()

        # Update tray icon + tooltip
        if self.icon is not None:
            self.icon.icon = make_tray_image(self.state.color())
            self.icon.title = self._tooltip()

        # Update window if open
        self.window.notify_data_changed()

    def _fetch_device_info(self):
        """Background: fetch model, firmware, and recent alarms."""
        client = self._client()
        if client is None:
            return
        try:
            detail = client.get_device_detail()
            self.device_info.model = detail.get("deviceType") or "—"
            self.device_info.firmware = detail.get("softVersion") or "—"
        except Exception:
            pass
        try:
            self.device_info.alarms = client.get_alarms()
        except Exception:
            pass

    def _tooltip(self) -> str:
        s = self.state
        if s.last_error:
            return f"FoxESS: {s.last_error[:80]}"
        return (
            f"FoxESS — {s.status()}\n"
            f"Solar: {s.solar_w:.0f} W\n"
            f"Home: {s.home_w:.0f} W\n"
            f"Grid import: {s.grid_import_w:.0f} W  export: {s.grid_export_w:.0f} W"
        )

    def _poll_loop(self):
        """Background thread that polls every REFRESH_SECONDS."""
        while not self._stop_event.is_set():
            start = time.monotonic()
            self._poll_once()
            elapsed = time.monotonic() - start
            remaining = max(0.0, REFRESH_SECONDS - elapsed)
            self._wakeup_event.wait(timeout=remaining)
            self._wakeup_event.clear()

    # ---- Tray menu actions ----

    def refresh_now(self):
        """Trigger an immediate refresh from any thread."""
        self._wakeup_event.set()

    def open_window(self, icon=None, item=None):
        self.window.show()  # thread-safe via root.after(0, ...)

    def open_settings(self, icon=None, item=None):
        # Schedule the dialog on the Tk event loop so it always runs on the
        # main thread and can safely use Toplevel instead of a second Tk root.
        if self.window.root is not None:
            self.window.root.after(0, self._show_settings_on_tk)

    def _show_settings_on_tk(self):
        new_cfg = show_settings_dialog(self.config, master=self.window.root)
        if new_cfg is not None:
            self.config = new_cfg
            save_config(self.config)
            self.refresh_now()

    def quit_app(self, icon=None, item=None):
        self._stop_event.set()
        self._wakeup_event.set()
        if self.icon is not None:
            self.icon.stop()
        # Destroy the Tk root on the main thread so mainloop() returns.
        if self.window.root is not None:
            self.window.root.after(0, self.window.root.destroy)

    # ---- Entry point ----

    def run(self):
        # Build the tray icon (don't start it yet)
        menu = Menu(
            item("Show window", self.open_window, default=True),
            item("Refresh now", lambda icon, item: self.refresh_now()),
            Menu.SEPARATOR,
            item("Settings…", self.open_settings),
            Menu.SEPARATOR,
            item("Quit", self.quit_app),
        )
        self.icon = pystray.Icon(
            "foxess_tray",
            make_tray_image(self.state.color()),
            APP_NAME,
            menu,
        )

        # Start polling + pystray on daemon threads so the main thread is free
        # for Tkinter, which needs the main thread on Windows to fire after() callbacks.
        threading.Thread(target=self._poll_loop, daemon=True).start()
        threading.Thread(target=self.icon.run, daemon=True).start()

        # If first run with no config, show the settings dialog once Tk is ready.
        if not self.config.get("api_key") or not self.config.get("device_sn"):
            self.window.root_ready_callback = self._show_initial_settings

        # Blocks on the main thread until quit_app destroys the root.
        self.window.run_main_loop()

    def _show_initial_settings(self):
        new_cfg = show_settings_dialog(self.config, master=self.window.root)
        if new_cfg is not None:
            self.config = new_cfg
            save_config(self.config)
            self.refresh_now()


def main():
    app = FoxessApp()
    app.run()


if __name__ == "__main__":
    main()
