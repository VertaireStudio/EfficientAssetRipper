@echo off
setlocal enabledelayedexpansion

echo ====================================================
echo  EfficientAssetRipper — Build Pipeline
echo ====================================================
echo.

set "PROJECT_DIR=%~dp0"
set "DIST_DIR=%PROJECT_DIR%dist\EfficientAssetRipper"

:: BUILD_TEMP must live on the same drive as PROJECT_DIR so PyInstaller's
:: makespec (which calls os.path.relpath) doesn't raise ValueError when the
:: spec dir and the source main.py are on different drives. This bites on
:: GitHub Actions runners where the workspace is on D:\ but %TEMP% is on C:\.
set "PROJECT_DRIVE=%PROJECT_DIR:~0,2%"
set "TEMP_DRIVE=%TEMP:~0,2%"
if /I "%PROJECT_DRIVE%"=="%TEMP_DRIVE%" (
    set "BUILD_TEMP=%TEMP%\EAR_build"
) else (
    set "BUILD_TEMP=%PROJECT_DIR%build\EAR_build"
)

:: Skip interactive `pause` calls when running unattended (CI, scripts).
:: Set the CI env var (GitHub Actions does this automatically) or
:: NO_PAUSE=1 to suppress them.
set "PAUSE_CMD=pause"
if defined CI set "PAUSE_CMD=rem"
if defined NO_PAUSE set "PAUSE_CMD=rem"

:: ── Check Python ─────────────────────────────────────────────────────────────
where py >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo ERROR: Python not found. Install from https://python.org
    %PAUSE_CMD%
    exit /b 1
)

:: ── Check pip packages ───────────────────────────────────────────────────────
echo [0/5] Checking dependencies...
py -m pip show pyinstaller >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo      Installing build/test dependencies from requirements-dev.txt...
    py -m pip install -r requirements-dev.txt
)
py -m pip show pillow >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo      Installing runtime dependencies from requirements.txt...
    py -m pip install -r requirements.txt
)
echo.

:: ── Step 0b: Run pre-build tests ─────────────────────────────────────────────
echo [0b/5] Running pre-build tests...
py -m pip show pytest >nul 2>nul
if %ERRORLEVEL% neq 0 (
    echo      Installing test dependencies...
    py -m pip install -r requirements-dev.txt
)
:: Drop -q so CI logs name the failing test instead of printing a wall of
:: dots followed by "ERROR: Pre-build tests failed."
py -m pytest -m "not slow and not requires_blender and not requires_everything and not requires_dotnet_cli" --maxfail=1 --tb=short -rfE
if %ERRORLEVEL% neq 0 (
    echo.
    echo ERROR: Pre-build tests failed. Aborting build.
    %PAUSE_CMD%
    exit /b 1
)
echo.

:: ── Step 1: Build Python EXE ─────────────────────────────────────────────────
echo [1/5] Building Python application with PyInstaller...
echo.

cd /d "%PROJECT_DIR%"

:: Generate VERSIONINFO from _version.py so the EXE file properties match the UI.
set "VERSION_INFO=%BUILD_TEMP%\version_info.txt"
if not exist "%BUILD_TEMP%" mkdir "%BUILD_TEMP%"
py "%PROJECT_DIR%tools\make_version_info.py" "%VERSION_INFO%"
if %ERRORLEVEL% neq 0 (
    if defined CI (
        echo ERROR: Failed to generate version_info.txt in CI build.
        exit /b 1
    )
    echo WARNING: Failed to generate version_info.txt — building without it.
    set "VERSION_ARG="
) else (
    set "VERSION_ARG=--version-file %VERSION_INFO%"
)

set "ICON_PATH=%PROJECT_DIR%assets\icon.ico"
if exist "%ICON_PATH%" (
    set "ICON_ARG=--icon "%ICON_PATH%""
) else (
    set "ICON_ARG="
)

py -m PyInstaller --noconfirm --clean --onedir --windowed --name EfficientAssetRipper --distpath "%BUILD_TEMP%\dist" --workpath "%BUILD_TEMP%\work" --specpath "%BUILD_TEMP%" %VERSION_ARG% %ICON_ARG% --hidden-import PySide6.QtWidgets --hidden-import PySide6.QtCore --hidden-import PySide6.QtGui --hidden-import PySide6.QtMultimedia --hidden-import PIL main.py

