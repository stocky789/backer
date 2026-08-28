; Backer Agent Windows Installer
; Inno Setup Script
;
; Build requirements:
;   - Inno Setup 6.x (https://jrsoftware.org/isinfo.php)
;   - backer-agent.exe (built with PyInstaller)
;   - kopia.exe (downloaded separately)
;
; Build command:
;   iscc backer-agent.iss

#define MyAppName "Backer Agent"
#define MyAppVersion "0.8.0"
#define MyAppPublisher "Backer"
#define MyAppURL "https://git.stockhome.com.au/stocky789/backer"
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
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Close running instances before installing
CloseApplications=force
CloseApplicationsFilter=backer-agent.exe,backer-agent-service.exe
RestartApplications=no
; Application icon
SetupIconFile=..\assets\backer.ico
UninstallDisplayIcon={app}\backer.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start Backer Agent when Windows starts"; GroupDescription: "Startup options:"

[Files]
; Main executable
Source: "..\dist\backer-agent.exe"; DestDir: "{app}"; Flags: ignoreversion
; Dedicated unattended runner used by the boot task, never the Tk GUI.
Source: "..\dist\backer-agent-service.exe"; DestDir: "{app}"; Flags: ignoreversion

; Application icon (for GUI window and shortcuts)
Source: "..\assets\backer.ico"; DestDir: "{app}"; Flags: ignoreversion

; Backup tool (Kopia)
Source: "..\dist\tools\kopia.exe"; DestDir: "{app}\tools"; Flags: ignoreversion skipifsourcedoesntexist

; License and readme
Source: "..\LICENSE"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\backer.ico"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\backer.ico"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
; Launch app after install
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop and remove the boot task before uninstall. Commands tolerate absent tasks/processes.
Filename: "schtasks"; Parameters: "/end /tn BackerAgentService"; Flags: runhidden; RunOnceId: "EndBackerTask"
Filename: "taskkill"; Parameters: "/F /IM backer-agent.exe"; Flags: runhidden; RunOnceId: "StopBacker"
Filename: "taskkill"; Parameters: "/F /IM backer-agent-service.exe"; Flags: runhidden; RunOnceId: "StopBackerService"
Filename: "schtasks"; Parameters: "/delete /tn BackerAgentService /f"; Flags: runhidden; RunOnceId: "DeleteBackerTask"

[Registry]
; Store install location
Root: HKLM; Subkey: "SOFTWARE\Backer"; ValueType: string; ValueName: "InstallPath"; ValueData: "{app}"; Flags: uninsdeletekey
Root: HKLM; Subkey: "SOFTWARE\Backer"; ValueType: string; ValueName: "Version"; ValueData: "{#MyAppVersion}"

[Code]
// Custom code for additional setup logic

function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  // Stop the existing task before replacing its executable; retain it for the upgrade.
  Exec('schtasks', '/end /tn BackerAgentService', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Release both PyInstaller executables before install.
  Exec('taskkill', '/F /IM backer-agent.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Exec('taskkill', '/F /IM backer-agent-service.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Wait a moment for process cleanup
  Sleep(1000);
  Result := True;
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
