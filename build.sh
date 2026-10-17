#!/usr/bin/env bash
# EfficientAssetRipper — Linux/macOS build pipeline (mirrors build.bat).
#
# Produces dist/EfficientAssetRipper/ as a PyInstaller --onedir bundle with
# data/, fonts/, blender/ and (when the .NET SDK is available) a
# self-contained CUE4ParseCLI next to the executable.
#
# Usage:
#   ./build.sh            # build everything it can
#   NO_PAUSE=1 CI=1 ./build.sh   # unattended (no prompts either way)

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DIST_DIR="$PROJECT_DIR/dist/EfficientAssetRipper"
BUILD_TEMP="${TMPDIR:-/tmp}/EAR_build"


step() { echo; echo "$1"; }

step "===================================================="
step " EfficientAssetRipper — Build Pipeline (Linux)"
step "===================================================="

# ── Python selection ─────────────────────────────────────────────────────────
PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "ERROR: Python not found. Install Python 3.11+ from https://python.org"
    exit 1
fi
"$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
    echo "ERROR: Python 3.11+ required, found $($PY --version 2>&1)"
    exit 1
}

# Prefer the project venv when present so deps don't leak into the system.
if [ -f "$PROJECT_DIR/venv/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/venv/bin/activate"
    PY=python
fi

# ── Step 0: dependencies ─────────────────────────────────────────────────────
step "[0/5] Checking dependencies..."
if ! "$PY" -m pip show pyinstaller >/dev/null 2>&1; then
    echo "     Installing build/test dependencies from requirements-dev.txt..."
    "$PY" -m pip install -r "$PROJECT_DIR/requirements-dev.txt"
fi
if ! "$PY" -m pip show pillow >/dev/null 2>&1; then
    echo "     Installing runtime dependencies from requirements.txt..."
    "$PY" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

# ── Step 0b: pre-build tests (same gate as build.bat) ────────────────────────
step "[0b/5] Running pre-build tests..."
cd "$PROJECT_DIR"
"$PY" -m pytest -m "not slow and not requires_blender and not requires_everything and not requires_dotnet_cli" --maxfail=1 --tb=short -rfE

# ── Step 1: PyInstaller build ────────────────────────────────────────────────
step "[1/5] Building Python application with PyInstaller..."
rm -rf "$BUILD_TEMP"
mkdir -p "$BUILD_TEMP"

cd "$PROJECT_DIR"
"$PY" -m PyInstaller --noconfirm --clean --onedir --windowed \
    --name EfficientAssetRipper \
    --distpath "$BUILD_TEMP/dist" \
    --workpath "$BUILD_TEMP/work" \
    --specpath "$BUILD_TEMP" \
    --hidden-import PySide6.QtWidgets \
    --hidden-import PySide6.QtCore \
    --hidden-import PySide6.QtGui \
    --hidden-import PySide6.QtMultimedia \
    --hidden-import PIL \
    main.py

# ── Step 2: copy to dist/ (preserving runtime dirs) ──────────────────────────
step "[2/5] Copying build output to dist/..."
RUNTIME_BACKUP="$BUILD_TEMP/runtime_backup"
rm -rf "$RUNTIME_BACKUP"
for d in profiles cache outputs logs; do
    if [ -d "$DIST_DIR/$d" ]; then
        mkdir -p "$RUNTIME_BACKUP/$d"
        cp -a "$DIST_DIR/$d/." "$RUNTIME_BACKUP/$d/"
    fi
done

rm -rf "$DIST_DIR"
mkdir -p "$DIST_DIR"
cp -a "$BUILD_TEMP/dist/EfficientAssetRipper/." "$DIST_DIR/"

if [ -d "$RUNTIME_BACKUP" ]; then
    cp -a "$RUNTIME_BACKUP/." "$DIST_DIR/"
fi

for d in data fonts blender; do
    if [ -d "$PROJECT_DIR/$d" ]; then
        mkdir -p "$DIST_DIR/$d"
        cp -a "$PROJECT_DIR/$d/." "$DIST_DIR/$d/"
    fi
done

# ── Step 3: runtime dirs ─────────────────────────────────────────────────────
step "[3/5] Creating runtime directories..."
mkdir -p "$DIST_DIR/profiles" "$DIST_DIR/cache" "$DIST_DIR/outputs" "$DIST_DIR/logs"

# ── Step 4: CUE4ParseCLI (optional locally, REQUIRED in CI) ──────────────────
if ! command -v dotnet >/dev/null 2>&1 && { [ -x "$HOME/.dotnet/dotnet" ] || [ -f "$PROJECT_DIR/venv/bin/activate" ]; }; then
    [ -x "$HOME/.dotnet/dotnet" ] && export PATH="$HOME/.dotnet:$PATH"
fi

if ! command -v dotnet >/dev/null 2>&1 || ! dotnet --list-sdks 2>/dev/null | grep -q "^10\."; then
    if [ -n "${CI:-}" ]; then
        echo "[4/5] FAIL — .NET 10.0 SDK not found in CI build."
        exit 1
    fi
    echo "[4/5] SKIP — .NET 10.0 SDK not found (CUE4ParseCLI not rebuilt)"
    echo "     Existing bin/publish may still be bundled; install .NET to rebuild."
    if [ -d "$PROJECT_DIR/cue4parse_cli/bin/publish" ]; then
        mkdir -p "$DIST_DIR/cue4parse_cli/bin/publish"
        cp -a "$PROJECT_DIR/cue4parse_cli/bin/publish/." "$DIST_DIR/cue4parse_cli/bin/publish/"
    else
        touch "$DIST_DIR/CUE4PARSE_CLI_MISSING.txt"
    fi
else
    step "[4/5] Building CUE4ParseCLI..."
    # Publish into temp first (architecture rule: avoid in-place publish),
    # then copy the single-file binary into both the in-tree publish dir
    # and the distribution bundle.
    CLI_PUB="$BUILD_TEMP/CUE4ParseCLI_publish"
    CLI_OBJ="$BUILD_TEMP/CUE4ParseCLI_obj"
    CLI_BIN="$BUILD_TEMP/CUE4ParseCLI_bin"
    rm -rf "$PROJECT_DIR/cue4parse_cli/obj" "$PROJECT_DIR/cue4parse_cli/bin/Release" \
           "$PROJECT_DIR/cue4parse_cli/bin/Debug" "$CLI_PUB" "$CLI_OBJ" "$CLI_BIN"

    case "$(uname -m)" in
        aarch64|arm64) RID="linux-arm64" ;;
        *)             RID="linux-x64" ;;
    esac

    dotnet publish "$PROJECT_DIR/cue4parse_cli/CUE4ParseCLI.csproj" -c Release -r "$RID" \
        --self-contained true -p:PublishSingleFile=true \
        -p:IncludeNativeLibrariesForSelfExtract=true \
        -p:GenerateAssemblyInfo=false -p:GenerateTargetFrameworkAttribute=false \
        -o "$CLI_PUB" -p:BaseIntermediateOutputPath="$CLI_OBJ/" -p:BaseOutputPath="$CLI_BIN/"

    mkdir -p "$PROJECT_DIR/cue4parse_cli/bin/publish" "$DIST_DIR/cue4parse_cli/bin/publish"
    cp "$CLI_PUB/CUE4ParseCLI" "$PROJECT_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
    chmod +x "$PROJECT_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
    cp "$CLI_PUB/CUE4ParseCLI" "$DIST_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
    chmod +x "$DIST_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
fi

# ── Step 5: archive ──────────────────────────────────────────────────────────
step "[5/5] Creating distribution archive..."
cd "$PROJECT_DIR/dist"
ARCHIVE=""
if command -v zip >/dev/null 2>&1; then
    zip -r -q "EfficientAssetRipper-linux-x64.zip" "EfficientAssetRipper"
    ARCHIVE="EfficientAssetRipper-linux-x64.zip"
else
    tar -czf "EfficientAssetRipper-linux-x64.tar.gz" "EfficientAssetRipper"
    ARCHIVE="EfficientAssetRipper-linux-x64.tar.gz"
fi

step "===================================================="
step " Build complete!"
step "===================================================="
echo
echo " APP:    $DIST_DIR/EfficientAssetRipper"
echo " ARCHIVE: $PROJECT_DIR/dist/$ARCHIVE"
echo
