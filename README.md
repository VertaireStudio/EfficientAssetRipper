# EfficientAssetRipper

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/exterminathan/EfficientAssetRipper?include_prereleases&sort=semver)](https://github.com/exterminathan/EfficientAssetRipper/releases)
[![Platform](https://img.shields.io/badge/platform-Windows%20x64-0078d4)](#download)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/)
[![Built with PySide6 + .NET 10](https://img.shields.io/badge/built%20with-PySide6%20%2B%20.NET%2010-512bd4)](#dependencies)

> EfficientAssetRipper is an asset extraction tool for Unreal Engine 4 and 5
> games that uses CUE4Parse as its core parsing library, with robust support
> for the latest UE4 and UE5 archive formats. It pairs a modern PySide6
> desktop interface with an automated Blender export pipeline and a
> comprehensive set of utility tools for previewing textures, combining
> models, and inspecting game packages.
>
> EfficientAssetRipper is actively maintained and welcomes contributions
> and feedback.

<!-- Demo GIF — record locally with ScreenToGif/LICEcap, drop in docs/demo.gif -->
![EfficientAssetRipper demo](docs/demo.gif)

**Unpack → Scan → Resolve → Process → Done.** Mount `.pak` / `.utoc` archives, point it at a game folder, and EfficientAssetRipper finds meshes, resolves their materials and textures, then batch-processes everything in Blender — importing PSK/PSKX meshes, wiring PBR shader nodes, and saving ready-to-use `.blend` files. Plus utility tools for previewing textures, combining models, and more.

---

## ⚖️ Legal

EfficientAssetRipper is licensed under the MIT License.

Please be aware that using or distributing the output from this software may
be against copyright legislation in your jurisdiction. You are responsible
for ensuring that you're not breaking any laws.

This software is not sponsored by or affiliated with Epic Games, Inc. or its
affiliates. "Unreal" and "Unreal Engine" are trademarks or registered
trademarks of Epic Games, Inc. in the United States and elsewhere.

---

## Features

- **Asset Scanner** — Finds PSK/PSKX mesh files via [Everything SDK](https://www.voidtools.com/) with instant search across game directories
- **Material Resolver** — Reads exported `.props.txt` metadata, follows material inheritance chains, and classifies textures by PBR slot (Base Color, Normal, ORM, Emissive, etc.)
- **Automatic Blender Processing** — Headless Blender subprocess imports meshes, builds full shader node graphs with proper texture wiring, and saves `.blend` files
- **VFS Unpacker** — Built-in [CUE4Parse](https://github.com/FabianFG/CUE4Parse) CLI mounts `.pak`/`.utoc` archives with AES decryption and exports meshes, textures, animations, audio, and material properties
- **Batch Queue** — Multi-asset processing with progress tracking, logging, and cancellation
- **Texture Preset System** — Configurable texture slot rules (suffixes, parameter names, wiring types) with per-material overrides
- **WWise Audio Support** — Scans AkAudioEvent assets, extracts WEM files, and converts to WAV/OGG via vgmstream
- **FMOD Audio Support** — Scans FMOD Studio `.bank` files (packed or loose on disk), lists every stream by name under the bank in the tree, and previews/exports them as WAV via vgmstream
- **Blend Combiner** — Merge multiple `.blend` files into a single scene with grid layout
- **Multi-Profile** — Per-game configuration profiles (game paths, AES keys, UE version, export directories)
- **Themeable UI** — 8 built-in color schemes + custom scheme editor with PySide6/Qt6

---

## Screenshots

![Main window](docs/screenshots/main.png)

---

## Verified Compatible With

EfficientAssetRipper has been tested end-to-end against the following UE5
titles:

- **Star Wars Jedi: Survivor**
- **Rocket League**
- **Satisfactory**

Other UE5 (and most UE4) games **should** work as well — the pipeline is
generic. If your game doesn't, please [open an issue](../../issues/new) with
the game name and a sample `.props.txt` so the resolver rules can be tuned.

---

## Download

### Pre-built Release (Windows x64)

Download the latest release from the [Releases](../../releases) page:

1. Download `EfficientAssetRipper-win-x64.zip`
2. Extract to any folder
3. Run `EfficientAssetRipper.exe`

**Requirements for the pre-built release:**

- Windows 10/11 (x64)
- [.NET 10.0 Runtime](https://dotnet.microsoft.com/download/dotnet/10.0) (for CUE4ParseCLI)
- [Blender 4.0+](https://www.blender.org/download/) (for asset processing)
- [Everything](https://www.voidtools.com/) (must be running for asset search)

---

## Installation (from source)

### Prerequisites

| Tool                                                         | Version | Purpose                                                        |
| ------------------------------------------------------------ | ------- | -------------------------------------------------------------- |
| [Python](https://python.org)                                 | 3.10+   | Application runtime                                            |
| [Blender](https://www.blender.org/download/)                 | 4.0+    | Headless mesh import & material wiring                         |
| [Everything](https://www.voidtools.com/)                     | 1.4+    | Fast file search (must be running)                             |
| [.NET SDK](https://dotnet.microsoft.com/download/dotnet/8.0) | 8.0+    | Build CUE4ParseCLI (optional — pre-built included in releases) |

### Steps

1. **Clone the repository**

   ```bash
   git clone https://github.com/exterminathan/EfficientAssetRipper.git
   cd EfficientAssetRipper
   ```

2. **Create a virtual environment**

   ```bash
   python -m venv venv
   ```

3. **Activate the virtual environment**

   Windows:

   ```powershell
   .\venv\Scripts\Activate.ps1
   ```

   Linux/macOS:

   ```bash
   source venv/bin/activate
   ```

4. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

5. **Build CUE4ParseCLI** (optional — only needed for the VFS unpacker)

   Windows:

   ```bash
   build_cli.bat
   ```

   Linux/macOS:

   ```bash
   ./build_cli.sh
   ```

6. **Run the application**
   ```bash
   python main.py
   ```

### Blender PSK Addon

EfficientAssetRipper requires a PSK import addon in Blender. The default is the built-in `io_scene_psk_psa` extension (Blender 4.0+). You can change the addon name in **Settings → Processing**.

---

## First-Time Setup

1. **Launch the app** — the splash screen will play, then the main window
   appears. The first-run wizard auto-detects Blender, Everything, and
   .NET if they're installed.
2. **Configure global tooling paths** in **Settings**:
   - **Blender** — path to `blender.exe`
   - **Everything DLL** — path to `Everything64.dll` (usually
     `C:\Program Files\Everything\Everything64.dll`)
   - **CUE4Parse CLI** — path to `CUE4ParseCLI.exe` (in
     `cue4parse_cli/bin/publish/`)
3. **Create a game profile** in **Manage Profiles**:
   - **Game folder** — path to the game's content/pak directory
   - **UE Version** — `GAME_UE5_4` is a sane default; pick the closest
     match for your title
   - **Mounted folder** — where the unpacker writes exported files
   - **Output folder** — where `.blend` files will be saved
   - **AES Keys** — paste the GUID + key for each `.pak`. Most games
     use a single GUID of all zeros. The Unpacker no longer has its
     own key editor; **Manage Profiles → AES Keys** is the only edit
     point. If you click **Mount Archives** and the CLI reports
     encrypted archives, a prompt opens automatically so you can paste
     the missing key without leaving the workspace.
4. **Scan** — click **Scan Game Folder** to discover meshes
5. **Select & Process** — check assets in the browser tree, click
   **Add to Queue**, then **Process Queue**.

### Workspace controls

- **Dockable panels** — every tab (left and right) is a draggable dock.
  Drop one onto an edge or another dock to tile or tabify, like VS
  Code's panel system. The arrangement persists between sessions.
- **Window menu** — toggle individual docks on/off; **Window → Reset
  Layout** restores the default left/right preset.
- **Tree controls** — right-click any tree row for **Expand All /
  Collapse All / Expand Selected / Collapse Selected**. **Alt+click**
  on a folder recursively expands or collapses its descendants. Typing
  in the search box auto-expands matching rows.

If something goes wrong, see
[docs/troubleshooting.md](docs/troubleshooting.md) for common
fixes — Everything not running, AES keys, queue resume, and crash
reports.

---

## Building an EXE

A build script is included to package the application as a standalone Windows executable.

### Quick Build

```bash
build.bat
```

This will:

1. Install PyInstaller if needed
2. Build the Python application into a standalone exe
3. Copy data files (blender scripts, texture presets, fonts)
4. Build CUE4ParseCLI (if .NET SDK is installed)
5. Create `dist/EfficientAssetRipper-win-x64.zip`

### Manual Build

```bash
pip install pyinstaller
pyinstaller --onedir --windowed --name EfficientAssetRipper ^
    --hidden-import PySide6.QtWidgets ^
    --hidden-import PySide6.QtCore ^
    --hidden-import PySide6.QtGui ^
    --hidden-import PySide6.QtMultimedia ^
    --hidden-import PIL ^
    main.py

:: Copy data files alongside the exe
xcopy /Y /E /I data dist\EfficientAssetRipper\data
xcopy /Y /E /I fonts dist\EfficientAssetRipper\fonts
xcopy /Y /E /I blender dist\EfficientAssetRipper\blender
```

The output will be in `dist/EfficientAssetRipper/`.

---

## Running Tests

A pytest-based test suite covers the core parsers, the asset scanner, the
CUE4ParseCLI IPC layer, and the PySide6 widgets. Tests are standalone — they
do **not** run automatically when you launch the app, but the build pipeline
gates `build.bat` on the unit + integration + Qt tiers.

### Install dev dependencies (one-time)

```bash
py -m pip install -r requirements-dev.txt
```

### Run all tests

```bash
py -m pytest
```

Expected on a fresh machine: ~230 passed, a handful skipped (the e2e tier
auto-skips when Blender / Everything / CUE4ParseCLI binaries aren't found).
Total runtime is ~10 seconds.

### Run specific tiers

```bash
py -m pytest tests/unit            # ~1s, pure logic, no I/O
py -m pytest tests/integration     # ~1s, real fixtures + disk I/O
py -m pytest tests/qt              # ~10s, PySide6 widgets via pytest-qt
```

### Run a single test file or test

```bash
py -m pytest tests/unit/core/test_classifier.py
py -m pytest tests/unit/core/test_classifier.py::test_classify_characters_path
```

### Opt-in binary smoke tests

Set the relevant environment variable, then run with the matching marker:

```bat
set BLENDER_EXE=C:\Program Files\Blender Foundation\Blender 4.0\blender.exe
py -m pytest -m requires_blender

set CUE4PARSE_CLI=cue4parse_cli\bin\publish\CUE4ParseCLI.exe
py -m pytest -m requires_dotnet_cli

set EVERYTHING_DLL=C:\Program Files\Everything\Everything64.dll
py -m pytest -m requires_everything
```

### Coverage report

```bash
py -m pytest --cov --cov-report=html
start htmlcov\index.html
```

### How tests slot into other workflows

- `build.bat` runs the fast tiers automatically as step `[0b/5]` and aborts the
  build on any failure.
- Other scripts can queue the suite via `py -m pytest <args>`. Exit code is 0
  on success, non-zero on any failure.
- `tests/unit/test_environment.py` reports required vs. optional dependencies
  (Python version, PySide6, Pillow, Blender, .NET, Everything DLL, etc.).
  Required failures abort; optional warnings tell you which `requires_*`
  smoke tests will be skipped on this machine.

---

## Project Structure

```
EfficientAssetRipper/
├── main.py                  # Application entry point
├── _base.py                 # Base directory resolver (source & frozen exe)
├── config.py                # QSettings-based configuration
├── requirements.txt         # Python dependencies
├── build.bat                # Windows exe build script
├── build_cli.bat            # CUE4ParseCLI build script (Windows)
├── build_cli.sh             # CUE4ParseCLI build script (Linux/macOS)
│
├── core/                    # Backend logic
│   ├── asset_scanner.py     # Asset discovery & material resolution
│   ├── blender_runner.py    # Headless Blender subprocess wrapper
│   ├── classifier.py        # Asset categorization by folder path
│   ├── everything.py        # Everything SDK ctypes wrapper
│   ├── job_manager.py       # Batch processing queue (QThread)
│   ├── profile_manager.py   # Per-game profile CRUD
│   ├── props_parser.py      # UE .props.txt parser (JSON + legacy text)
│   ├── texture_resolver.py  # Texture classification & file lookup
│   └── unpacker.py          # CUE4ParseCLI NDJSON IPC wrapper
│
├── gui/                     # PySide6 UI (every panel ships as a QDockWidget)
│   ├── main_window.py       # Dockable workspace orchestrator + layout persistence
│   ├── aes_prompt_dialog.py # Modal that opens when CLI reports encrypted archives
│   ├── asset_browser.py     # Hierarchical asset tree with filtering
│   ├── media_previewer.py   # Unified audio + video playback panel
│   ├── blend_combiner.py    # Multi-blend merge tool
│   ├── color_schemes.py     # Built-in + custom color scheme registry
│   ├── log_viewer.py        # Color-coded log display
│   ├── profile_dialog.py    # Manage Profiles editor (single source of truth for AES keys)
│   ├── psk_picker.py        # PSK file browser with Everything search
│   ├── queue_panel.py       # Processing queue table
│   ├── settings_panel.py    # Configuration dialog (paths, processing, appearance)
│   ├── splash.py            # Animated startup overlay
│   ├── text_viewer.py       # Read-only text/JSON viewer
│   ├── tga_previewer.py     # TGA/PNG image viewer with zoom
│   ├── theme.py             # Centralized theming (palette, QSS, fonts)
│   ├── unpacker_panel.py    # VFS browser & asset export UI (read-only mount toolbar)
│   └── widgets.py           # Reusable widgets (zoomable tree, collapsible sections)
│
├── blender/                 # Headless Blender scripts (run as subprocesses)
│   ├── process_asset.py     # Import PSK → wire materials → save .blend
│   ├── material_setup.py    # Shader node graph construction
│   └── combine_blends.py    # Merge multiple .blend files
│
├── cue4parse_cli/           # .NET 10.0 CLI tool
│   ├── Program.cs           # NDJSON IPC server for UE archive operations
│   └── CUE4ParseCLI.csproj  # Project file (CUE4Parse + Newtonsoft.Json)
│
├── data/
│   └── texture_presets.json # Texture slot rules & material overrides
│
├── fonts/                   # Custom font directory (drop .ttf/.otf files)
│
├── profiles/                # Per-game config (gitignored)
├── cache/                   # Scan result cache (gitignored)
├── outputs/                 # Generated .blend files (gitignored)
└── logs/                    # Processing logs (gitignored)
```

---

## Texture Wiring

EfficientAssetRipper uses a preset system to wire textures to Blender shader nodes. The default preset (`default_pbr`) handles standard PBR materials:

| Texture Slot | Suffixes                     | Wiring Type                                   |
| ------------ | ---------------------------- | --------------------------------------------- |
| Base Color   | `_C`, `_D`, `_Albedo`, `_CS` | Direct → Base Color                           |
| Normal       | `_N`, `_Normal`              | Normal Map node → Normal                      |
| ORM          | `_ORM`                       | Split channels: R→AO, G→Roughness, B→Metallic |
| Emissive     | `_E`, `_Emissive`            | Direct → Emission Color                       |
| Opacity/Mask | `_M`, `_Mask`, `_A`          | Direct → Alpha                                |
| Roughness    | `_R`, `_Roughness`           | Direct → Roughness                            |
| Metallic     | `_MT`, `_Metallic`           | Direct → Metallic                             |

Custom presets and per-material overrides can be defined in [data/texture_presets.json](data/texture_presets.json).

---

## CUE4ParseCLI

The built-in CLI tool communicates via NDJSON (newline-delimited JSON) over stdin/stdout. It supports:

- **Archive mounting** with AES key decryption
- **VFS browsing** of game content directories
- **Asset export**: Static/Skeletal Meshes (PSK/PSKX), Textures (PNG/TGA), Animations (PSA), Audio (WAV/OGG)
- **Material property serialization** to JSON
- **WWise audio** event scanning and WEM export with automatic naming
- **FMOD audio** bank scanning with per-stream preview and WAV export
- **Oodle decompression** (auto-downloads if needed)
- **vgmstream integration** for WEM→WAV/OGG conversion

---

## Configuration

Settings are stored in the Windows registry via QSettings (`HKCU\Software\EfficientAssetRipper`). Per-game profiles are saved as JSON files in the `profiles/` directory.

Key settings:

| Setting           | Description                                        |
| ----------------- | -------------------------------------------------- |
| `game_folder`     | Path to game content directory                     |
| `blender_exe`     | Path to Blender executable                         |
| `everything_dll`  | Path to Everything64.dll                           |
| `output_dir`      | Default output directory for .blend files          |
| `cue4parse_cli`   | Path to CUE4ParseCLI.exe                           |
| `timeout_seconds` | Blender processing timeout (default: 120s)         |
| `color_scheme`    | UI theme (Dusk, Bloom, Slate, Midnight, Ember, Moss, Lagoon, Graphite, or custom) |

---

## Dependencies

### Python

- [PySide6](https://pypi.org/project/PySide6/) ≥ 6.6.0 — Qt6 GUI framework
- [Pillow](https://pypi.org/project/Pillow/) ≥ 10.0.0 — Image loading (TGA/PNG/DDS preview)

### External Tools

- [Blender](https://www.blender.org/) 4.0+ — Headless PSK import and shader node construction
- [Everything](https://www.voidtools.com/) — Windows file search (must be running)
- [.NET 10.0 Runtime](https://dotnet.microsoft.com/download/dotnet/10.0) — Required for CUE4ParseCLI
- [vgmstream](https://vgmstream.org/) — WEM audio conversion (auto-downloaded by CUE4ParseCLI)

---

## Acknowledgments

EfficientAssetRipper stands on the shoulders of these excellent open-source
projects — please support them:

- **[CUE4Parse](https://github.com/FabianFG/CUE4Parse)** by FabianFG — the .NET
  library that does all of the heavy lifting for UE archive parsing,
  decryption, and asset export. Without it, none of the unpacker pipeline
  would exist.
- **[Everything](https://www.voidtools.com/)** by voidtools — the instant file
  index that powers the asset scanner.
- **[Blender](https://www.blender.org/)** — the headless renderer that imports
  meshes and assembles material graphs.
- **[io_scene_psk_psa](https://github.com/DarklightGames/io_scene_psk_psa)** —
  the bundled Blender extension that imports PSK/PSKX/PSA files.
- **[vgmstream](https://vgmstream.org/)** — game audio decoding for WWise WEM and FMOD banks
  conversion.
- **[Oodle](http://www.radgametools.com/oodlecompressors.htm)** by Epic Games /
  RAD Game Tools — the compression codec used by most modern UE games
  (auto-downloaded by CUE4Parse on first use).

---

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for the
dev environment setup, the project's code map, and the rules around
rebuilding the CUE4Parse CLI after editing C# code.

Bug reports and feature ideas: please use the
[issue templates](.github/ISSUE_TEMPLATE/).

---

## License

Released under the MIT License — see [LICENSE](LICENSE).

See the [Legal](#-legal) section above for the disclaimer regarding
jurisdiction, responsibility, and trademarks.
