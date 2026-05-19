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
    "todayYield",           # Today's solar production (kWh)
]


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
        return flat


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
        self.today_yield_kwh = 0.0
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
        s.today_yield_kwh = float(data.get("todayYield", 0) or 0)
        soc = data.get("SoC")
        s.battery_soc = float(soc) if soc is not None else None
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
        self.labels = {}
        self.status_label = None
        self.updated_label = None

    def show(self):
        """Create the window if needed, then bring it to the front."""
        if self.root is not None:
            try:
                self.root.deiconify()
                self.root.lift()
                self.root.focus_force()
                self._refresh_labels()
                return
            except tk.TclError:
                self.root = None

        self.root = tk.Tk()
        self.root.title("FoxESS Monitor")
        self.root.configure(bg=self.BG)
        self.root.geometry("420x520")
        self.root.resizable(False, False)
        # Closing the X button just hides; the app keeps running in the tray
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._refresh_labels()
        self.root.mainloop()

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=self.ACCENT, height=60)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="FoxESS Monitor",
                 bg=self.ACCENT, fg=self.TEXT,
                 font=("Segoe UI", 16, "bold")).pack(side="left", padx=20, pady=15)
        self.status_label = tk.Label(
            header, text="—", bg=self.ACCENT, fg=self.TEXT,
            font=("Segoe UI", 10),
        )
        self.status_label.pack(side="right", padx=20)

        # Body: a stack of metric cards
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=16, pady=16)

        self._add_card(body, "solar", "Solar", "W", self.GREEN)
        self._add_card(body, "home", "Total Home", "W", self.TEXT)
        self._add_card(body, "grid", "Grid", "W", self.AMBER)
        self._add_card(body, "battery", "Battery", "%", self.BLUE)
        self._add_card(body, "today", "Today's Production", "kWh", self.SUB)

        # Footer
        footer = tk.Frame(self.root, bg=self.BG)
        footer.pack(fill="x", padx=16, pady=(0, 12))
        self.updated_label = tk.Label(
            footer, text="Never updated", bg=self.BG, fg=self.SUB,
            font=("Segoe UI", 8),
        )
        self.updated_label.pack(side="left")

        tk.Button(
            footer, text="Refresh now",
            command=self.app.refresh_now,
            bg=self.CARD, fg=self.TEXT, bd=0, padx=12, pady=4,
            activebackground=self.ACCENT, activeforeground=self.TEXT,
            font=("Segoe UI", 9),
        ).pack(side="right")

    def _add_card(self, parent, key: str, title: str, unit: str, value_color: str):
        """Create one metric card: title on top, big value below, unit suffix."""
        card = tk.Frame(parent, bg=self.CARD)
        card.pack(fill="x", pady=6, ipady=8)

        tk.Label(card, text=title, bg=self.CARD, fg=self.SUB,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=14, pady=(8, 0))
        value = tk.Label(card, text="—", bg=self.CARD, fg=value_color,
                         font=("Segoe UI", 22, "bold"))
        value.pack(anchor="w", padx=14)
        sub = tk.Label(card, text="", bg=self.CARD, fg=self.SUB,
                       font=("Segoe UI", 9))
        sub.pack(anchor="w", padx=14, pady=(0, 4))
        self.labels[key] = (value, sub, unit)

    def _refresh_labels(self):
        """Pull values out of app.state and push them into the UI."""
        if self.root is None:
            return
        s = self.app.state

        def fmt_w(w: float) -> str:
            # Show in kW once we're above 1000 W, like the FoxESS dashboard
            if abs(w) >= 1000:
                return f"{w / 1000:.2f}", "kW"
            return f"{w:.0f}", "W"

        # Solar
        v, u = fmt_w(s.solar_w)
        self.labels["solar"][0].config(text=f"{v} {u}")
        self.labels["solar"][1].config(text="Live PV output")

        # Home
        v, u = fmt_w(s.home_w)
        self.labels["home"][0].config(text=f"{v} {u}")
        self.labels["home"][1].config(text="Live consumption")

        # Grid: show either import or export, color-coded
        grid_val, grid_unit, grid_sub, grid_color = self._grid_display(s)
        self.labels["grid"][0].config(text=f"{grid_val} {grid_unit}", fg=grid_color)
        self.labels["grid"][1].config(text=grid_sub)

        # Battery
        if s.battery_soc is not None:
            self.labels["battery"][0].config(text=f"{s.battery_soc:.0f} %")
            if s.battery_charge_w > 10:
                sub = f"Charging at {s.battery_charge_w:.0f} W"
            elif s.battery_discharge_w > 10:
                sub = f"Discharging at {s.battery_discharge_w:.0f} W"
            else:
                sub = "Idle"
            self.labels["battery"][1].config(text=sub)
        else:
            self.labels["battery"][0].config(text="—")
            self.labels["battery"][1].config(text="No battery detected")

        # Today's yield
        self.labels["today"][0].config(text=f"{s.today_yield_kwh:.2f} kWh")
        self.labels["today"][1].config(text="Since midnight")

        # Status bar
        self.status_label.config(text=s.status())
        if s.last_update:
            self.updated_label.config(
                text=f"Last update: {s.last_update.strftime('%H:%M:%S')}"
            )

    def _grid_display(self, s: DataState):
        """Decide what to render in the Grid card."""
        if s.grid_export_w > 10:
            v_str, unit = self._fmt_w_pair(s.grid_export_w)
            return v_str, unit, "Exporting to grid", self.GREEN
        if s.grid_import_w > 10:
            v_str, unit = self._fmt_w_pair(s.grid_import_w)
            return v_str, unit, "Importing from grid", self.AMBER
        return "0", "W", "No grid flow", self.SUB

    @staticmethod
    def _fmt_w_pair(w: float):
        if abs(w) >= 1000:
            return f"{w / 1000:.2f}", "kW"
        return f"{w:.0f}", "W"

    def _on_close(self):
        """Hide the window but keep the tray icon alive."""
        if self.root is not None:
            self.root.withdraw()

    def notify_data_changed(self):
        """Called by the app from the polling thread after a successful refresh."""
        if self.root is not None:
            try:
                self.root.after(0, self._refresh_labels)
            except tk.TclError:
                pass


