"""Tests for UnpackerPanel export-failure popup + suppression flag."""

from __future__ import annotations

import pytest

from PySide6.QtWidgets import QCheckBox, QMessageBox

from gui.unpacker_panel import UnpackerPanel


class TestExportFailurePopup:
    """Verify that export errors raise a modal and respect the suppress flag."""

    @pytest.fixture
    def panel(self, qtbot):
        p = UnpackerPanel()
        qtbot.addWidget(p)
        return p

    def test_suppress_flag_initialized_false(self, panel):
        assert panel._suppress_export_error_popup is False

    def test_export_done_with_failures_shows_popup(self, panel, mocker):
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        failed = [{"path": "Game/Foo.uasset", "error": "Invalid FString length"}]
        panel._on_export_done(["Game/Bar.uasset"], failed)
        spy.assert_called_once()

    def test_export_done_no_failures_no_popup(self, panel, mocker):
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_export_done(["Game/Bar.uasset"], [])
        spy.assert_not_called()

    def test_popup_suppressed_when_flag_set(self, panel, mocker):
        panel._suppress_export_error_popup = True
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_export_done([], [{"path": "x", "error": "boom"}])
        spy.assert_not_called()

    def test_user_checking_box_sets_suppress_flag(self, panel, mocker):
        # The helper inspects checkBox().isChecked() after exec() returns,
        # so simulating "user ticked the box" = stub isChecked to True.
        mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        mocker.patch.object(QCheckBox, "isChecked", return_value=True)
        panel._on_export_done([], [{"path": "x", "error": "boom"}])
        assert panel._suppress_export_error_popup is True

    def test_unchecked_box_leaves_suppress_flag_false(self, panel, mocker):
        mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        mocker.patch.object(QCheckBox, "isChecked", return_value=False)
        panel._on_export_done([], [{"path": "x", "error": "boom"}])
        assert panel._suppress_export_error_popup is False

    def test_fatal_error_during_export_shows_popup(self, panel, mocker):
        panel._exporting = True
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_error("CLI crashed: version mismatch")
        spy.assert_called_once()
        assert panel._exporting is False

    def test_fatal_error_outside_export_no_popup(self, panel, mocker):
        panel._exporting = False
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_error("Mount failure")
        spy.assert_not_called()

    def test_fatal_error_respects_suppress_flag(self, panel, mocker):
        panel._exporting = True
        panel._suppress_export_error_popup = True
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_error("CLI crashed")
        spy.assert_not_called()
        # _exporting is still cleared so the UI re-enables export buttons
        assert panel._exporting is False

    def test_process_ended_resets_suppress_flag(self, panel):
        panel._suppress_export_error_popup = True
        panel._on_process_ended()
        assert panel._suppress_export_error_popup is False

    def test_load_from_profile_resets_suppress_flag(self, panel):
        panel._suppress_export_error_popup = True
        panel.load_from_profile({
            "game_dir": "",
            "ue_version": "GAME_UE5_4",
            "mappings_path": "",
            "unpack_output_dir": "",
            "aes_keys": [],
        })

    def test_export_done_cancelled_summary_not_success(self, panel):
        """A cancelled run must not be reported as "Export complete"."""
        panel._on_export_done(["Game/A"], [], cancelled=True, remaining=9)
        assert "cancelled" in panel._status_label.text()
        assert "9" in panel._status_label.text()

    def test_export_done_cancelled_discards_progress_bar(self, panel):
        """A cancelled run must blank the bar instead of leaving it stalled
        at the last partial value the CLI reported."""
        panel._progress.setRange(0, 100)
        panel._progress.setValue(67)
        panel._on_export_done(["Game/A"], [], cancelled=True, remaining=9)
        # Qt's reset() marks the bar "no progress" via the -1 sentinel
        # (distinct from value 0, which would imply a started-but-empty run).
        assert panel._progress.value() == -1

    def test_export_done_success_fills_progress_bar(self, panel):
        """A normal completion still fills the bar from the result counts."""
        panel._progress.setRange(0, 100)
        panel._progress.setValue(10)
        panel._on_export_done(["Game/A"], [])
        assert panel._progress.value() == panel._progress.maximum()

    def test_export_done_cancelled_with_failures_no_popup(self, panel, mocker):
        spy = mocker.patch.object(QMessageBox, "exec", return_value=QMessageBox.StandardButton.Ok)
        panel._on_export_done(["Game/A"], [{"path": "x", "error": "boom"}],
                              cancelled=True, remaining=5)
        spy.assert_not_called()

    def test_export_done_cancelled_drops_pending_chain(self, panel, mocker):
        """Cancel must not kick off the queued FMOD/WWise export stage."""
        fmod_spy = mocker.patch.object(panel._unpacker, "export_fmod_audio")
        wwise_spy = mocker.patch.object(panel._unpacker, "export_wwise_audio")
        panel._pending_fmod_export = ([{"bank": "x"}], "/tmp/out")
        panel._pending_wwise_export = ([{"wem": "y"}], "/tmp/out")
        panel._on_export_done([], [], cancelled=True, remaining=1)
        assert panel._pending_fmod_export is None
        assert panel._pending_wwise_export is None
        fmod_spy.assert_not_called()
        wwise_spy.assert_not_called()

    def test_export_done_warning_count_shown_in_summary(self, panel):
        panel._on_export_done(["Game/A"], [], warning_count=1167)
        assert "1167 asset warnings" in panel._status_label.text()

    def test_process_ended_during_export_logs_abort(self, panel, qtbot):
        panel._exporting = True
        with qtbot.waitSignal(panel.log_message, timeout=1000) as sig:
            panel._on_process_ended()
        assert "aborted" in sig.args[0].lower()
        assert panel._exporting is False

    def test_process_ended_after_cancel_shutdown_remounts(self, panel):
        """A cancel-driven CLI shutdown must bring the Unpacker straight
        back to a usable state (auto re-mount), not park every button
        behind a manual re-mount step."""
        panel._exporting = True
        panel._unpacker._cancel_shutdown_requested = True
        calls = []
        panel._mount_archives = lambda: calls.append(True)
        panel._on_process_ended()
        assert calls == [True]
        assert panel._exporting is False

    def test_process_ended_crash_does_not_remount(self, panel):
        """A genuine CLI crash keeps the manual re-mount flow — no auto
        restart for unplanned exits."""
        panel._exporting = True
        calls = []
        panel._mount_archives = lambda: calls.append(True)
        panel._on_process_ended()
        assert calls == []

    def test_process_ended_idle_no_abort_warning(self, panel):
        panel._exporting = False
        sent = []
        panel.log_message.connect(lambda m, l: sent.append((m, l)))
        panel._on_process_ended()
        assert not any("aborted" in m.lower() for m, _ in sent)
        assert panel._suppress_export_error_popup is False
