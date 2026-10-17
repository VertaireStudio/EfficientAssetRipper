# Contributing to EfficientAssetRipper

Thanks for your interest! Bug reports, feature ideas, and PRs are all welcome.

## Dev environment

```bash
git clone https://github.com/exterminathan/EfficientAssetRipper.git
cd EfficientAssetRipper

# Windows
py -m venv venv
.\venv\Scripts\Activate.ps1

# Linux/macOS
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt -r requirements-dev.txt
```

Then run the app:

```bash
python main.py
```

Headless test runs (CI-ish environments without a display):

```bash
QT_QPA_PLATFORM=offscreen python -m pytest
```

## Code map

The architectural shape of the repo (Python GUI / orchestration in `gui/` +
`core/`, .NET CLI in `cue4parse_cli/`, Blender subprocess scripts in
`blender/`, etc.) is documented in [docs/architecture.md](docs/architecture.md).
Read that first before making non-trivial changes — it covers the import
boundaries, the NDJSON IPC protocol, the texture-resolver heuristics, and
the gotchas that aren't obvious from a single file read.

For runtime issues, see [docs/troubleshooting.md](docs/troubleshooting.md).
For maintainer release steps, see [docs/release-checklist.md](docs/release-checklist.md).
For cooking your own UE5 fixtures (loose / pak / IoStore / AES-encrypted), see
[docs/ue5-test-builds.md](docs/ue5-test-builds.md).

## Tests

Run the fast tiers before opening a PR:

```bash
python -m pytest          # Windows: py -m pytest
```

Expect ~230 passing tests in ~10 seconds. The suite is structured as:

- `tests/unit` — pure logic, no I/O
- `tests/integration` — disk + fixtures
- `tests/qt` — PySide6 widgets via `pytest-qt`
- `tests/e2e` — opt-in, gated by `BLENDER_EXE`, `EVERYTHING_DLL`, and
  `CUE4PARSE_CLI` env vars

`build.bat` / `build.sh` run the fast tiers as step `[0b/5]` and abort on
any failure.

## Rebuilding the CUE4Parse CLI

After editing anything under `cue4parse_cli/`, you must rebuild and copy the
output back into the repo. Some checkout locations (cloud-synced folders)
hold file locks during `dotnet publish`, so build into temp first:

```powershell
dotnet publish "cue4parse_cli\CUE4ParseCLI.csproj" `
    --configuration Release `
    --runtime win-x64 `
    --self-contained true `
    -p:PublishSingleFile=true `
    -p:IncludeNativeLibrariesForSelfExtract=true `
    --output "$env:TEMP\CUE4ParseCLI_build"

Copy-Item "$env:TEMP\CUE4ParseCLI_build\*" `
    "cue4parse_cli\bin\publish" -Recurse -Force
```

Linux (or just run `./build_cli.sh`, which does all of this):

```bash
dotnet publish "cue4parse_cli/CUE4ParseCLI.csproj" \
    --configuration Release \
    --runtime linux-x64 \
    --self-contained true \
    -p:PublishSingleFile=true \
    -p:IncludeNativeLibrariesForSelfExtract=true \
    --output "/tmp/CUE4ParseCLI_build"

cp /tmp/CUE4ParseCLI_build/CUE4ParseCLI cue4parse_cli/bin/publish/
chmod +x cue4parse_cli/bin/publish/CUE4ParseCLI
```

`build.bat` / `build.sh` do the same thing as part of step `[4/5]`.

## Branch & PR conventions

- Branch off `main`. Use a short, descriptive branch name
  (e.g. `feat/material-overrides`, `fix/scan-cache-stale`).
- Commit messages are short, imperative, and capitalized — see existing
  history (`git log`) for the project's style.
- One concern per PR. Keep diffs reviewable.
- Update [CHANGELOG.md](CHANGELOG.md) under the `## [Unreleased]` section
  for any user-visible change.
- Include a screenshot or short clip for any UI change.

## Recording demo media

The README hero is `docs/demo.gif` and the screenshot is
`docs/screenshots/main.png`. Record with whichever tool you prefer —
[ScreenToGif](https://www.screentogif.com/) and
[LICEcap](https://www.cockos.com/licecap/) both work well on Windows.
Aim for ≤ 8 MB so GitHub renders it inline.

## Reporting bugs

Use the [bug report template](.github/ISSUE_TEMPLATE/bug_report.md). The
project-specific debug info that's most useful: Blender version, .NET
version, the affected profile JSON (with AES keys redacted), and the most
recent log file from `logs/`.

## Cutting a release (maintainers)

CI watches for tag pushes matching `v*.*.*`. To ship a new version:

1. Bump `__version__` in [_version.py](_version.py) and add a matching
   `## [x.y.z] - YYYY-MM-DD` block to [CHANGELOG.md](CHANGELOG.md).
2. Run `py -m pytest` locally — the version test guards against
   CHANGELOG/`_version.py` drift.
3. Commit, then tag and push:
   ```bash
   git tag v0.5.1 -m "v0.5.1 — bug fixes"
   git push origin main --tags
   ```
4. The [release workflow](.github/workflows/release.yml) builds the ZIP,
   pulls the changelog block for the tag's version, and publishes a
   GitHub Release with the artifact attached. Pre-release tags (anything
   containing a `-`, e.g. `v0.6.0-rc1`) are marked as pre-release
   automatically.
