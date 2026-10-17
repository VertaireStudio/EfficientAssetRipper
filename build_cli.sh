#!/usr/bin/env bash
set -e

echo "============================================"
echo " Building CUE4ParseCLI..."
echo "============================================"
echo

if ! command -v dotnet &> /dev/null; then
    if [ -x "$HOME/.dotnet/dotnet" ]; then
        export PATH="$HOME/.dotnet:$PATH"
    else
        echo "ERROR: .NET SDK not found. Install the .NET 10.0 SDK from"
        echo "https://dotnet.microsoft.com/download/dotnet/10.0, or run:"
        echo "  curl -fsSL https://dot.net/v1/dotnet-install.sh | bash -s -- --channel 10.0"
        exit 1
    fi
fi

if ! dotnet --list-sdks 2>/dev/null | grep -q "^10\."; then
    echo "ERROR: .NET 10.0 SDK required (net10.0 is forced by the CUE4Parse build)."
    echo "Found: $(dotnet --list-sdks 2>/dev/null || echo 'none')"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/cue4parse_cli"

case "$(uname -m)" in
    aarch64|arm64) RID="linux-arm64" ;;
    *)             RID="linux-x64" ;;
esac

# Publish into temp first (architecture rule: .NET file locks can wedge
# dotnet publish when run in-place on cloud-synced folders), then copy back.
PUBLISH_TMP="$(mktemp -d /tmp/CUE4ParseCLI_build.XXXXXX)"

echo "[1/3] Restoring NuGet packages..."
dotnet restore

echo
echo "[2/3] Publishing self-contained single-file Release..."
# Self-contained mirrors build.bat so end users need no .NET runtime. The
# CLI's temp/native downloads (Oodle, vgmstream) still happen at run time.
# IncludeNativeLibrariesForSelfExtract guarantees bundled natives (libSkiaSharp,
# libblake3) are extracted to a temp dir at startup and dlopen-able — in-bundle
# direct loading silently failed in the field with a bare type-initializer error.
dotnet publish CUE4ParseCLI.csproj -c Release -r "$RID" --self-contained true \
    -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true \
    -p:GenerateAssemblyInfo=false \
    -p:GenerateTargetFrameworkAttribute=false -o "$PUBLISH_TMP"

echo
echo "[3/3] Copying back..."
mkdir -p "$SCRIPT_DIR/cue4parse_cli/bin/publish"
cp "$PUBLISH_TMP/CUE4ParseCLI" "$SCRIPT_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
chmod +x "$SCRIPT_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
rm -rf "$PUBLISH_TMP"

echo
echo "Done!"
echo
echo "Output: $SCRIPT_DIR/cue4parse_cli/bin/publish/CUE4ParseCLI"
echo
echo "To use: Set the CLI path in EfficientAssetRipper Settings."