if %ERRORLEVEL% neq 0 (
    echo ERROR: PyInstaller build failed.
    %PAUSE_CMD%
    exit /b 1
)
echo.

:: ── Step 2: Copy output to dist/ ─────────────────────────────────────────────
echo [2/5] Copying build output to dist\...

:: Preserve runtime directories across rebuilds so profiles/cache/logs survive.
set "RUNTIME_BACKUP=%BUILD_TEMP%\runtime_backup"
if exist "%RUNTIME_BACKUP%" rmdir /s /q "%RUNTIME_BACKUP%"
for %%D in (profiles cache outputs logs) do (
    if exist "%DIST_DIR%\%%D" (
        xcopy /Y /E /I /Q "%DIST_DIR%\%%D" "%RUNTIME_BACKUP%\%%D" >nul
    )
)

if exist "%DIST_DIR%" rmdir /s /q "%DIST_DIR%"
xcopy /Y /E /I /Q "%BUILD_TEMP%\dist\EfficientAssetRipper" "%DIST_DIR%" >nul

:: Restore preserved runtime directories.
if exist "%RUNTIME_BACKUP%" (
    xcopy /Y /E /I /Q "%RUNTIME_BACKUP%" "%DIST_DIR%" >nul
    rmdir /s /q "%RUNTIME_BACKUP%"
)

:: Copy data files alongside the exe (not inside _internal)
xcopy /Y /E /I /Q "%PROJECT_DIR%data" "%DIST_DIR%\data" >nul
xcopy /Y /E /I /Q "%PROJECT_DIR%fonts" "%DIST_DIR%\fonts" >nul
xcopy /Y /E /I /Q "%PROJECT_DIR%blender" "%DIST_DIR%\blender" >nul
echo.

:: ── Step 3: Ensure runtime directories exist ──────────────────────────────────
echo [3/5] Creating runtime directories...
if not exist "%DIST_DIR%\profiles" mkdir "%DIST_DIR%\profiles"
if not exist "%DIST_DIR%\cache" mkdir "%DIST_DIR%\cache"
if not exist "%DIST_DIR%\outputs" mkdir "%DIST_DIR%\outputs"
if not exist "%DIST_DIR%\logs" mkdir "%DIST_DIR%\logs"
echo.

:: ── Step 4: Build CUE4Parse CLI (optional locally, REQUIRED in CI) ───────────
where dotnet >nul 2>nul
if %ERRORLEVEL% neq 0 (
    if defined CI (
        echo [4/5] FAIL — .NET SDK not found in CI build.
        echo      CI builds must ship CUE4ParseCLI; install .NET 10.0 SDK.
        exit /b 1
    )
    echo [4/5] SKIP — .NET SDK not found ^(CUE4ParseCLI not built^)
    echo      Install .NET 10.0 SDK to include CUE4ParseCLI in the build.
    echo      Marker file written to %DIST_DIR%\CUE4PARSE_CLI_MISSING.txt
    echo.
    > "%DIST_DIR%\CUE4PARSE_CLI_MISSING.txt" echo CUE4ParseCLI was not bundled with this local build ^(no .NET SDK on PATH^).
    goto :CREATE_ZIP
)

echo [4/5] Building CUE4ParseCLI...
set "CLI_PUB=%TEMP%\CUE4ParseCLI_publish"
set "CLI_OBJ=%TEMP%\CUE4ParseCLI_obj"
set "CLI_BIN=%TEMP%\CUE4ParseCLI_bin"

