; installer.iss — Inno Setup script for FoxESS Tray Monitor
; Compile with: ISCC.exe installer.iss   (or run build.ps1 which does it for you)

#define MyAppName        "FoxESS Tray Monitor"
#define MyAppVersion     "1.0.0"
#define MyAppPublisher   "Roge"
#define MyAppExeName     "FoxessTray.exe"

[Setup]
; A new GUID gives the installer its own identity for upgrades / uninstall
AppId={{B7F4E2C1-3A8D-4F92-9E2B-1C5D7A8F3B6E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\FoxessTray
DefaultGroupName=FoxESS Tray Monitor
UninstallDisplayIcon={app}\{#MyAppExeName}
OutputDir=installer
OutputBaseFilename=FoxessTraySetup-{#MyAppVersion}
SetupIconFile=foxess.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; Per-user install — no admin rights needed
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; Modern installer look
DisableProgramGroupPage=yes
DisableReadyPage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Optional checkboxes on the install wizard
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"
Name: "startupicon"; Description: "Start automatically when Windows starts"; GroupDescription: "Autostart:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md";            DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Start Menu
Name: "{autoprograms}\{#MyAppName}";          Filename: "{app}\{#MyAppExeName}"
Name: "{autoprograms}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
; Desktop (optional)
Name: "{autodesktop}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
; Startup folder (optional)
Name: "{userstartup}\{#MyAppName}";  Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Offer to launch the app right after install
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Clean up the config folder on uninstall so reinstalls start fresh.
; Comment this out if you'd rather keep API keys across reinstalls.
Type: filesandordirs; Name: "{userappdata}\FoxessTray"
