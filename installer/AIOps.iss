#define AppName "AIOps Platform"
#define AppVersion "1.0.0"
#define AppPublisher "AIOps"
#define AppExe "AIOpsServer.exe"

[Setup]
AppId={{E62A1CBE-BD5B-4D22-8E1B-0C69DE6EF3AD}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AIOps Platform
DefaultGroupName={#AppName}
OutputDir=..\dist\installer
OutputBaseFilename=AIOps-Setup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "service"; Description: "注册为 Windows 服务（开机自启）"; Flags: checkedonce

[Files]
Source: "..\dist\AIOps-Windows\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Run]
Filename: "{app}\AIOpsService.exe"; Parameters: "install"; Tasks: service; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "config AIOpsPlatform start= auto"; Tasks: service; Flags: runhidden waituntilterminated
Filename: "sc.exe"; Parameters: "start AIOpsPlatform"; Tasks: service; Flags: runhidden waituntilterminated
Filename: "{app}\deploy\start.bat"; Tasks: not service; Flags: runhidden nowait

[UninstallRun]
Filename: "sc.exe"; Parameters: "stop AIOpsPlatform"; Flags: runhidden waituntilterminated
Filename: "{app}\AIOpsService.exe"; Parameters: "remove"; Flags: runhidden waituntilterminated
