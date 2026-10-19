# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9-VS.2] - 2026-10-19

Parallel GPU-accelerated exports with a memory budget, new export formats
(glTF / FBX / WebP / MP3 / WebM), instant cancel with memory reclaim.

### Added
- GPU texture decode — block-compressed formats (BC1/BC3/BC4/BC5/BC7 and
  ASTC LDR) decode on the graphics card through Vulkan's fixed-function
  sampler instead of handwritten compute kernels — one code path covers
  Windows and Linux. Per-texture failures fall back to the CPU decoder,
  a failed init marks the GPU unavailable for the session, and texture
  outputs stay byte-comparable with plain CPU runs. The mode is a new
  Auto/On/Off setting, the mount-time status ("enabled — <device>") is
  surfaced in the unpacker log, and the implementation is pure P/Invoke
  against the driver's Vulkan loader, so no GPU driver just means the
  CPU fallback.
- Parallel multi-threaded export — the worker count is configurable in
  Settings (Auto = one worker per CPU core); the CLI sizes a bounded
  serialize-thread pool so N workers never dead-lock the serializer,
  mounted VFS readers are forced concurrently readable before export,
  and progress emission is rate-limited so a 16-worker run doesn't
  flood stdout with per-asset lines.
- Export memory policy enforced inside the CUE4Parse CLI, two layers:
  - "Use only physical memory" — the exporter's working set never
    leaves physical RAM (swap/page file never touched). Flips the CLI
    to workstation GC (`DOTNET_gcServer=0`) on the next mount,
    relaunching the CLI when the setting changes on a running one.
  - Memory Limit (0–95 % of the RAM currently free) — the percentage is
    applied to available RAM at each export start (not total RAM), with the
    exporter's own resident footprint added back so consecutive exports
    don't shrink each other's budget. The exporter stays under the
    budget: the .NET GC itself is capped, workers back off near the
    soft threshold, at the hard wall one rate-limited worker runs a
    blocking compacting collection between batch barriers (then the
    working set is trimmed), workers are shed/added adaptively from
    live usage, and a stuck asset is skipped with a clear error after
    a bounded wait instead of hanging the run.
  - Settings shows the resolved budget ("Exporter budget ≈ 6.0 GB of
    12.1 GB RAM currently free") using dependency-free physical RAM
    detection that quietly hides the hint where the platform can't be
    probed.
- Three new mesh output formats in Settings → psk / glb / fbx: glTF
  2.0 binary (`.glb`) is produced directly by the CLI; FBX is a
  two-step path — the CLI exports PSKs (it has no FBX writer) and a
  new headless-Blender worker converts each newly-written PSK through
  the same Blender runner the .blend pipeline uses (sequential,
  cancel-terminates the live Blender subtree, tracked on shutdown).
  FBX output keeps the armature unstripped so rigs and skin weights
  survive, skips material wiring for geometry-only batches, and
  exports atomically. glTF/FBX files preview by opening the system
  default app rather than the PSK-only built-in viewer.
- WebP texture export (png / tga / webp) with a lossless
  (pixel-exact) mode and a lossy mode with a configurable quality
  slider; the built-in TGA/image previewer accepts `.webp`.
- MP3 and WebM audio export targets — ffmpeg is auto-downloaded
  (hash-pinned, cached once per session cache dir), extracted
  zip-slip-checked on Windows and via the system `tar` `.tar.xz` on
  Linux, and the produced binary is marked executable on Linux.
- Large payloads stream straight to disk instead of buffering whole in
  managed memory — WWise `.wem` dumps and video exports (direct leaves
  and resolved `FileMediaSource` paths alike) write through a streaming
  copy, so multi-GB cutscenes never exist whole inside the CLI's heap.
- CUE4ParseCLI publish-readying: ReadyToRun precompile (every GUI
  session spawns a fresh CLI before mounting, so startup matters),
  tiered PGO, server GC sized for the parallel workers, and invariant
  globalization to skip the runtime ICU initializer.

### Changed
- WWise and FMOD audio exports keep their own configured worker count
  instead of clobbering the global serialize-thread cap (they can run
  concurrently with a queued export/scan).
- Video entries no longer burn a progress slot when the per-entry
  memory-gate timeout skips them — the count and the summary now agree.

### Fixed
- Cancellation is immediate, not "between batches": the cancel token is
  wired into the parallel export loop, so the scheduler stops
  dispatching the moment cancel lands instead of draining the
  remaining 64-asset batch one early-return at a time (in-flight
  assets still finish — up to a couple of seconds). The final
  `export_done` (cancelled + remaining) is guaranteed even when a
  mid-batch cancel aborts the loop, so the panel never hangs in
  "Cancelling...".
- Progress memory is reclaimed right after every export, not left for
  whenever the idle process next GCs: a one-shot large-object-heap
  compaction plus a double compacting collection returns decoded pixel
  buffers and mesh LOD arrays, then the working set is trimmed so the
  OS sees the pages freed. A cancelled run discards its in-progress
  decode work with it.
- The GUI discards the progress bar on a cancelled run instead of
  leaving it parked at the last partial value, which would read as
  real progress; a replaced cancellation source is disposed rather
  than left to finalize later.
- Log viewer "Copy to clipboard" is bounded to the in-memory log cap,
  so a huge session cannot build a runaway clipboard string when the
  visible log has already capped its entries.
- The Blender worker saves/exports atomically — output goes to a
  `.tmp` file that is replaced on success, and a zero-length output
  aborts instead of "succeeding" with a corrupt file.

## [0.9-VS.1] - 2026-10-18

Linux platform support — the full pipeline runs on Linux x64 — plus
FMOD audio, better tool detection, and honest-er exports.

### Added
- Linux platform support. The app, its Qt UI, and the full
  Unpack → Scan → Resolve → Process pipeline now run on Linux x64:
  - Blender detection covers PATH, `/usr/bin`, `/opt`, Flatpak, Snap,
    and every Steam library (`steamapps/common/Blender/blender`), with
    the classic Windows install paths still honoured.
  - `build.sh` mirrors `build.bat`: pre-build test gate, PyInstaller
    onedir bundle, runtime-dir preservation, self-contained
    single-file CUE4ParseCLI publish, and zip/tar.gz packaging.
  - FMOD audio support — `.bank` files (packed or loose on disk) are
    scanned after mount; every stream appears as a named child row under
    its bank for double-click preview and WAV export via vgmstream.
    Selecting a bank or a folder above it exports all of its streams.

### Changed
- Everything SDK dependency is Windows-only: Linux scans use the
  built-in file walker (the setup wizard now reports this as expected,
  not as a dependency failure).
- CUE4ParseCLI ships/tech-updates a platform-correct toolchain: Oodle
  downloads `liboodle-data-shared.so` via the hash-pinned Linux zip and
  searches `Binaries/Linux` for game-shipped Oodle; vgmstream downloads
  the statically linked `vgmstream-linux-cli.zip` r1980 and marks the
  extracted binary executable. Windows artifacts unchanged.
- `docs/troubleshooting.md` and README document the Linux layout
  (QSettings INI, walker fallback, Steam Blender detection).

### Fixed
- Classifier and texture-resolver path prefixing now treat backslash
  and forward-slash separators equivalently on all platforms, so cached
  Windows-style scan data classifies correctly after a platform switch.
- Texture decoding crashed on Linux with hundreds of
  `SkiaSharp.SKImageInfo` type-initializer failures during bulk exports:
  the published single-file CLI now bundles its native libraries with
  `IncludeNativeLibrariesForSelfExtract=true` (all three build scripts),
  and pack init probes Skia once at mount so a broken native surfaces
  immediately with its inner-exception detail instead of mid-export.
- BC7/BC6H/ETC textures every-decoded as
  `Detex decompression failed: not initialized` on Linux: the installed
  CUE4Parse-Conversion routes those formats through a Windows-only
  native Detex library that no host ever initializes. The CLI now
  enables `TextureDecoder.UseAssetRipperTextureDecoder`, matching what
  CUE4Parse itself uses on non-Windows hosts (verified: Islands of
  Insight TC_BC7 textures export as valid PNGs; 133/141 sampled
  textures decode). The remaining failure class — 8 leftover
  `ASophia/Characters/.../old/` virtual textures — ships zlib-corrupt
  tile payloads (verified against two CUE4Parse builds; dead content
  the game no longer reads).
- Cancellation is no longer reported as success. `export_done` (and the
  WWise/FMOD scan results) carry `cancelled` and `remaining`, the GUI
  logs "Export cancelled: N succeeded, M failed, K not attempted" rather
  than "Export complete", skips queued audio-export chaining when you
  stop a run, and notes "Export aborted — CLI exited before its export
  finished" if the CLI dies mid-export.
- Per-object export failures (e.g. a Texture2D that cannot decode) are
  counted: export summaries now read "N successes, M failures (K asset
  warnings)" instead of burying warning-level failures under
  "0 failed".
