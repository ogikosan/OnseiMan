; OnseiMan インストーラー（Inno Setup 6）
; 作り方: packaging\exe-installer\build_installer.bat を実行する（exe 化 → インストーラー作成まで行う）
; - 管理者権限なしで、ユーザーのフォルダ（%LOCALAPPDATA%\Programs\OnseiMan）に入れる
; - 音声認識のデータ（約 840MB）は入れない。初回起動時に OnseiMan が自動でダウンロードする
; - 設定・辞書・ログは %LOCALAPPDATA%\OnseiMan に作られる（アンインストールしても残る）

#define AppName "OnseiMan"
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{6B0D7C4E-3E1B-4F5A-9C1E-0A6C1E5D2B11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=OnseiMan
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\..\build_dist\installer
OutputBaseFilename=OnseiMan-Setup-{#AppVersion}
SetupIconFile=onseiman.ico
UninstallDisplayIcon={app}\OnseiMan.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "startup"; Description: "Windows の起動時に OnseiMan を自動で起動する"; GroupDescription: "その他:"
Name: "desktopicon"; Description: "デスクトップにショートカットを作る"; GroupDescription: "その他:"; Flags: unchecked

[Files]
Source: "..\..\build_dist\dist\OnseiMan\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\OnseiMan"; Filename: "{app}\OnseiMan.exe"
Name: "{group}\OnseiMan をアンインストール"; Filename: "{uninstallexe}"
Name: "{userdesktop}\OnseiMan"; Filename: "{app}\OnseiMan.exe"; Tasks: desktopicon

[Registry]
; OnseiMan の設定画面の「Windows 起動時に自動起動」と同じ場所・同じ名前
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "OnseiMan"; ValueData: """{app}\OnseiMan.exe"""; Tasks: startup; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "OnseiMan"; Flags: uninsdeletevalue dontcreatekey

[Run]
Filename: "{app}\OnseiMan.exe"; Description: "OnseiMan を起動する"; Flags: nowait postinstall skipifsilent
; アプリからの自動更新（/UPDATE=1 付きの無人インストール）では、入れ替え後に起動し直す
Filename: "{app}\OnseiMan.exe"; Flags: nowait; Check: IsUpdateMode

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/IM OnseiMan.exe /F"; Flags: runhidden; RunOnceId: "StopOnseiMan"

[Code]
function IsUpdateMode: Boolean;
begin
  Result := ExpandConstant('{param:UPDATE|0}') = '1';
end;

// 上書きインストールの前に、動いている OnseiMan（見張り役も含む）を止める
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM OnseiMan.exe /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
  Result := '';
end;
