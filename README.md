# FoxESS Tray Monitor

A small Windows system-tray + popup-window app that shows live data from your
FoxESS Cloud account: solar harvest, grid import/export, total home
consumption, and battery state of charge. Refreshes every 30 seconds.

The tray icon is a colored dot that tells you what's happening at a glance:

| Color  | Meaning                              |
|--------|--------------------------------------|
| Green  | Exporting solar to the grid          |
| Amber  | Importing from the grid              |
| Blue   | Running on battery (discharging)     |
| Gray   | Balanced / idle                      |
| Red    | Error contacting FoxESS              |

Left-click the tray icon to open the dashboard window. Right-click for
**Refresh now**, **Settings**, or **Quit**.

---

## A. I just want to install it (the .exe / installer)

If someone has already built `FoxessTraySetup-1.0.0.exe` for you, just:

1. **Double-click `FoxessTraySetup-1.0.0.exe`**
2. Click **Next** → pick whether you want a desktop shortcut and/or autostart
   → **Install**
3. The app launches automatically on **Finish**. It pops up a settings
   dialog asking for two things:
   - **API Key** — generate one at <https://www.foxesscloud.com> →
     avatar → User Profile → API Management
   - **Device SN** — your inverter's serial number, listed on the Devices
     page
4. Within ~30 seconds the tray icon goes from red (error) to green / amber /
   blue depending on what your system is doing.

To uninstall later: Windows **Settings → Apps → FoxESS Tray Monitor →
Uninstall**.

The installer is per-user (no admin password required) and installs to
`%LOCALAPPDATA%\Programs\FoxessTray` by default.

---

## B. I want to BUILD the .exe / installer from source

You only need to do this once, on a Windows PC. The output is the
`FoxessTraySetup-1.0.0.exe` from section A, which you can copy to any
other Windows PC.

### One-time prerequisites

1. **Python 3.10 or newer** — <https://www.python.org/downloads/windows/>.
   Tick **"Add Python to PATH"** during install.
2. **Inno Setup 6** — <https://jrsoftware.org/isdl.php> (free, ~5 MB).
   This is what turns the standalone .exe into a proper installer with
   Start Menu shortcuts and an uninstaller.

### Build it

Open **PowerShell** in this project folder and run:

```powershell
.\build.ps1
```

If PowerShell complains about script execution policy, run this first
(just once, in the same window):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

The script will:

1. Install the Python dependencies (`requests`, `pystray`, `Pillow`,
   `pyinstaller`)
2. Bundle everything into **`dist\FoxessTray.exe`** — a single ~15 MB
   self-contained executable that runs on any 64-bit Windows 10/11
   machine without needing Python installed
3. Wrap it in **`installer\FoxessTraySetup-1.0.0.exe`** — the
   installer you actually distribute

If Inno Setup isn't installed, step 3 is skipped but you'll still get the
portable `dist\FoxessTray.exe`. You can just double-click that directly —
it's the same program, just without a Start Menu entry.

---

## File layout

```
foxess-tray-installer/
├── foxess_tray.py        ← the actual application code
├── foxess.ico            ← multi-size icon used by exe + installer
├── foxess.png            ← icon used by the in-app window
├── requirements.txt      ← Python dependencies
├── build.ps1             ← run this on Windows to build everything
├── installer.iss         ← Inno Setup definition (don't edit unless tweaking)
└── README.md             ← this file
```

---

## Troubleshooting

- **Red dot in tray + "Not configured"**: right-click tray → **Settings**,
  paste your API key and Device SN.
- **HTTP 40256 / 40257**: API key or signature mismatch. Re-paste the key
  carefully (no trailing spaces); make sure your PC's clock is correct
  (the signature uses a timestamp).
- **"API returned no result"**: the Device SN doesn't match your account.
  Look it up again on the FoxESS Devices page.
- **Tray icon doesn't appear**: Windows sometimes hides new tray icons.
  Click the small ^ arrow in the system tray and drag the FoxESS icon
  out so it stays visible.
- **Rate limit (HTTP 40400)**: the API quota is 1440 calls/inverter/day,
  well above what 30-second polling needs (~2880/day across endpoints,
  but we only hit one). If you hit this, something else is also calling
  the API. Edit `REFRESH_SECONDS` in `foxess_tray.py` and rebuild.
- **Config location**: `%APPDATA%\FoxessTray\config.json`. Delete this
  file to fully reset.

---

## How it works (brief)

- **Polling thread** calls `POST /op/v0/device/real/query` every 30 seconds.
- **Signature** is `md5(path \r\n token \r\n timestamp)` per the FoxESS
  OpenAPI docs.
- **Tkinter** renders the dashboard window; **pystray** + **Pillow** draw
  the tray icon. The polling thread shares a `DataState` object with the UI.
- **PyInstaller** bundles Python + all dependencies into a single .exe.
- **Inno Setup** wraps that .exe in a per-user installer with Start Menu
  shortcuts, optional autostart, and an uninstaller.