- `init` accepts AES keys both as `{"guid", "key"}` objects and as bare
  hex strings; an unrecognized shape is skipped with a warning instead
  of crashing the whole init.

## [0.9.0] - 2026-08-02

UE 5.6–5.9 support, dependency-following export, honest error messages.

### Added
- UE 5.6–5.9 support. CUE4ParseCLI now targets .NET 10 and bundles
  CUE4Parse 1.2.2.202607; UE 5.7 raised the package `LegacyFileVersion`
  to -9, which every earlier build rejected outright.
- Dependency-following export — exporting a mesh now also exports its
  referenced materials, their parent chains, and all referenced
  textures, so a single-mesh export is immediately processable.
- Unexpanded package files in the Unpacker tree carry a + badge so
  double-click expandability is visible before the first click.

### Fixed
- A UE version CUE4Parse doesn't define no longer falls back to 5.4 in
  silence — the CLI warns and names the newest supported version.
- Auto-detect no longer clamps UE5 minors to 5.4; the ceiling is read
  from the `EGame` enum instead of a hardcoded constant.
- `"Can't load legacy UE3 file"` and the missing-mappings parser errors
  are rewritten into messages that name the actual cause and fix.
- A mappings path that is missing or unparseable warns at mount instead
  of being skipped silently; the browser and preview surface the CLI's
  real error instead of guessing "unsupported format" / "no data".
