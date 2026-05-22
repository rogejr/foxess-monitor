# Changelog

All notable changes to FoxESS Tray Monitor are documented here.

---

## [1.1.0] — 2026-05-22

### Added
- **Inverter info bar** — a 3-column header strip below the title showing Model, Firmware version, and Temperature (inverter + ambient). Refreshes every 60 seconds.
- **Fault/error code display** — the info bar shows active alarm codes; clears to "No faults" when none are present. Fetched via the FoxESS alarm query endpoint.
- **State of Charge card** — dedicated bento card showing battery SoC percentage, replacing the previous "Last Updated" card.
- **Electricity Generated Today card** — shows today's yield in kWh using `todayYield` with fallbacks to `pvEnergyToday` and `generation` so the value appears on all inverter firmware variants.
- **Historical Energy Logs** — "View Logs" button in Settings opens a separate window with a date-range picker and a table of hourly energy figures (Generation, Feed-in, Grid, Home, Charge, Discharge). Data fetched from the FoxESS energy report endpoint.
- **Auto-open dashboard on startup** — the dashboard window appears automatically when the app launches, without needing to click the tray icon.
- **Last Updated timestamp** — footer of the dashboard shows the actual time the inverter last reported data (taken from the API `time` field), not the local poll time.

### Changed
- **2-column bento landscape layout** — dashboard redesigned from a single vertical list to a 2 × 3 bento grid (660 × 440 px). Cards: Solar, Total Home / Grid, Battery watts / Electricity Generated Today, State of Charge.
- **Title bar removed** — the purple "FoxESS Monitor" header row and the "Importing / Exporting" status label are gone; inverter info bar takes their place.
- **"Today's Production" renamed** to "Electricity Generated Today".
- **Tkinter threading architecture** — Tkinter now owns the main thread; pystray runs on a daemon thread. This unblocks `after()` callbacks on Windows, which were silently dropped when Tkinter ran on a daemon thread, causing the window to appear frozen.
- **Settings dialog** — converted from a second `tk.Tk()` root to a `Toplevel` on the existing root. Prevents multiple-root conflicts and enables proper modal behaviour.
- **Refresh interval accuracy** — the poll loop now measures elapsed time with `time.monotonic()` and sleeps only the remaining slice, so the effective cycle matches `REFRESH_SECONDS` regardless of API response time.

### Fixed
- **Dashboard window frozen** — `after()` callbacks were silently dropped when Tkinter ran on a daemon thread. Moving Tkinter to the main thread resolved this.
- **Effective refresh longer than `REFRESH_SECONDS`** — cycle was `api_duration + REFRESH_SECONDS`. Fixed with monotonic elapsed tracking.
- **Multiple `tk.Tk()` instances** — Settings dialog created a second root. Fixed by using `Toplevel`.
- **Electricity Generated Today showing N/A** — caused by two separate bugs: (a) the API returns the key with a `null` value on some firmware versions; fixed by checking `if key in data` rather than `if value is not None`; (b) variable name differs across firmware versions; fixed by trying `todayYield` → `pvEnergyToday` → `generation` in order.
- **Timestamp incrementing every 5 s regardless of data age** — `last_update` was set to `datetime.now()` on every poll. Now populated from the inverter's own `time` field in the API response.
- **API error 40257 when fetching energy logs** — `chargeEnergy` / `dischargeEnergy` are rejected by the API for inverters without a battery. Fixed by retrying with the battery variables removed on a 40257 response.

---

## [1.0.2] — 2026-05-20

### Changed
- `REFRESH_SECONDS` reduced from 30 to 5 for more responsive live data.

---

## [1.0.1] — 2026-05-20

### Added
- PyInstaller build pipeline (`build.ps1`, `FoxessTray.spec`) producing a single self-contained `dist\FoxessTray.exe`.
- Inno Setup installer definition (`installer.iss`) packaging the exe into a per-user `FoxessTraySetup-1.0.0.exe` with Start Menu shortcuts, optional autostart, and an uninstaller.

---

## [1.0.0] — 2026-05-19

### Added
- Windows system tray icon (colored hexagon + lightning bolt) that reflects current power-flow state: green (exporting), amber (importing), blue (on battery), red (error), gray (idle).
- Dashboard popup window showing live Solar, Home consumption, Grid import/export, Battery SoC, and Today's production.
- Background polling thread querying the FoxESS Cloud OpenAPI (`POST /op/v0/device/real/query`) on a configurable interval.
- FoxESS HMAC-style MD5 request signing (`path\r\n token\r\n timestamp` literal — not CR+LF).
- Config persistence to `%APPDATA%\FoxessTray\config.json` (API key + Device SN).
- First-run Settings dialog; re-openable from the tray right-click menu.
- "Refresh now" tray menu action and in-window button for an immediate poll.
- PyInstaller `resource_path()` helper for bundled-resource resolution at runtime.
