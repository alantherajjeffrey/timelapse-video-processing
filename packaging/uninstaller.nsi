; Timelapse Video Processing 0.8 — standalone uninstaller.
;
; Builds "Uninstall Timelapse Video Processing.exe", a small NSIS program kept
; at the version folder root (per docs/plans/v0.4_plan.md decision 19) so a
; user who lost the Start Menu/Desktop shortcuts can still uninstall. It does
; not install anything itself: it looks up the real installation via the
; HKCU uninstall registry key that installer.nsi writes, runs the installed
; "$INSTDIR\Uninstall.exe" (built into every install by installer.nsi) and
; then cleans up what that inner uninstaller cannot remove of itself.
;
; Command-line flags:
;   /S                 Silent. No dialogs are shown. Default behaviour is to
;                       remove settings/profiles/logs (same as answering
;                       "Yes" to the interactive prompt below), unless
;                       /KEEPSETTINGS=1 is also given.
;   /KEEPSETTINGS=1     Keep %LOCALAPPDATA%\Timelapse Video Processing
;                       (settings.json, profiles\, preview_cache\, logs\).
;                       Honoured both silently and interactively (it
;                       pre-answers the prompt instead of showing it).
;   /KEEPSETTINGS=0     Explicit "remove" — same as the default, spelled out.
;
; Never touches any analysis_output folder anywhere: only the installed
; program directory and %LOCALAPPDATA%\Timelapse Video Processing are removed.
;
; Exit codes: 0 = not installed, or successfully uninstalled.
;             1 = the installed uninstaller returned a non-zero exit code.
;
; This script has no pages: all work happens in .onInit, which then calls
; Quit before any window would otherwise appear (aside from the MessageBox
; calls below, which run when not silent). Build normally invokes this via
; packaging\build.ps1 with no overrides. To smoke-compile without a real
; install present, just run makensis on it directly — it never touches the
; app's dist\ output, only the registry and %LOCALAPPDATA% at *run* time
; (compiling never executes it).

Unicode True

!include "LogicLib.nsh"
!include "FileFunc.nsh"

!define PRODUCT_NAME "Timelapse Video Processing"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\TimelapseVideoProcessing"
!define DATA_DIR_NAME "Timelapse Video Processing"

!ifndef OUTFILE
  !define OUTFILE "..\dist\Uninstall Timelapse Video Processing.exe"
!endif

Name "${PRODUCT_NAME} Uninstaller"
OutFile "${OUTFILE}"
RequestExecutionLevel user
Icon "..\source\etaluma_video\assets\icon.ico"
ShowInstDetails nevershow

VIProductVersion "1.0.0.0"
VIAddVersionKey "ProductName" "${PRODUCT_NAME} Uninstaller"
VIAddVersionKey "FileDescription" "Standalone uninstaller for ${PRODUCT_NAME}"
VIAddVersionKey "FileVersion" "1.0.0"
VIAddVersionKey "CompanyName" "BIOMIS Team, SATIE laboratory, ENS Paris-Saclay"
VIAddVersionKey "LegalCopyright" "Copyright (c) 2026 the Timelapse Video Processing contributors"

Function .onInit
  SetShellVarContext current

  ; $0 = UninstallString (already double-quoted, as written by installer.nsi)
  ; $1 = InstallLocation (plain, unquoted path)
  ReadRegStr $0 HKCU "${UNINST_KEY}" "UninstallString"
  ReadRegStr $1 HKCU "${UNINST_KEY}" "InstallLocation"
  ${If} $0 == ""
  ${OrIf} $1 == ""
    ; An un-/SD MessageBox still shows (and blocks) during a silent (/S)
    ; run, so this must be skipped outright when silent, not just defaulted.
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK "${PRODUCT_NAME} is not installed for this user." /SD IDOK
    ${EndIf}
    SetErrorLevel 0
    Quit
  ${EndIf}

  ${IfNot} ${FileExists} "$1\Uninstall.exe"
    ; Stale registry key: the record survived but the installed uninstaller
    ; did not (manually deleted folder, interrupted previous uninstall...).
    ; Clean up rather than surface a confusing ExecWait failure.
    DeleteRegKey HKCU "${UNINST_KEY}"
    RMDir /r "$1"
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK "${PRODUCT_NAME}'s installation record was damaged (its uninstaller was missing). The leftover registry entry and folder have been cleaned up." /SD IDOK
    ${EndIf}
    SetErrorLevel 0
    Quit
  ${EndIf}

  ${GetParameters} $R0
  ${GetOptions} $R0 "/KEEPSETTINGS=" $R1

  StrCpy $2 "1" ; 1 = remove settings/profiles/logs (default), 0 = keep
  ${If} $R1 == "1"
    StrCpy $2 "0"
  ${ElseIf} ${Silent}
    StrCpy $2 "1"
  ${Else}
    MessageBox MB_YESNO|MB_ICONQUESTION "Also remove settings, profiles, preview cache and logs for ${PRODUCT_NAME}?$\r$\n$\r$\nChoose No to keep them in $LOCALAPPDATA\${DATA_DIR_NAME}." /SD IDYES IDNO ask_keep
    Goto ask_done
    ask_keep:
    StrCpy $2 "0"
    ask_done:
  ${EndIf}

  ; Run the installed uninstaller silently and wait for it. _?= makes
  ; ExecWait block (an NSIS uninstaller normally self-copies to %TEMP% and
  ; returns immediately) and is intentionally unquoted per NSIS convention.
  ${If} $2 == "0"
    ExecWait '$0 /S /KEEPSETTINGS=1 _?=$1' $3
  ${Else}
    ExecWait '$0 /S _?=$1' $3
  ${EndIf}

  ; With _?= the inner uninstaller cannot delete itself or $INSTDIR; finish
  ; that here.
  Delete "$1\Uninstall.exe"
  RMDir "$1"

  ${If} $2 == "1"
  ${AndIf} $LOCALAPPDATA != ""
    RMDir /r "$LOCALAPPDATA\${DATA_DIR_NAME}"
  ${EndIf}

  ${If} $3 == 0
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK "${PRODUCT_NAME} was uninstalled."
    ${EndIf}
    SetErrorLevel 0
  ${Else}
    ${IfNot} ${Silent}
      MessageBox MB_ICONEXCLAMATION|MB_OK "${PRODUCT_NAME}'s uninstaller returned exit code $3."
    ${EndIf}
    SetErrorLevel 1
  ${EndIf}
  Quit
FunctionEnd

; Required by NSIS (at least one section must exist) but unreachable: every
; path through .onInit above ends in Quit before the section phase begins.
Section "Placeholder" SEC_PLACEHOLDER
  SectionIn RO
  SetOutPath "$TEMP"
SectionEnd
