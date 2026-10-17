@echo off
echo ============================================
echo  Building CUE4ParseCLI...
echo ============================================
echo.

where dotnet >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ERROR: .NET SDK not found. Install from https://dotnet.microsoft.com/download
    echo Requires .NET 8.0 SDK or later.
    pause
    exit /b 1
)

set "PUBLISH_DIR=%TEMP%\CUE4ParseCLI_publish"
set "OBJ_DIR=%TEMP%\CUE4ParseCLI_obj"
set "BIN_DIR=%TEMP%\CUE4ParseCLI_bin"
set "FINAL_DIR=%~dp0cue4parse_cli\bin\publish"

echo [1/2] Building and publishing Release...
echo      (intermediate files in %%TEMP%% to avoid Google Drive locks)
cd /d "%~dp0cue4parse_cli"
:: --self-contained true matches the path used by build.bat / the shipped
:: artifact, so dev rebuilds produce the same single-file exe end users get.
dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o "%PUBLISH_DIR%" -p:BaseIntermediateOutputPath=%OBJ_DIR%\ -p:BaseOutputPath=%BIN_DIR%\
if %ERRORLEVEL% neq 0 (
    echo ERROR: Build failed.
    pause
    exit /b 1
)

echo.
echo [2/2] Copying output...
if not exist "%FINAL_DIR%" mkdir "%FINAL_DIR%"
xcopy /Y /E /Q "%PUBLISH_DIR%\*" "%FINAL_DIR%\" >nul
echo.
echo Done!
echo.
echo Output: %FINAL_DIR%\CUE4ParseCLI.exe
echo.
echo To use: Set the CLI path in EfficientAssetRipper Settings.
pause
