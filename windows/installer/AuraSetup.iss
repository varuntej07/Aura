; Inno Setup script for the Buddy desktop overlay (Aura for Windows).
; Build order (from repo root):
;   1. flutter build windows --release -t lib/main_desktop.dart
;   2. ISCC.exe windows\installer\AuraSetup.iss
; Output: build\windows\installer\AuraSetup.exe
;
; Per-user install (no admin prompt, installs under %LOCALAPPDATA%\Programs),
; matching how VS Code's user setup ships. The MSVC runtime DLLs are bundled
; app-local from the build machine because flutter build does not copy them
; and a clean Windows install may not have the VC++ redistributable.

#define MyAppName "Aura"
#define MyAppPublisher "Aura"
#define MyAppVersion "2.0.1"
#define MyAppExeName "aura.exe"
#define MyAppUrl "https://auravoiceapp.com"
#define BuildDir "..\..\build\windows\x64\runner\Release"

[Setup]
; Stable app identity: never change this GUID, upgrades key off it.
AppId={{7E3D9C41-52B6-4A87-9F0E-A16C83D2B5F4}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppUrl}
AppSupportURL={#MyAppUrl}
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\build\windows\installer
OutputBaseFilename=AuraSetup
SetupIconFile=..\runner\resources\app_icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; App-local MSVC runtime so the app launches on machines without the VC++ redist.
Source: "{sys}\msvcp140.dll"; DestDir: "{app}"; Flags: ignoreversion external
Source: "{sys}\vcruntime140.dll"; DestDir: "{app}"; Flags: ignoreversion external
Source: "{sys}\vcruntime140_1.dll"; DestDir: "{app}"; Flags: ignoreversion external

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
