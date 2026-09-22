#define AppVersion GetEnv('VERSION')
#if AppVersion == ''
  #define AppVersion '1.0.0'
#endif
[Setup]
AppId={{A8919F6A-EE2B-4AF8-9A17-03751BC2D6EF}
AppName=PVEClient
AppVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\PVEClient
DefaultGroupName=PVEClient
OutputDir=..\..\dist-windows
OutputBaseFilename=PVEClient-Windows-x64-{#AppVersion}-Setup
ArchitecturesInstallIn64BitMode=x64
PrivilegesRequired=lowest
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
[Files]
Source: "..\..\dist\PVE远程管理客户端\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\PVEClient"; Filename: "{app}\PVE远程管理客户端.exe"
Name: "{autodesktop}\PVEClient"; Filename: "{app}\PVE远程管理客户端.exe"; Tasks: desktopicon
[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; Flags: unchecked
[Run]
Filename: "{app}\PVE远程管理客户端.exe"; Description: "启动 PVEClient"; Flags: postinstall nowait skipifsilent