- Combobox and header sort arrows were invisible; they are now tinted
  SVGs that follow the active color scheme.
- Type-scan caches are keyed to the bundled parser generation, so a
  CUE4Parse upgrade that remaps the mount tree can't serve stale paths.

## [0.8.8] - 2026-05-07

Dockable workspace, profile-aware texture resolution, unified media
preview, and a slimmer setup wizard.

### Added
- Dockable workspace — every panel is a `QDockWidget` with persistent
  layout state and a Window menu (toggle docks, Reset Layout).
- Auto AES prompt — when CUE4Parse reports unmounted archives, the
  prompt opens with affected names and GUIDs.
- Unified Media Preview tab plays audio and video with one auto-
  switching player and side-by-side per-kind history; replaces the
  Audio Preview tab.
- Video export — `FileMediaSource` resolves its embedded `FilePath`
  to a VFS entry; raw `.bk2/.mp4/.webm/.mov` extract byte-for-byte.
- Per-profile texture preset, material overrides dialog, and a
  keyword auto-detect fallback when suffix/param matching leaves
  slots empty.
- `regex_suffixes` in `texture_presets.json`; ships Obduction-style
  `_DIFF/_NRM/_MASK/_METALLIC_MASK` patterns.
- Tree right-click Expand/Collapse and Alt+click recursive expansion
  across the asset trees.

### Changed
- AES key editing lives only in Manage Profiles; the Unpacker AES
  table is gone.
- Setup wizard drops the Game Folder and Output Directory pages
  (per-game settings live in profiles); replaced by a single Profile
  page with "Open Profile Manager…".
- Help menu sits to the right of Window in the menu bar.
- Bink (`.bk2`) and other Qt-undecodable formats fall back to an
  Open Folder shortcut so the asset is still recoverable.
- Legal language refreshed across README, About dialog, and first-run
  wizard. No license change.

### Fixed
- Stale `presets_path` heals to the bundled file when the stored
  path is missing — fixes Verify Setup reporting a dead Google Drive
  path after the repo move.
- Texture resolver no longer pads Obduction master materials with
  unrelated keyword textures when the main pass resolved any slot.