:: Wipe stale build artifacts before publishing. MSBuild reads any pre-existing
:: AssemblyInfo.cs / TargetFrameworkAttribute files under the in-tree obj\
:: regardless of BaseIntermediateOutputPath, producing CS0579 duplicate-
:: attribute errors. Same risk for bin\Release if a prior IDE build left it
:: behind. bin\publish\ is preserved — it's the fallback for failed rebuilds.
if exist "%PROJECT_DIR%cue4parse_cli\obj" rmdir /s /q "%PROJECT_DIR%cue4parse_cli\obj"
if exist "%PROJECT_DIR%cue4parse_cli\bin\Release" rmdir /s /q "%PROJECT_DIR%cue4parse_cli\bin\Release"
if exist "%PROJECT_DIR%cue4parse_cli\bin\Debug" rmdir /s /q "%PROJECT_DIR%cue4parse_cli\bin\Debug"
if exist "%CLI_PUB%" rmdir /s /q "%CLI_PUB%"
if exist "%CLI_OBJ%" rmdir /s /q "%CLI_OBJ%"
if exist "%CLI_BIN%" rmdir /s /q "%CLI_BIN%"

:: Double trailing backslashes so MSBuild's command-line parser doesn't
:: treat the closing \" as an escaped quote (which would swallow the next
:: -p: arg). The doubled \\ collapses to a single \ inside the property.
dotnet publish "%PROJECT_DIR%cue4parse_cli\CUE4ParseCLI.csproj" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:GenerateAssemblyInfo=false -p:GenerateTargetFrameworkAttribute=false -o "%CLI_PUB%" -p:BaseIntermediateOutputPath="%CLI_OBJ%\\" -p:BaseOutputPath="%CLI_BIN%\\"
if %ERRORLEVEL% neq 0 (
    echo WARNING: CUE4ParseCLI rebuild failed.
    if exist "%PROJECT_DIR%cue4parse_cli\bin\publish\CUE4ParseCLI.exe" (
        :: Self-contained single-file publish produces a CUE4ParseCLI.exe
        :: bigger than ~30 MB; a non-self-contained one is well under 1 MB
        :: and depends on a system .NET install we may not have.  Refuse
        :: that fallback rather than silently shipping a broken bundle.
        for %%S in ("%PROJECT_DIR%cue4parse_cli\bin\publish\CUE4ParseCLI.exe") do set "FALLBACK_SIZE=%%~zS"
        if !FALLBACK_SIZE! LSS 5000000 (
            echo          Fallback CUE4ParseCLI.exe is suspiciously small ^(!FALLBACK_SIZE! bytes^).
            echo          Refusing to bundle a non-self-contained CLI.
            if defined CI exit /b 1
        ) else (
            echo          Falling back to the in-tree publish at cue4parse_cli\bin\publish\.
            if not exist "%DIST_DIR%\cue4parse_cli\bin\publish" mkdir "%DIST_DIR%\cue4parse_cli\bin\publish"
            xcopy /Y /E /Q "%PROJECT_DIR%cue4parse_cli\bin\publish\*" "%DIST_DIR%\cue4parse_cli\bin\publish\" >nul
        )
    ) else (
        echo          No fallback CLI available — bundle will not include CUE4ParseCLI.
        if defined CI exit /b 1
        > "%DIST_DIR%\CUE4PARSE_CLI_MISSING.txt" echo CUE4ParseCLI rebuild failed and no fallback was available.
    )
    echo.
    goto :CREATE_ZIP
)

if not exist "%DIST_DIR%\cue4parse_cli\bin\publish" mkdir "%DIST_DIR%\cue4parse_cli\bin\publish"
xcopy /Y /E /Q "%CLI_PUB%\*" "%DIST_DIR%\cue4parse_cli\bin\publish\" >nul
echo.

:: ── Step 5: Create ZIP ────────────────────────────────────────────────────────
:CREATE_ZIP
echo [5/5] Creating distribution ZIP...
cd /d "%PROJECT_DIR%dist"
if exist "EfficientAssetRipper-win-x64.zip" del "EfficientAssetRipper-win-x64.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'EfficientAssetRipper' -DestinationPath 'EfficientAssetRipper-win-x64.zip' -Force"
echo.

:: ── Done ─────────────────────────────────────────────────────────────────────
echo ====================================================
echo  Build complete!
echo ====================================================
echo.
echo  EXE:  %DIST_DIR%\EfficientAssetRipper.exe
echo  ZIP:  %PROJECT_DIR%dist\EfficientAssetRipper-win-x64.zip
echo.
%PAUSE_CMD%
