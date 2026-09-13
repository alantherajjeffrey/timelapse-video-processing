; Timelapse Video Processing 0.8 — per-user NSIS installer.
; Adapted from history/04_2026-09_codex_v0.3/packaging/installer.nsi.
; Differences from 0.3: no WebView2 runtime step (native PySide6 app has no
; embedded browser); multi-section uninstaller with a "keep my settings"
; component instead of the single unconditional-delete uninstall section;
; detects and offers to remove a prior "Etaluma Codex" (0.3) install.
;
; Build normally invokes this via packaging\build.ps1, which passes no
; overrides (DIST_DIR and OUTFILE take their real defaults below). To
; smoke-compile this script without a frozen app present (e.g. while the
; application is still being written by other agents), pass stub values:
;   makensis /V2 "/DDIST_DIR=<folder containing a placeholder Timelapse Video Processing.exe>" ^
;                "/DOUTFILE=<scratch path>\Setup-smoketest.exe" installer.nsi
; This compiles the real script end to end (registry writes, sections,
; uninstall logic) against fake payload files, without touching dist\ or the
; version folder root.

Unicode True

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "FileFunc.nsh"
!include "x64.nsh"
!include "WinVer.nsh"

; --------------------------------------------------------------------------
; Product identity (fixed; see docs/plans/v0.4_plan.md decision 19 and 5.5)
; --------------------------------------------------------------------------
!define PRODUCT_NAME "Timelapse Video Processing"
!define PRODUCT_VERSION "1.0.0"
!define PRODUCT_PUBLISHER "BIOMIS Team, SATIE laboratory, ENS Paris-Saclay"
!define PRODUCT_COPYRIGHT "Copyright (c) 2026 the Timelapse Video Processing contributors"
!define PRODUCT_URL "https://github.com/alantherajjeffrey/timelapse-video-processing"
!define PRODUCT_EXE "Timelapse Video Processing.exe"
!define INSTALL_REGKEY "Software\Timelapse Video Processing"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TimelapseVideoProcessing"
; Registry key written by the 0.3 "Etaluma Codex" installer
; (history/04_2026-09_codex_v0.3/packaging/installer.nsi:73), used here only
; to offer removing the old install; never written or modified by this script.
!define OLD_PRODUCT_REGKEY "Software\Etaluma Codex"
; the same app under its name before 0.8
!define PREV_UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\EtalumaVideoProcessing"

!ifndef DIST_DIR
  !define DIST_DIR "..\dist\Timelapse Video Processing"
!endif
!ifndef OUTFILE
  !define OUTFILE "..\dist\Timelapse Video Processing Setup 1.0.exe"
!endif

Name "${PRODUCT_NAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Timelapse Video Processing"
InstallDirRegKey HKCU "${INSTALL_REGKEY}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "${PRODUCT_NAME}"
VIAddVersionKey "FileDescription" "${PRODUCT_NAME} installer"
VIAddVersionKey "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey "CompanyName" "${PRODUCT_PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PRODUCT_COPYRIGHT}"

!define MUI_ICON "..\source\etaluma_video\assets\icon.ico"
!define MUI_UNICON "..\source\etaluma_video\assets\icon.ico"
!define MUI_ABORTWARNING

; --------------------------------------------------------------------------
; Pages
; --------------------------------------------------------------------------
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\${PRODUCT_EXE}"
!define MUI_FINISHPAGE_RUN_NOTCHECKED
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!define MUI_COMPONENTSPAGE_NODESC
; The default MUI wording ("choose which features to uninstall") reads
; backwards for a single "keep my settings" checkbox — spell out what
; ticking it actually does.
!define MUI_COMPONENTSPAGE_TEXT_TOP "Tick the box below to keep your settings, profiles, preview cache and logs. Leave it unticked (default) to remove them along with the program."
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; --------------------------------------------------------------------------
; Install-time init: Windows 10+ x64 gate
; --------------------------------------------------------------------------
Function .onInit
  ; /SD IDOK matters here: an un-/SD MessageBox still shows (and blocks) even
  ; during a silent (/S) install, so a silent run on an unsupported OS must
  ; be able to auto-answer and then Abort rather than hang.
  ${IfNot} ${AtLeastWin10}
    MessageBox MB_ICONSTOP "${PRODUCT_NAME} requires Windows 10 or Windows 11." /SD IDOK
    Abort
  ${EndIf}
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP "${PRODUCT_NAME} requires 64-bit Windows 10 or Windows 11." /SD IDOK
    Abort
  ${EndIf}
FunctionEnd