# ----------------------------------------------------------------------------
# Settings dialog
# ----------------------------------------------------------------------------

def show_settings_dialog(current: dict) -> dict | None:
    """
    Modal settings dialog. Returns the new config dict, or None if cancelled.
    Runs its own tk root since it can be called from the tray menu before the
    main window exists.
    """
    result = {}
    root = tk.Tk()
    root.title("FoxESS Settings")
    root.geometry("420x220")
    root.resizable(False, False)
    root.configure(bg="#1f1f2e")

    tk.Label(root, text="API Key", bg="#1f1f2e", fg="white",
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(16, 2))
    api_var = tk.StringVar(value=current.get("api_key", ""))
    api_entry = tk.Entry(root, textvariable=api_var, width=50, show="•")
    api_entry.pack(padx=16, fill="x")

    tk.Label(root, text="Device SN (inverter serial number)",
             bg="#1f1f2e", fg="white",
             font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(12, 2))
    sn_var = tk.StringVar(value=current.get("device_sn", ""))
    tk.Entry(root, textvariable=sn_var, width=50).pack(padx=16, fill="x")

    btns = tk.Frame(root, bg="#1f1f2e")
    btns.pack(fill="x", padx=16, pady=16)

    saved = {"ok": False}

    def on_save():
        if not api_var.get().strip() or not sn_var.get().strip():
            messagebox.showerror("Missing data", "Both fields are required.")
            return
        result["api_key"] = api_var.get().strip()
        result["device_sn"] = sn_var.get().strip()
        saved["ok"] = True
        root.destroy()

    def on_cancel():
        root.destroy()

    tk.Button(btns, text="Save", command=on_save,
              bg="#8b5cf6", fg="white", bd=0, padx=16, pady=4).pack(side="right")
    tk.Button(btns, text="Cancel", command=on_cancel,
              bg="#2a2a3d", fg="white", bd=0, padx=16, pady=4
              ).pack(side="right", padx=(0, 8))

    root.mainloop()
    return result if saved["ok"] else None


# ----------------------------------------------------------------------------
# The app — wires the tray icon, window, and polling thread together
# ----------------------------------------------------------------------------

class FoxessApp:
    def __init__(self):
        self.config = load_config()
        self.state = DataState()
        self.window = DashboardWindow(self)
        self.icon: pystray.Icon | None = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()

    # ---- API + polling ----

    def _client(self) -> FoxessClient | None:
        if not self.config.get("api_key") or not self.config.get("device_sn"):
            return None
        return FoxessClient(self.config["api_key"], self.config["device_sn"])

    def _poll_once(self):
        """One refresh cycle — fetch, update state, update tray + window."""
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
            self._poll_once()
            # Wait for either the refresh interval or an explicit wakeup
            self._wakeup_event.wait(timeout=REFRESH_SECONDS)
            self._wakeup_event.clear()

    # ---- Tray menu actions ----

    def refresh_now(self):
        """Trigger an immediate refresh from any thread."""
        self._wakeup_event.set()

    def open_window(self, icon=None, item=None):
        # Window has its own mainloop, so run in a separate thread to avoid
        # blocking the tray's own event loop.
        threading.Thread(target=self.window.show, daemon=True).start()

    def open_settings(self, icon=None, item=None):
        # Settings dialog must run on a thread that isn't already running a tk root.
        # We spawn a worker thread that creates its own Tk root for the dialog.
        def worker():
            new_cfg = show_settings_dialog(self.config)
            if new_cfg is not None:
                self.config = new_cfg
                save_config(self.config)
                self.refresh_now()
        threading.Thread(target=worker, daemon=True).start()

    def quit_app(self, icon=None, item=None):
        self._stop_event.set()
        self._wakeup_event.set()
        if self.icon is not None:
            self.icon.stop()
        # Close any open tk window
        try:
            if self.window.root is not None:
                self.window.root.after(0, self.window.root.destroy)
        except Exception:
            pass

    # ---- Entry point ----

    def run(self):
        # If first run with no config, pop the settings dialog up front.
        if not self.config.get("api_key") or not self.config.get("device_sn"):
            new_cfg = show_settings_dialog(self.config)
            if new_cfg is not None:
                self.config = new_cfg
                save_config(self.config)

        # Start the polling thread
        threading.Thread(target=self._poll_loop, daemon=True).start()

        # Build the tray icon
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
        # icon.run() blocks until quit_app is called
        self.icon.run()


def main():
    app = FoxessApp()
    app.run()


if __name__ == "__main__":
    main()
