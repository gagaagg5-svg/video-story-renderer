#define AppName "Niche Scout"
#define AppVersion "0.1.1"

[Setup]
AppId={{CF0DAE58-3316-4EFA-A734-6BB26C78E52D}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Niche Scout
DefaultDirName={localappdata}\Programs\NicheScout
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\installer-output
OutputBaseFilename=NicheScout-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\NicheScout.exe
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\NicheScout.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README_WINDOWS_RU.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README_RU.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\NicheScout.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\NicheScout.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\NicheScout.exe"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent
