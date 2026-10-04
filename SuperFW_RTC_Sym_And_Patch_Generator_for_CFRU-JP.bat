@echo off
setlocal

if "%~1"=="" goto usage
if not "%~2"=="" goto usage

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0SuperFW_RTC_Sym_And_Patch_Generator_for_CFRU-JP.ps1" -RomPath "%~1"
set "exitCode=%ERRORLEVEL%"
goto finish

:usage
echo Usage: Drag and drop exactly one .gba file onto this batch file.
set "exitCode=2"

:finish
echo.
pause
exit /b %exitCode%
