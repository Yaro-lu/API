; AIWorker Setup Script for Inno Setup
#define MyAppName "AIWorker"
#define MyAppVersion "0.1.0"
#define MyAppPublisher "AI Story Studio"
#define MyAppURL "https://your-server.com"

[Setup]
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={pf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputBaseFilename=AIWorker_Setup_{#MyAppVersion}
OutputDir=..\dist
Compression=lzma
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
InternalCompressLevel=max
ShowLanguageDialog=no

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\config\*"; DestDir: "{app}\config"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\workflows\*"; DestDir: "{app}\workflows"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\models\*"; DestDir: "{app}\models"; Flags: ignoreversion recursesubdirs createallsubdirs; OnlyBelowVersion: 0,6.1
Source: "..\inputs\*"; DestDir: "{app}\inputs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\outputs\*"; DestDir: "{app}\outputs"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\VERSION"; DestDir: "{app}"

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\app\start_worker.bat"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\app\start_worker.bat"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[UninstallDelete]
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\logs"

[CustomMessages]
chinesesimplified.DeleteUserData=删除用户数据（模型、输出、配置）？
english.DeleteUserData=Delete user data (models, outputs, config)?

[Code]
var
  DeleteUserDataPage: TInputOptionWizardPage;

procedure InitializeUninstallProgressForm();
begin
  DeleteUserDataPage := CreateInputOptionPage(wpSelectTasks,
    'Confirm',
    'Additional Options',
    CustomMessage('DeleteUserData'),
    True, False);
  DeleteUserDataPage.Add('Delete user data (models, outputs, config)');
  DeleteUserDataPage.Values[0] := False;
end;

procedure DeinitializeUninstall();
begin
  if DeleteUserDataPage.Values[0] then
  begin
    DelTree(ExpandConstant('{app}\models'), True, True, True);
    DelTree(ExpandConstant('{app}\outputs'), True, True, True);
    DelTree(ExpandConstant('{app}\config'), True, True, True);
    DelTree(ExpandConstant('{app}\logs'), True, True, True);
  end;
end;