- Unpacker hand-off only feeds Send-to-Queue files written this
  session, not stale PSKs from previous runs.
- Categorization fallback — case-insensitive `game_folder`, UE
  `Content/Game` marker, on-disk top-level folder name as last
  resort; reason surfaced in the asset detail dialog.

## [0.8.7] - 2026-05-06

Unpacker performance and ergonomics: type-cache-backed asset filtering,
visual type indicators, and dropdown polish.

### Added
- Type cache + heuristic scan — asset-type filter resolves without
  re-walking the archive, large trees stay responsive.
- Visual type indicators on Unpacker rows (mesh / texture / audio / etc.).

### Changed
- Headless test suite — Qt tests run with no visible windows.

### Fixed
- Unpacker filter dropdowns no longer get stuck or mismatch their state.

## [0.8.6] - 2026-05-05

Mesh preview tab plus type-aware right-click previews across the Unpacker
and PSK Picker.

### Added
- 3D Mesh Preview tab — full 360° orbit, see-through wireframe, fast load.
- Right-click "Preview Mesh / Texture / Audio / Properties" in the Unpacker;
  unexpanded `.uasset` rows offer all kinds, expanded rows show only the
  preview buttons matching their contained types.
- Right-click "Preview Mesh" + "Open containing folder" in the PSK Picker.
- Auto-detect UE version from the game executable's `FileVersionInfo`.

### Changed
- More descriptive preview-failure status — "This asset has no <kind> data
  to preview" instead of "file exported but file not found".
- Optional dependencies cleanup and minor UI polish.

### Fixed
- First mesh preview no longer fails with a stale "file not found" — the
  resolver rescans the temp dir for both flat and nested CLI layouts.
- Mesh previewer: UV-grid crash and wireframe leak between loads.
- Build script clears stale .NET artifacts before publishing CUE4ParseCLI.

## [0.8.5] - 2026-05-05

Resilience and UX pass on top of v0.8.0. Full notes:
[docs/architecture.md](docs/architecture.md),
[docs/troubleshooting.md](docs/troubleshooting.md).

### Added
- Queue checkpoint + startup resume prompt for interrupted batches.
- Crash reporter writes redacted JSON under `logs/` with a one-click
  "Open GitHub issue" path.
- Color Scheme dialog: live preview toggle and Reset to Default.
- Settings dialog: Restore Defaults button.
- Synthetic end-to-end pipeline test in CI (no external binaries).

### Changed
- Defensive profile loading — corrupt JSON is quarantined to
  `<name>.json.corrupt-<ts>` and the GUI keeps running.
- "Set the game folder in Settings" prompts now point at Manage Profiles.
- Architecture / troubleshooting / release notes moved into `docs/`.

### Fixed
- Theme-aware alert banner and swatch borders pick contrast from the
  active scheme instead of hardcoded values.
- Long asset paths in the detail dialog wrap and are selectable.
- Several minor cleanups: dead `_terminate_then_kill` removed, duplicate
  `QGroupBox` import dropped, hot-loop status-color lookup hoisted.

## [0.8.0] - 2026-05-04

Pre-1.0 hardening release. Sweeping security, reliability, and UX pass across
the GUI, Blender pipeline, CUE4ParseCLI, and the CI/release chain.

### Added
- **Manage Profiles dialog** — per-profile game/output/unpack paths, AES keys,
  mappings, and UE version live in a dedicated dropdown + editor instead of the
  global Settings panel.
- **Custom color scheme editor** with live swatches alongside the four built-in
  themes.
- **Release verification** — every ZIP ships `SHA256SUMS` and a signed
  build-provenance attestation; release notes embed `sha256sum --check` and
  `gh attestation verify` snippets.
- **Setup wizard** reachable from the Help menu (previously first-run only).
- **CHANGELOG enforcement** — release workflow refuses to publish a tag without
  a matching changelog block.
- **CUE4ParseCLI `--help` / `-h` / `/?`** usage banner.
- **Per-run nonce protocol** — Blender stdout is only trusted on
  `##ASSET_STATUS:<nonce>##` lines so unrelated output can't masquerade as
  status. Mirrored in the blend combiner.
