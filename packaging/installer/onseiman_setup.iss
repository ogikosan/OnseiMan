; OnseiMan インストーラー（Inno Setup 6）。作り方: python packaging\installer\build_installer.py
; - 中身は Windows 用の zip と同じ（build_installer.py が build_dist\installer_stage に並べる）
; - 管理者の権限なしで、ユーザーのフォルダ（はじめは %LOCALAPPDATA%\Programs\OnseiMan）に入れる
; - 入れるのはアプリだけ。このパソコンを「声を文字にする役」にするのは、アプリの設定画面から（T3 Code のように切り替えるだけ）
; - 設定・辞書・履歴（config.json・dict.json・logs・server_data）は、アンインストールしても残す

#define AppName "OnseiMan"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\build_dist\installer_stage\OnseiMan"
#endif

[Setup]
AppId={{3F8A2D61-9B47-4C0E-8E52-7D1A6C4B9E20}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=OnseiMan
AppPublisherURL=https://github.com/ogikosan/OnseiMan
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir=..\..\build_dist\installer
OutputBaseFilename=OnseiMan-Setup-{#AppVersion}
SetupIconFile=onseiman.ico
UninstallDisplayIcon={app}\onseiman.ico
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=no
RestartIfNeededByRun=no
; Inno Setup の守り（RedirectionGuard）は、準備で動かす子の手順にも引き継がれ、uv が Python の近道（ジャンクション）を
; 作れずに失敗する（os error 448。Inno Setup 7 で直された動き）。入れる場所はユーザーのフォルダなので切る
RedirectionGuard=no

[Languages]
Name: "japanese"; MessagesFile: "compiler:Languages\Japanese.isl"

[Tasks]
Name: "desktopicon"; Description: "デスクトップにショートカットを作る"; GroupDescription: "その他:"; Flags: unchecked

[Files]
Source: "{#StageDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "onseiman.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\OnseiMan"; Filename: "{app}\start_client.bat"; WorkingDir: "{app}"; IconFilename: "{app}\onseiman.ico"; Flags: runminimized
Name: "{group}\OnseiMan を新しい版にする"; Filename: "{app}\update.bat"; WorkingDir: "{app}"; IconFilename: "{app}\onseiman.ico"
Name: "{group}\OnseiMan をアンインストール"; Filename: "{uninstallexe}"
Name: "{userdesktop}\OnseiMan"; Filename: "{app}\start_client.bat"; WorkingDir: "{app}"; IconFilename: "{app}\onseiman.ico"; Flags: runminimized; Tasks: desktopicon

[Run]
Filename: "{app}\start_client.bat"; WorkingDir: "{app}"; Description: "OnseiMan を起動する"; Flags: nowait postinstall skipifsilent runminimized; Check: PrepareOk

[UninstallRun]
Filename: "powershell.exe"; Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\tools\uninstall_helper.ps1"" -Root ""{app}"""; Flags: runhidden waituntilterminated; RunOnceId: "OnseiManCleanup"

[UninstallDelete]
; 作り直せる大きなもの（専用の Python・部品・聞き取りの部品・控え）は消す
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\.venv_server"
Type: filesandordirs; Name: "{app}\tools\python"
Type: filesandordirs; Name: "{app}\tools\bin"
Type: filesandordirs; Name: "{app}\tools\speech"
Type: filesandordirs; Name: "{app}\models"
Type: filesandordirs; Name: "{app}\backup"
Type: filesandordirs; Name: "{app}\browser_profile"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\client\__pycache__"
Type: filesandordirs; Name: "{app}\shared\__pycache__"
Type: filesandordirs; Name: "{app}\server\__pycache__"

[Code]
var
  PrepareFailed: Boolean;

function PrepareOk(): Boolean;
begin
  Result := not PrepareFailed;
end;

// ファイルを置いたあとに、部品を用意する（進み具合は別の窓に出る）
procedure CurStepChanged(CurStep: TSetupStep);
var
  Args: String;
  Code: Integer;
begin
  if CurStep <> ssPostInstall then
    exit;
  Args := '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\tools\installer_run.ps1') + '"';
  if WizardSilent() then
    Args := Args + ' -NoPause';
  WizardForm.StatusLabel.Caption := '部品を用意しています（別の窓に進み具合が出ます。数分かかります）...';
  if not Exec('powershell.exe', Args, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, Code) or (Code <> 0) then
  begin
    PrepareFailed := True;
    SuppressibleMsgBox('部品の用意がうまくいきませんでした。' + #13#10 + 'くわしい記録: ' + ExpandConstant('{app}\logs\install.log') + #13#10 +
      'インターネットにつながっているか確かめて、もう一度インストーラーを実行してください（OnseiMan を起動したときにも、もう一度用意します）。', mbError, MB_OK, IDOK);
  end;
end;
