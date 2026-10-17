#define AppName "Video Story Renderer"
#define AppVersion "0.2.0"
[Setup]
AppId={{D3F4F13D-06F7-46DE-9378-9746CB86B0E4}
AppName={#AppName}
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\VideoStoryRenderer
DefaultGroupName={#AppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
WizardStyle=modern
Compression=lzma2
SolidCompression=yes
OutputDir=..\installer-output
OutputBaseFilename=VideoStoryRenderer-Setup
UninstallDisplayIcon={app}\VideoStoryRenderer.exe
[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"
[Files]
Source: "..\dist\VideoStoryRenderer\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README_RU.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\timing_template.csv"; DestDir: "{app}"; Flags: ignoreversion
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\VideoStoryRenderer.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\VideoStoryRenderer.exe"; Tasks: desktopicon
[Run]
Filename: "{app}\VideoStoryRenderer.exe"; Description: "Launch Video Story Renderer"; Flags: nowait postinstall skipifsilent