- **Test coverage** grew from ~250 to ~470 tests across unit / integration / Qt
  tiers; new e2e smoke suite for the real CUE4ParseCLI binary under
  [tests/e2e/](tests/e2e/).

### Changed
- **Profile bar** collapsed to a dropdown plus a single Manage Profiles button.
  Unpacker-tab edits no longer leak back into the saved profile unless
  `auto_save_paths` is set.
- **PathPicker** widget hoisted into [gui/widgets.py](gui/widgets.py) for
  consistent path field UX.
- **Filter rebuilds** in PSK picker, asset browser, and text viewer are
  debounced — 40k-asset folders stay responsive while typing.
- **Audio and image previewers** decode and `rmtree` on `QThreadPool` with
  token-based invalidation so stale loads can't overwrite the active selection.
- **Texture classifier** uses longest-suffix-wins with `priority_order` from
  the preset JSON breaking ties — `_OR` can no longer shadow `_ORM`.
- **Material parent chain** capped at depth 32; case-mixed cycles terminate.
- **Closest-path tiebreaker** deterministic (path-len, then lower-cased
  string), insulating picks from candidate-list shuffles.
- **Scan cache** version mismatches archived to `scan_*.json.bak.<ts>`; backups
  older than 30 days pruned on launch.
- **Processed-state auto-detect** symmetric — deleting a `.blend` flips state
  back to False on the next load.
- **JobManager** terminates the live Blender subprocess on cancel and emits
  `asset_updated(idx, state_dict)` for GUI-thread state application instead of
  cross-thread mutation.
- **MainWindow** tracks worker threads, drains them on close, and writes scan
  caches off-thread.
- **`build.bat`** runs the fast test tiers as step `[0b/5]` and aborts on
  failure; CI builds fail if the CLI cannot be built or `version_info.txt`
  generation errors out.
- **`build_cli.bat`** uses `--self-contained true` + `PublishSingleFile` so
  dev rebuilds match the shipped artifact.
- **`sync.ps1`** restore is non-destructive; backup additive unless `-Force`.
- **Dependabot** collapsed to a single weekly grouped PR for GitHub Actions
  updates.
- **CI actions** pinned to full SHA + version; default permissions
  `contents: read`, write scopes opted into per-job.
- **`PY_PYTHON=3.11`** pinned in CI so the `py` launcher doesn't pick the
  runner's 3.14 (no PySide6 wheels). PySide6 upper bound bumped to `<7.0`.

### Fixed
- Alert banner colors and swatch borders now follow the active scheme.
- Long asset paths wrap and are selectable instead of clipping.
- Blender pipe-drain failures are logged instead of swallowed.
- Settings dialog validates path-typed fields before save, clamps the timeout,
  and opens presets via `QDesktopServices`.
- `Theme.apply` skips no-op reapplies and only registers fonts once per process.
- Splash always closes even if the finish callback throws.
- Detail dialogs use `WA_DeleteOnClose`; menu actions gain `&`-mnemonics and
  standard shortcut keys.
- PSK `MATT` chunk parser bound-checks dims, returns `(names, ok)` so corrupt
  PSKs surface as `scan_failed`, and falls back utf-8 / cp1252 / ascii on
  material name decode.
- [blender/material_setup.py](blender/material_setup.py) uses named mix-node
  sockets and a `BSDF_RENAMES` table for Blender 4.x socket names
  (Subsurface / Specular / Transmission / Sheen / Clearcoat).
- [blender/process_asset.py](blender/process_asset.py) treats `CANCELLED` and
  "no mesh produced" as hard failures; `.blend` save is atomic (tmp file →
  `os.replace`) with a non-zero size check.
- [blender/combine_blends.py](blender/combine_blends.py) refuses to save when
  nothing was appended.
- [core/props_parser.py](core/props_parser.py) reads `utf-8-sig`, parses RGBA
  in any order, logs JSON errors with lineno/msg.
- `build.bat` parens inside `if (...)` blocks escaped — fixes the
  `was unexpected at this time.` abort at step `[3/5]`.
- `test_validate_re_runs_when_mtime_changes` flake — explicit `os.utime` bump
  instead of relying on two consecutive `write_bytes` calls landing in distinct
  NTFS mtime ticks. Was the sole cause of the recent CI test-gate failures.

