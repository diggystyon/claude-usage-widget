; Inno Setup script for Claude Usage Tray.
; Build with: make_installer.bat  (or directly via ISCC.exe installer.iss)
; Output: installer_dist\Claude Usage Setup.exe

#define AppName       "Claude Usage"
; AppVersion is normally passed on the ISCC command line as
;   /DAppVersion=1.2.3
; sourced from _version.py by both rebuild_and_install.bat (local) and
; .github/workflows/build-windows.yml (CI). The fallback below only
; fires if someone invokes ISCC directly without /DAppVersion, in which
; case a clearly-fake version makes the misuse obvious.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#define AppPublisher  "diggystyon"
#define AppExeName    "Claude Usage.exe"
#define AppId         "{{C61D6A7B-9E0E-4C2C-9F2A-CLAUDEUSAGE001}"

[Setup]
AppId={#AppId}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=installer_dist
OutputBaseFilename=Claude Usage Setup
SetupIconFile=app.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel2=This will install [name/ver] on your computer.%n%nMost users also need the included browser extension so the widget can read live usage from claude.ai. The final wizard page walks you through that in about 30 seconds. You'll also need to be signed in to https://claude.ai in the browser where you install the extension -- the extension reads cookies from that browser only.%n%nIt is recommended that you close all other applications before continuing.

[Tasks]
Name: "startupicon"; Description: "Start {#AppName} when Windows starts"; GroupDescription: "Additional options:"; Flags: checkedonce
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional options:"; Flags: unchecked

[Files]
Source: "dist\{#AppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "extension\manifest.json";  DestDir: "{app}\extension"; Flags: ignoreversion
Source: "extension\background.js";  DestDir: "{app}\extension"; Flags: ignoreversion
Source: "extension\icon-16.png";    DestDir: "{app}\extension"; Flags: ignoreversion
Source: "extension\icon-48.png";    DestDir: "{app}\extension"; Flags: ignoreversion
Source: "extension\icon-128.png";   DestDir: "{app}\extension"; Flags: ignoreversion
Source: "extension\README.md";      DestDir: "{app}\extension"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Browser extension folder (for hands-off mode)"; Filename: "{app}\extension"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[Run]
; All post-install actions are launched from Pascal code in [Code] (see
; OpenExtensionFolderAndEdge / CurStepChanged) so they fire BEFORE the
; Finish page renders and we have full control over them. The Finish page
; therefore has no checkboxes, leaving more room for the instructions.

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM ""{#AppExeName}"" /F"; Flags: runhidden; RunOnceId: "killtray"

[Code]
function SetForegroundWindow(hWnd: HWND): Boolean;
  external 'SetForegroundWindow@user32.dll stdcall';

function SetWindowPos(hWnd: HWND; hWndInsertAfter: HWND; X, Y, cx, cy: Integer; uFlags: LongWord): Boolean;
  external 'SetWindowPos@user32.dll stdcall';

function ShowWindow(hWnd: HWND; nCmdShow: Integer): Boolean;
  external 'ShowWindow@user32.dll stdcall';

function FlashWindow(hWnd: HWND; bInvert: Boolean): Boolean;
  external 'FlashWindow@user32.dll stdcall';

function ExpandEnvVars(const s: String): String;
{ Best-effort expansion of common %ENV% placeholders found in REG_EXPAND_SZ
  values that Inno Setup's RegQueryStringValue returns unexpanded. We only
  care about the variables Windows uses for tray-icon ExecutablePath
  storage, which in practice is dominated by %LOCALAPPDATA% (since we
  install there). Other vars covered for safety.

  Tries three casings of each placeholder (canonical / upper / lower)
  because StringChangeEx is case-sensitive and the registry doesn't
  promise canonical casing across users' machines. }
var
  i, j: Integer;
  vars: array[0..6] of String;
  casings: array[0..2] of String;
  v: String;
begin
  Result := s;
  vars[0] := 'LOCALAPPDATA';
  vars[1] := 'APPDATA';
  vars[2] := 'USERPROFILE';
  vars[3] := 'ProgramFiles';
  vars[4] := 'ProgramFiles(x86)';
  vars[5] := 'SYSTEMROOT';
  vars[6] := 'WINDIR';
  for i := 0 to 6 do
  begin
    v := GetEnv(vars[i]);
    if v <> '' then
    begin
      casings[0] := vars[i];
      casings[1] := UpperCase(vars[i]);
      casings[2] := LowerCase(vars[i]);
      for j := 0 to 2 do
        StringChangeEx(Result, '%' + casings[j] + '%', v, True);
    end;
  end;
end;

procedure CleanupOldTrayIcons();
{ Walks HKCU:\Control Panel\NotifyIconSettings and removes ONLY entries
  whose ExecutablePath BOTH:
   (a) names a file that no longer exists on disk, AND
   (b) belongs to a Claude Usage install (basename matches {#AppExeName}).

  Filter (a) preserves the active install's pinned-state toggle on
  upgrades. Filter (b) keeps us from sweeping orphan tray entries that
  belong to unrelated apps the user has uninstalled -- a Claude Usage
  installer should clean up after Claude Usage, not the rest of the system. }
var
  rootPath: String;
  subkeys: TArrayOfString;
  i: Integer;
  ep, expanded, basename: String;
  removed: Integer;
begin
  rootPath := 'Control Panel\NotifyIconSettings';
  if not RegGetSubkeyNames(HKEY_CURRENT_USER, rootPath, subkeys) then
    Exit;
  removed := 0;
  for i := 0 to GetArrayLength(subkeys) - 1 do
  begin
    if RegQueryStringValue(HKEY_CURRENT_USER, rootPath + '\' + subkeys[i],
                           'ExecutablePath', ep) then
    begin
      if ep <> '' then
      begin
        expanded := ExpandEnvVars(ep);
        basename := ExtractFileName(expanded);
        if (CompareText(basename, '{#AppExeName}') = 0)
           and not FileExists(expanded) then
        begin
          if RegDeleteKeyIncludingSubkeys(HKEY_CURRENT_USER,
                                           rootPath + '\' + subkeys[i]) then
            removed := removed + 1;
        end;
      end;
    end;
  end;
  Log(Format('CleanupOldTrayIcons: removed %d Claude Usage orphan entries', [removed]));
end;


function FindEdgePath(): String;
var
  candidates: array[0..3] of String;
  i: Integer;
begin
  Result := '';
  candidates[0] := ExpandConstant('{commonpf32}\Microsoft\Edge\Application\msedge.exe');
  candidates[1] := ExpandConstant('{commonpf64}\Microsoft\Edge\Application\msedge.exe');
  candidates[2] := ExpandConstant('{localappdata}\Microsoft\Edge\Application\msedge.exe');
  candidates[3] := ExpandConstant('{localappdata}\Microsoft\Edge SxS\Application\msedge.exe');
  for i := 0 to 3 do
    if FileExists(candidates[i]) then
    begin
      Result := candidates[i];
      Exit;
    end;
end;

procedure ResizeExplorerWindow();
{ explorer.exe ignores window-size command-line args, so we poll for the
  Explorer window we just opened (class name "CabinetWClass") and shrink
  it via SetWindowPos. Keeps Explorer in the upper-left so the wizard
  stays visible. SWP flags: NOZORDER ($0004) | NOACTIVATE ($0010). }
var
  hwnd: Longint;
  attempts: Integer;
begin
  hwnd := 0;
  attempts := 0;
  while (hwnd = 0) and (attempts < 25) do
  begin
    Sleep(100);
    hwnd := FindWindowByClassName('CabinetWClass');
    attempts := attempts + 1;
  end;
  if hwnd <> 0 then
    SetWindowPos(hwnd, 0, 30, 50, 700, 520, $0014);
end;

procedure OpenExtensionFolderAndEdge();
var
  ResultCode: Integer;
  edgePath: String;
  appDir: String;
begin
  appDir := ExpandConstant('{app}\extension');
  // Open the extension folder in File Explorer.
  Exec(ExpandConstant('{win}\explorer.exe'), '"' + appDir + '"',
       '', SW_SHOWNORMAL, ewNoWait, ResultCode);
  // Shrink the Explorer window so it doesn't bury the wizard. Has to
  // happen *after* the Explorer window is up, so we poll briefly.
  ResizeExplorerWindow();
  // Pre-stage the URL on the clipboard so the user can just press Ctrl+L,
  // then Ctrl+V, then Enter in Edge -- no typing required.
  Exec(ExpandConstant('{cmd}'),
       '/C echo|set /p="edge://extensions/" | clip',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Open Edge in a new window. We can't reliably navigate to edge://extensions/
  // from an external launch (Edge strips privileged URLs from external command
  // lines when an instance is already running), so the user pastes the URL
  // from the clipboard.
  edgePath := FindEdgePath();
  if edgePath <> '' then
    // Force a fixed window size + position so Edge doesn't open maximized
    // and bury File Explorer / the wizard. SW_SHOWNOACTIVATE (4) tells the
    // shell not to give Edge foreground, though Chromium handles its own
    // window creation and may still steal focus briefly.
    Exec(edgePath,
         '--new-window --window-size=1000,720 --window-position=240,80 about:blank',
         '', 4, ewNoWait, ResultCode);
end;

procedure LaunchTrayWidget();
{ Just starts the widget. Split out from OpenExtensionFolderAndEdge so silent
  reinstalls (rebuild_and_install.bat) can launch the widget without firing
  the first-time Edge / Explorer / wizard-to-front setup ceremony. }
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{app}\{#AppExeName}'), '', '',
       SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

procedure BringWizardToFront();
begin
  // Edge is launched minimized so the wizard never actually loses focus,
  // but Explorer can still steal foreground briefly. We pull the wizard
  // back via the minimize/restore + topmost-flip dance, and also flash the
  // wizard's taskbar entry so the user sees it light up if focus stealing
  // wins.
  Sleep(700);
  ShowWindow(WizardForm.Handle, 6);  // SW_MINIMIZE
  Sleep(120);
  ShowWindow(WizardForm.Handle, 9);  // SW_RESTORE
  SetWindowPos(WizardForm.Handle, HWND($FFFFFFFF), 0, 0, 0, 0, $0003);
  SetWindowPos(WizardForm.Handle, HWND($FFFFFFFE), 0, 0, 0, 0, $0003);
  WizardForm.BringToFront();
  SetForegroundWindow(WizardForm.Handle);
  // Flash the taskbar entry to draw attention if focus-stealing prevented
  // us from coming to front (FlashWindow alternates each call so we toggle
  // a few times for visibility).
  FlashWindow(WizardForm.Handle, True);
  FlashWindow(WizardForm.Handle, True);
  FlashWindow(WizardForm.Handle, True);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Wipe stale tray-icon registrations from any previous Claude Usage
    // install BEFORE launching the new widget. The freshly-launched widget
    // then registers a single clean entry under "Other system tray icons".
    CleanupOldTrayIcons();
    if WizardSilent() then
    begin
      // Silent reinstall (rebuild_and_install.bat): skip the first-time
      // setup ceremony, just relaunch the widget.
      LaunchTrayWidget();
    end
    else
    begin
      // Normal interactive install: open Edge + Explorer for the user to
      // install the browser extension, then start the widget, then pull
      // the wizard back to front so the user sees the instructions.
      OpenExtensionFolderAndEdge();
      LaunchTrayWidget();
      BringWizardToFront();
    end;
  end;
end;

var
  FinishMemo: TNewMemo;

procedure CurPageChanged(CurPageID: Integer);
var
  L: TNewStaticText;
  topY, leftX, w, h: Integer;
  txt: String;
begin
  if CurPageID = wpFinished then
  begin
    txt :=
      'Microsoft Edge and File Explorer have just opened.' + #13#10 +
      'They may be behind this wizard -- use Alt+Tab or your taskbar' + #13#10 +
      'to switch between them.' + #13#10 +
      'The URL "edge://extensions/" is already on your clipboard.' + #13#10 + #13#10 +
      'To enable hands-off auto-refresh (one-time, ~30 seconds):' + #13#10 + #13#10 +
      '  1. Switch to Edge. Press Ctrl+L (focus address bar),' + #13#10 +
      '     Ctrl+V (paste URL), Enter. (Or paste edge://extensions/)' + #13#10 +
      '  2. Turn ON "Developer mode" (left sidebar).' + #13#10 +
      '     If Edge shows a warning popup, click "Not now".' + #13#10 +
      '  3. Click "Load unpacked" and pick this folder (also open in' + #13#10 +
      '     File Explorer behind Edge):' + #13#10 +
      '         ' + ExpandConstant('{app}\extension') + #13#10 +
      '  4. Turn "Developer mode" BACK OFF. The extension keeps' + #13#10 +
      '     running and the warning popup never reappears.' + #13#10 +
      '  5. REQUIRED: sign in to https://claude.ai IN EDGE.' + #13#10 +
      '     The extension reads cookies from this browser only;' + #13#10 +
      '     being signed in via Claude desktop or another browser' + #13#10 +
      '     does not count.' + #13#10 + #13#10 +
      'The tray tooltip will show "Source: Claude browser extension"' + #13#10 +
      'within a minute. If it instead says "Sign in to claude.ai in' + #13#10 +
      'your browser", repeat step 5. Click Finish when done.';

    L := WizardForm.FinishedLabel;
    leftX := L.Left;
    topY  := L.Top;
    w     := L.Width;
    h     := WizardForm.RunList.Top + WizardForm.RunList.Height - topY;
    L.Visible := False;
    if FinishMemo = nil then
    begin
      FinishMemo := TNewMemo.Create(WizardForm);
      FinishMemo.Parent := L.Parent;
      FinishMemo.SetBounds(leftX, topY, w, h);
      FinishMemo.ReadOnly := True;
      FinishMemo.WordWrap := True;
      FinishMemo.ScrollBars := ssVertical;
      FinishMemo.Color := clBtnFace;
      FinishMemo.BorderStyle := bsNone;
    end;
    FinishMemo.Visible := True;
    FinishMemo.Text := txt;
  end;
end;