; --------------------------------------------------------------------------
; Install section (the only component; required)
; --------------------------------------------------------------------------
Section "${PRODUCT_NAME} (required)" SEC_APP
  SectionIn RO
  SetShellVarContext current

  ; Offer to remove an earlier "Etaluma Codex" (0.3) per-user install. This
  ; only runs its uninstaller; it never touches Codex's own install
  ; directory contents directly and never touches analysis_output anywhere.
  ReadRegStr $0 HKCU "${OLD_PRODUCT_REGKEY}" "InstallDir"
  ${If} $0 != ""
  ${AndIf} ${FileExists} "$0\Uninstall.exe"
    MessageBox MB_YESNO|MB_ICONQUESTION "An earlier version (Etaluma Codex) is installed at:$\r$\n$0$\r$\n$\r$\nRemove it before installing ${PRODUCT_NAME}?" /SD IDNO IDNO skip_old_uninstall
    DetailPrint "Removing previous Etaluma Codex installation..."
    ; _?= makes ExecWait block until the (self-copying) NSIS uninstaller
    ; truly finishes instead of returning immediately.
    ExecWait '"$0\Uninstall.exe" /S _?=$0' $1
    DetailPrint "Etaluma Codex uninstaller exit code: $1"
    skip_old_uninstall:
  ${EndIf}

  ; Until 0.8 the app was called "Etaluma Video Processing". Remove that install,
  ; keeping its settings: the app copies them over on its first start.
  ReadRegStr $2 HKCU "${PREV_UNINST_KEY}" "InstallLocation"
  ${If} $2 != ""
  ${AndIf} ${FileExists} "$2\Uninstall.exe"
    DetailPrint "Removing the earlier Etaluma Video Processing installation (settings kept)..."
    ExecWait '"$2\Uninstall.exe" /S /KEEPSETTINGS=1 _?=$2' $3
    DetailPrint "Earlier uninstaller exit code: $3"
    Delete "$2\Uninstall.exe"
    RMDir "$2"
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "${DIST_DIR}\*.*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${PRODUCT_NAME}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}"
  CreateShortcut "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk" "$INSTDIR\Uninstall.exe"
  CreateShortcut "$DESKTOP\${PRODUCT_NAME}.lnk" "$INSTDIR\${PRODUCT_EXE}"

  WriteRegStr HKCU "${INSTALL_REGKEY}" "InstallDir" "$INSTDIR"

  ${GetSize} "$INSTDIR" "/S=0K" $2 $3 $4
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${PRODUCT_NAME}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${PRODUCT_VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "${PRODUCT_PUBLISHER}"
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "${PRODUCT_URL}"
  WriteRegStr HKCU "${UNINST_KEY}" "HelpLink" "${PRODUCT_URL}/issues"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\${PRODUCT_EXE}"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$2"
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

; --------------------------------------------------------------------------
; Uninstall sections, in the order they appear on the components page:
;   1. "un.Program files"              required — removes $INSTDIR, its
;                                       shortcuts and the registry keys.
;   2. "un.Keep my settings..."        optional, unchecked by default.
;   3. "-un.RemoveUserData" (hidden)   always runs last; removes
;                                       %LOCALAPPDATA%\Timelapse Video Processing
;                                       unless SEC_UN_KEEP was selected.
; It is declared third, after SEC_UN_KEEP, purely because NSIS resolves
; section-index symbols (${SEC_UN_KEEP}) in file order, not by a whole-file
; pre-scan — putting the reference after the declaration keeps the page in
; the natural required-then-optional order instead of reordering it.
; No section here ever touches an analysis_output folder: only $INSTDIR and
; $LOCALAPPDATA\Timelapse Video Processing are ever removed.
; --------------------------------------------------------------------------
Section "un.Program files" SEC_UN_APP
  SectionIn RO
  SetShellVarContext current

  Delete "$DESKTOP\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\${PRODUCT_NAME}.lnk"
  Delete "$SMPROGRAMS\${PRODUCT_NAME}\Uninstall ${PRODUCT_NAME}.lnk"
  RMDir "$SMPROGRAMS\${PRODUCT_NAME}"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${PRODUCT_EXE}"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "${INSTALL_REGKEY}"
SectionEnd

Section /o "un.Keep my settings, profiles and logs" SEC_UN_KEEP
  ; Intentionally empty: selecting this component only sets its flag, which
  ; "-un.RemoveUserData" below reads. Nothing to do here directly.
SectionEnd

Section "-un.RemoveUserData" SEC_UN_DATA
  SetShellVarContext current
  ${IfNot} ${SectionIsSelected} ${SEC_UN_KEEP}
  ${AndIf} $LOCALAPPDATA != ""
    DetailPrint "Removing settings, profiles, preview cache and logs..."
    RMDir /r "$LOCALAPPDATA\Timelapse Video Processing"
  ${Else}
    DetailPrint "Keeping settings, profiles, preview cache and logs in $LOCALAPPDATA\Timelapse Video Processing"
  ${EndIf}
SectionEnd

Function un.onInit
  SetShellVarContext current
  ; Silent uninstall: /KEEPSETTINGS=1 pre-selects the "keep" component so a
  ; silent run (/S), which never shows the components page, still keeps
  ; user data when asked to. Default (no flag, or /KEEPSETTINGS=0) removes it.
  ${un.GetParameters} $R0
  ${un.GetOptions} $R0 "/KEEPSETTINGS=" $R1
  ${If} $R1 == "1"
    !insertmacro SelectSection ${SEC_UN_KEEP}
  ${EndIf}
FunctionEnd
