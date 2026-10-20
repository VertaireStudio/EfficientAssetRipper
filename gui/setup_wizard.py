"""First-run setup wizard.

Shown only when ``config.get("setup_complete") != "1"``. Walks the user
through dependency detection and a pointer at the Profiles menu (game
folder, output dir, AES keys etc. all live in profiles now). The user
can hit "Skip Setup" at any time and land in the main window with
whatever defaults are in place.

Wraps the existing ``SettingsDialog`` validators where possible — this
module is augmentation, not a re-implementation.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

import config
from _version import __version__
from core import tools_detect
from gui.settings_panel import SettingsDialog

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dependency probes (split out so tests can patch them)
# ---------------------------------------------------------------------------

def detect_blender() -> tuple[bool, str]:
    """Return (found, detail). Detail is the resolved path or a hint."""
    path = config.get("blender_exe") or tools_detect.detect_blender()
    if path and Path(path).is_file():
        return True, path
    if os.name != "nt":
        from core.platform_detect import is_nixos
        if is_nixos():
            return False, (
                "Blender not found — on NixOS add blender to "
                "environment.systemPackages (or home-manager) and Re-check"
            )
        location = "PATH"
    else:
        location = "default install locations"
    return False, f"Blender not found in {location} or common install dirs"


def detect_everything() -> tuple[bool, str]:
    """Return (found, detail). Detail is the DLL path or a hint.

    Everything SDK is a Windows app; on other platforms the app scans via
    the built-in WalkSearcher fallback, so this probe reports success there
    with a note rather than a red ✗.
    """
    if os.name != "nt":
        return True, "Windows app — built-in WalkSearcher used on this OS"
    path = config.get("everything_dll") or tools_detect.detect_everything_dll()
    if path and Path(path).is_file():
        return True, path
    return False, "Everything64.dll not found — install Everything desktop"


def detect_ffmpeg() -> tuple[bool, str]:
    """Return (found, detail). Same precedence as the CUE4Parse CLI: its
    verified cache first, then a system install. Not-found is not fatal —
    the CLI downloads its pinned build on the first audio export — but the
    row tells the user that is about to happen."""
    path = tools_detect.detect_ffmpeg()
    if path:
        return True, path
    return False, (
        "Not cached yet — CUE4Parse CLI auto-downloads ~90 MB on the first "
        "MP3/WebM audio export (or install ffmpeg and Re-check)"
    )


def detect_dotnet() -> tuple[bool, str]:
    """Return (found, detail).

    The CUE4Parse CLI is shipped self-contained (the .NET runtime is baked
    into the binary), so we first check whether the bundled CLI exists — if
    so, the user needs nothing else (ffmpeg for audio export is resolved by
    the CLI itself). Only fall back to probing
    ``dotnet --version`` when the bundled CLI is missing (which means the
    user is running from source and needs .NET to build it).
    """
    cli_path = tools_detect.bundled_cue4parse_cli()
    if cli_path.is_file():
        from core.platform_detect import is_nixos, has_nix_ld, steam_run_path
        if is_nixos() and not has_nix_ld() and not steam_run_path():
            return True, (
                "CUE4Parse CLI bundled — BUT on NixOS it needs the nix-ld loader."
                " Enable programs.nix-ld.enable = true; in configuration.nix"
                " (or install Steam for steam-run)."
            )
        return True, "CUE4Parse CLI bundled (no .NET install needed)"

    if not shutil.which("dotnet"):
        return False, ".NET SDK not on PATH — needed to build CUE4ParseCLI from source"
    try:
        proc = subprocess.run(
            ["dotnet", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        ver = proc.stdout.strip().splitlines()[0] if proc.stdout else "unknown"
        return True, f".NET {ver} (will build CUE4ParseCLI on first build)"
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"dotnet check failed: {e}"


# ---------------------------------------------------------------------------
# Wizard pages
# ---------------------------------------------------------------------------

class _WelcomePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle(f"Welcome to EfficientAssetRipper v{__version__}")
        self.setSubTitle("A few quick questions before we get started.")

        layout = QVBoxLayout(self)

        intro = QLabel(
            "EfficientAssetRipper unpacks any UE4/5 game files and easily "
            "exports them to Blender. It also includes utility tools for "
            "previewing textures, combining models, and other useful "
            "features.\n\n"
            "This wizard will:\n"
            "  • Detect the tools it needs (Blender, Everything, .NET runtime)\n"
            "  • Point you at the Profile Manager for per-game settings\n\n"
            "You can skip this wizard at any time and configure things "
            "manually from File → Settings or the Profiles menu."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        legal = QLabel(
            "<b>Legal:</b> Using or distributing the output from this software "
            "may be against copyright legislation in your jurisdiction — you "
            "are responsible for ensuring you're not breaking any laws. "
            'See the <a href="https://github.com/exterminathan/EfficientAssetRipper#-legal">'
            "README</a> for the full disclaimer."
        )
        legal.setOpenExternalLinks(True)
        legal.setWordWrap(True)
        layout.addWidget(legal)

        layout.addStretch()


class _DependencyPage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("Dependency check")
        self.setSubTitle(
            "We need three core tools plus FFmpeg (used for MP3/WebM audio "
            "export; it self-installs on first use). Anything missing here "
            "can be installed and re-detected with the Re-check button."
        )

        layout = QVBoxLayout(self)

        self._rows: dict[str, dict] = {}
        everything_label = (
            "Everything (must be running)"
            if os.name == "nt"
            else "Everything (Linux: built-in walker used)"
        )
        for key, label, hint_url in (
            ("blender", "Blender 4.0+", "https://www.blender.org/download/"),
            ("everything", everything_label, "https://www.voidtools.com/"),
            ("ffmpeg", "FFmpeg (MP3/WebM audio export)", "https://ffmpeg.org/download.html"),
            ("dotnet", "CUE4Parse CLI / .NET 10.0", "https://dotnet.microsoft.com/download/dotnet/10.0"),
        ):
            row = QHBoxLayout()
            status = QLabel("…")
            status.setFixedWidth(20)
            font = QFont()
            font.setBold(True)
            status.setFont(font)
            name = QLabel(label)
            name.setMinimumWidth(220)
            detail = QLabel("")
            detail.setWordWrap(True)
            link = QLabel(f'<a href="{hint_url}">Download</a>')
            link.setOpenExternalLinks(True)
            link.setFixedWidth(80)
            row.addWidget(status)
            row.addWidget(name)
            row.addWidget(detail, stretch=1)
            row.addWidget(link)
            layout.addLayout(row)
            self._rows[key] = {"status": status, "detail": detail}

        layout.addSpacing(8)
        self._recheck_btn = QPushButton("Re-check")
        self._recheck_btn.clicked.connect(self.recheck)
        layout.addWidget(self._recheck_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        layout.addStretch()

    def initializePage(self):
        self.recheck()

    def recheck(self):
        probe_results: dict[str, tuple[bool, str]] = {}
        for key, probe in (
            ("blender", detect_blender),
            ("everything", detect_everything),
            ("ffmpeg", detect_ffmpeg),
            ("dotnet", detect_dotnet),
        ):
            ok, detail = probe_results[key] = probe()
            row = self._rows[key]
            row["status"].setText("✓" if ok else "✗")
            row["status"].setStyleSheet(
                "color: #4caf50;" if ok else "color: #e53935;"
            )
            row["detail"].setText(detail)

        # If Blender was detected, persist it so the rest of the wizard
        # (and the main window) can use it without prompting again.
        if probe_results["blender"][0] and not config.get("blender_exe"):
            config.set("blender_exe", probe_results["blender"][1])
        if os.name == "nt" and probe_results["everything"][0] and not config.get("everything_dll"):
            config.set("everything_dll", probe_results["everything"][1])
        if probe_results["dotnet"][0] and not config.get("cue4parse_cli"):
            config.set("cue4parse_cli", probe_results["dotnet"][1])


class _ProfilePage(QWizardPage):
    """Direct the user at the Profiles menu instead of editing global paths.

    Per-game settings (game folder, AES keys, UE version, output dirs) live
    in profile JSONs now, not QSettings — so this page replaces the old
    Game Folder + Output Directory pages. Copy varies based on whether the
    wizard is firing for the first time or being re-run from Help.
    """

    def __init__(self, is_first_run: bool):
        super().__init__()
        self._is_first_run = is_first_run

        if is_first_run:
            self.setTitle("Set up your first profile")
            self.setSubTitle(
                "EfficientAssetRipper stores per-game settings in profiles."
            )
            body_text = (
                "A <b>Default</b> profile has already been created for you. "
                "Open the Profile Manager to edit it (or create a new one) "
                "and point it at your game's <code>Paks</code> folder, set "
                "AES keys, pick an output directory, and choose a UE "
                "version.\n\n"
                "You can also do this later from the <b>Profiles</b> menu "
                "in the main window."
            )
        else:
            self.setTitle("Profiles")
            self.setSubTitle(
                "Per-game settings now live in profiles instead of the wizard."
            )
            body_text = (
                "Profiles store the game folder, AES keys, UE version, and "
                "output directories for each game you work with. They're "
                "managed from the <b>Profiles</b> menu in the main window — "
                "or click below to open the Profile Manager now."
            )

        layout = QVBoxLayout(self)

        body = QLabel(body_text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(body)

        layout.addSpacing(12)

        btn_row = QHBoxLayout()
        self._open_btn = QPushButton("Open Profile Manager…")
        self._open_btn.clicked.connect(self._open_profile_manager)
        btn_row.addWidget(self._open_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        layout.addStretch()

    def _open_profile_manager(self):
        # The wizard owns the modal stack — don't open the profile dialog
        # while we're still inside QWizard.exec(). Set a flag and accept;
        # MainWindow opens the dialog after exec() returns.
        wiz = self.wizard()
        if wiz is not None:
            wiz.open_profile_manager_after = True
            wiz.accept()


class _DonePage(QWizardPage):
    def __init__(self):
        super().__init__()
        self.setTitle("All set!")
        self.setSubTitle("EfficientAssetRipper is ready to go.")

        layout = QVBoxLayout(self)
        msg = QLabel(
            "You can change any of these settings later from "
            "<b>File → Settings</b>, and create separate profiles per "
            "game from the profile bar at the top of the main window.\n\n"
            "Click <b>Finish</b> to launch the app."
        )
        msg.setWordWrap(True)
        layout.addWidget(msg)
        layout.addStretch()


# ---------------------------------------------------------------------------
# Wizard shell
# ---------------------------------------------------------------------------

class SetupWizard(QWizard):
    """First-run setup wizard. Sets ``setup_complete=1`` on Finish."""

    skipped = Signal()
    completed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # Captured at construction so re-running from Help (where setup_complete
        # is already "1") shows different copy than the genuine first run.
        self._is_first_run = config.get("setup_complete") != "1"
        # MainWindow checks this after exec() to decide whether to open the
        # ProfileDialog — avoids stacking modals inside QWizard.exec().
        self.open_profile_manager_after = False

        self.setWindowTitle(f"EfficientAssetRipper v{__version__} — Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setOption(QWizard.WizardOption.NoBackButtonOnStartPage, True)
        self.setOption(QWizard.WizardOption.HaveCustomButton1, True)
        self.setButtonText(QWizard.WizardButton.CustomButton1, "Skip Setup")
        self.setButtonLayout([
            QWizard.WizardButton.Stretch,
            QWizard.WizardButton.CustomButton1,
            QWizard.WizardButton.BackButton,
            QWizard.WizardButton.NextButton,
            QWizard.WizardButton.FinishButton,
            QWizard.WizardButton.CancelButton,
        ])
        self.customButtonClicked.connect(self._on_custom_button)

        self.addPage(_WelcomePage())
        self.addPage(_DependencyPage())
        self.addPage(_ProfilePage(self._is_first_run))
        self.addPage(_DonePage())

        self.setMinimumSize(640, 460)

    def _on_custom_button(self, which: int):
        if which == QWizard.WizardButton.CustomButton1:
            log.info("User skipped first-run setup")
            self.skipped.emit()
            self.reject()

    def accept(self):
        config.set("setup_complete", "1")
        log.info("First-run setup completed")
        self.completed.emit()
        super().accept()


def should_show_setup() -> bool:
    """Return True if the wizard should fire on this launch."""
    return config.get("setup_complete") != "1"
