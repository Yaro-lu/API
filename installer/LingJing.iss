; 灵镜造片厂 — Windows 10 x64 离线安装包
; 该脚本只能读取 build_release.ps1 生成并审核过的 staging 目录。

#ifndef MyAppVersion
  #error MyAppVersion must be supplied by scripts/build_release.ps1
#endif

#ifndef StageDir
  #error StageDir must be supplied by scripts/build_release.ps1
#endif

#ifndef ReleaseOutputDir
  #error ReleaseOutputDir must be supplied by scripts/build_release.ps1
#endif

#define MyAppName "灵镜造片厂"
#define MyAppPublisher "Yaro-lu"
#define MyAppURL "https://github.com/Yaro-lu/API"
#define MyAppExe "runtime\python\pythonw.exe"

[Setup]
AppId={{8587F9B2-C36E-49A6-942C-DC321E26510E}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={localappdata}\Programs\LingJingAI
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
AllowNoIcons=yes
DisableProgramGroupPage=yes
OutputBaseFilename=LingJingAI-Setup-{#MyAppVersion}-win-x64
OutputDir={#ReleaseOutputDir}
SetupIconFile={#StageDir}\app\gui\assets\app.ico
UninstallDisplayIcon={app}\app\gui\assets\app.ico
Compression=lzma2/ultra64
SolidCompression=yes
InternalCompressLevel=ultra64
WizardStyle=modern
ShowLanguageDialog=no
RestartApplications=no
CloseApplications=no
UsePreviousAppDir=yes
UsePreviousGroup=yes
CreateUninstallRegKey=yes
Uninstallable=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
; StageDir 已由 build_release.ps1 做过白名单复制、敏感信息扫描和成员校验。
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; IconFilename: "{app}\app\gui\assets\app.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Parameters: "-s -B ""{app}\app\gui\main_gateway.py"""; WorkingDir: "{app}"; Description: "启动 {#MyAppName}"; Flags: postinstall nowait skipifsilent runascurrentuser
