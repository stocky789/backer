; Backer Agent Windows Installer
; Inno Setup Script
;
; Build requirements:
;   - Inno Setup 6.x (https://jrsoftware.org/isinfo.php)
;   - backer-agent.exe (built with PyInstaller)
;   - rclone.exe and restic.exe (downloaded separately)
;
; Build command:
;   iscc backer-agent.iss

#define MyAppName "Backer Agent"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "Backer"
#define MyAppURL "https://github.com/stocky789/backer"
#define MyAppExeName "backer-agent.exe"

[Setup]
; Application info
AppId={{B4CK3R-4G3NT-W1ND-0WS1-NST4LL3R}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\Backer Agent
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputDir=..\dist
OutputBaseFilename=backer-agent-setup
SetupIconFile=..\assets\backer.ico
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start Backer Agent when Windows starts"; GroupDescription: "Startup options:"

[Files]
; Main executable
Source: "..\dist\backer-agent.exe"; DestDir: "{app}"; Flags: ignoreversion

; Backup tools (rclone and restic)
Source: "..\dist\tools\rclone.exe"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\dist\tools\restic.exe"; DestDir: "{app}\tools"; Flags: ignoreversion

; License and readme
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Launch app after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the service/process before uninstall
Filename: "taskkill"; Parameters: "/F /IM backer-agent.exe"; Flags: runhidden; RunOnceId: "StopBacker"

[Registry]
; Store install location
Root: HKLM; Subkey: "SOFTWARE\Backer"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "SOFTWARE\Backer"; ValueType: string; ValueName: "Version"; ValueData: "{#MyAppVersion}"

[Code]
// Custom code for additional setup logic

function InitializeSetup(): Boolean;
begin
  Result := True;
  // Add any pre-install checks here
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Create tools directory if it doesn't exist
    ForceDirectories(ExpandConstant('{app}\tools'));

    // Create data directory
    ForceDirectories(ExpandConstant('{userappdata}\Backer'));
  end;
end;