### Security
- **Path traversal** — `ProfileManager` CRUD routes through `_safe_path`,
  blocking `..\`, reserved Windows device names, and inputs that resolve
  outside `profiles/`. CUE4ParseCLI gains `SafeJoin()` at every write site
  (mesh / texture / anim / props / Wwise audio).
- **DLL planting** — `Everything64.dll` loader requires an absolute path and
  passes `LOAD_LIBRARY_SEARCH_SYSTEM32`; bare filenames rejected.
- **Secret redaction** — new [core/log_redaction.py](core/log_redaction.py)
  masks `key` / `aes` / `password` / `token` / `secret` fields and inline hex
  blobs ≥ 32 chars across the root logger, the Blender cmd/stderr surface, and
  job log entries.
- **AES error messages** no longer echo raw bytes.
- **Update check** — GitHub responses capped at 64 KB, `tag_name`
  shape-validated, off-host redirects refused, non-`github.com` release URLs
  stripped before render.
- **NDJSON DoS** — CUE4ParseCLI child killed if its stdout buffer exceeds
  16 MB without a newline.
- **Blender identity probe** — `--version` checked once per session; refuses
  to spawn the render subprocess if the banner isn't Blender. Manifest
  allowlist (psk extension, absolute output path, `.blend` suffix,
  outputs-root containment, image-extension texture paths) enforced both
  Python-side and Blender-side.
- **Supply chain** — Oodle and vgmstream downloads pinned to tag + SHA-256;
  per-entry zip extraction blocks zip-slip.
- **vgmstream invocation** uses `ProcessStartInfo.ArgumentList`, eliminating
  the prior shell-quoting injection surface.
- **Newtonsoft** pinned to `TypeNameHandling.None`.
- **Stdin oversize cap** — 4 MB per-line limit on CUE4ParseCLI.
- **Release tag** must be an ancestor of `origin/main` before publish.

## [0.5.0] - 2026-04-26

Initial public beta release.

### Added
- **Asset Scanner** with [Everything SDK](https://www.voidtools.com/) for
  instant search across game directories.
- **Material Resolver** — parses `.props.txt` metadata, follows material
  inheritance chains, classifies textures by PBR slot (Base Color, Normal,
  ORM, Emissive, Roughness, Metallic, Mask).
- **Headless Blender pipeline** — imports PSK/PSKX, builds full PBR shader
  node graphs, saves `.blend` files. Reuses bundled `io_scene_psk_psa`
  extension.
- **VFS Unpacker** — built-in CUE4Parse CLI mounts `.pak`/`.utoc` archives
  with AES decryption and exports meshes, textures, animations, and audio.
- **Batch processing queue** with progress tracking, logging, and cancel.
- **Per-game profiles** for game folder, AES keys, UE version, and processed
  asset lists.
- **Texture preset system** with per-material overrides
  ([data/texture_presets.json](data/texture_presets.json)).
- **WWise audio support** — scans `AkAudioEvent` assets, exports WEM, and
  converts to WAV/OGG via vgmstream.
- **Blend Combiner** — merge multiple `.blend` files into a single grid
  scene.
- **First-run setup wizard** with auto-detection for Blender, Everything,
  and the .NET runtime.
- **Auto-update check** against the GitHub Releases API on startup
  (cached for 24 hours, fails silently when offline).
- **Themeable UI** — 4 built-in colour schemes (Dusk, Bloom, Slate,
  Midnight) plus a custom scheme editor.
- **PyInstaller `--onedir` build** producing a portable
  `EfficientAssetRipper-win-x64.zip`.
- **CI/CD** — GitHub Actions workflows for PR/main builds and tag-driven
  releases.
- **Test suite** — pytest unit / integration / Qt tiers (~8s),
  plus opt-in `requires_blender` / `requires_everything` /
  `requires_dotnet_cli` e2e markers.
- **MIT License** and legal disclaimer for asset extraction usage.

[0.9.0]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.9.0
[0.8.8]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.8.8
[0.8.7]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.8.7
[0.8.6]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.8.6
[0.8.5]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.8.5
[0.8.0]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.8.0
[0.5.0]: https://github.com/exterminathan/EfficientAssetRipper/releases/tag/v0.5.0
