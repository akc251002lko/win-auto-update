"""Unit tests for the WinAutoUpdate command-line core."""

import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import win_auto_update as app


class ParseUpdateTests(unittest.TestCase):
    def test_parse_update_record(self):
        records = app.parse_updates([{
            "update_id": "abc-123", "revision": 7, "title": "Security Update",
            "description": "Important fix", "downloaded": True,
            "requires_user_input": False, "reboot_behavior": "2",
            "categories": ["Security Updates"],
        }])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].update_id, "abc-123")
        self.assertEqual(records[0].revision, 7)
        self.assertTrue(records[0].downloaded)
        self.assertEqual(app.update_identity(records[0]), "abc-123:7")

    def test_empty_update_response_is_valid(self):
        self.assertEqual(app.parse_updates([]), [])
        self.assertEqual(app.parse_updates(None), [])

    def test_invalid_update_response_raises(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates("not-an-array")

    def test_malformed_update_fields_raise_domain_error(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates([{"update_id": "abc", "revision": "invalid"}])

    def test_missing_update_identity_is_rejected(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates([{"update_id": "", "revision": 1}])

    def test_non_list_categories_are_rejected(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates([{"update_id": "abc", "revision": 1, "categories": "Security"}])

    def test_string_boolean_flags_are_rejected(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates([{"update_id": "abc", "revision": 1, "downloaded": "false"}])

    def test_boolean_revision_is_rejected(self):
        with self.assertRaises(app.PowerShellError):
            app.parse_updates([{"update_id": "abc", "revision": True}])


class InstallSafetyTests(unittest.TestCase):
    def test_preview_never_calls_powershell(self):
        with mock.patch.object(app, "run_powershell") as runner:
            result = app.execute_install(["abc:1"], False, False)
        self.assertEqual(result["status"], "planned")
        runner.assert_not_called()

    def test_empty_selection_is_rejected(self):
        with self.assertRaises(app.UpdateSelectionError):
            app.execute_install([], False, False)

    def test_duplicate_selection_is_rejected(self):
        with self.assertRaises(app.UpdateSelectionError):
            app.execute_install(["abc:1", "abc:1"], False, False)

    def test_execution_requires_administrator(self):
        with mock.patch.object(app, "is_admin", return_value=False):
            with self.assertRaises(app.PrivilegeError):
                app.execute_install(["abc:1"], True, True)

    def test_execution_requires_explicit_eula_acceptance(self):
        with mock.patch.object(app, "is_admin", return_value=True):
            with self.assertRaises(app.UpdateSelectionError):
                app.execute_install(["abc:1"], False, True)

    def test_install_exit_code_accepts_complete_success(self):
        self.assertEqual(app.install_exit_code({
            "download_result_code": 2,
            "install_result_code": 2,
            "reboot_required": False,
            "updates": [{"result_code": 2}],
        }), 0)

    def test_install_exit_code_rejects_partial_update_failure(self):
        self.assertEqual(app.install_exit_code({
            "download_result_code": 2,
            "install_result_code": 3,
            "reboot_required": False,
            "updates": [{"result_code": 2}, {"result_code": 4}],
        }), 1)

    def test_install_exit_code_rejects_malformed_response(self):
        with self.assertRaises(app.PowerShellError):
            app.install_exit_code({"download_result_code": 2, "install_result_code": "success", "reboot_required": False, "updates": [{"result_code": 2}]})

    def test_execution_passes_exact_identity_to_agent(self):
        expected = {"install_result_code": 2, "reboot_required": False}
        with mock.patch.object(app, "is_admin", return_value=True), \
             mock.patch.object(app, "run_powershell", return_value=expected) as runner:
            result = app.execute_install(["abc:4"], True, True)
        self.assertEqual(result, expected)
        runner.assert_called_once_with({
            "mode": "install", "updates": ["abc:4"], "accept_eula": True,
        })


class PlatformAndCliTests(unittest.TestCase):
    def test_non_windows_platform_is_rejected(self):
        with mock.patch.object(app.os, "name", "posix"):
            with self.assertRaises(app.UnsupportedPlatformError):
                app.require_windows()


    @mock.patch("win_auto_update.os.name", "nt")
    @mock.patch("win_auto_update.sys.getwindowsversion")
    def test_windows_7_without_service_pack_one_is_rejected(self, get_version):
        get_version.return_value = SimpleNamespace(
            major=6, minor=1, service_pack="", service_pack_major=0
        )
        with self.assertRaises(app.UnsupportedPlatformError):
            app.require_windows()

    @mock.patch("win_auto_update.os.name", "nt")
    @mock.patch("win_auto_update.sys.getwindowsversion")
    def test_windows_7_service_pack_one_is_supported(self, get_version):
        get_version.return_value = SimpleNamespace(
            major=6, minor=1, service_pack="Service Pack 1", service_pack_major=1
        )
        app.require_windows()

    @mock.patch("win_auto_update.os.name", "nt")
    @mock.patch("win_auto_update.sys.getwindowsversion")
    def test_windows_vista_is_rejected(self, get_version):
        get_version.return_value = SimpleNamespace(major=6, minor=0)
        with self.assertRaises(app.UnsupportedPlatformError):
            app.require_windows()

    @unittest.skipUnless(sys.platform == "win32", "Windows PowerShell integration test")
    def test_powershell_resolves_from_windows_system_directory(self):
        resolved = app._powershell_executable()
        self.assertTrue(resolved.is_absolute())
        self.assertEqual(resolved.name.lower(), "powershell.exe")
        self.assertTrue(resolved.is_file())

    @unittest.skipUnless(sys.platform == "win32", "Windows version integration test")
    def test_current_windows_version_is_supported(self):
        app.require_windows()

    def test_parser_requires_update_identity_for_install(self):
        with self.assertRaises(SystemExit):
            app.build_parser().parse_args(["install"])

    def test_version_argument(self):
        with self.assertRaises(SystemExit) as raised:
            app.build_parser().parse_args(["--version"])
        self.assertEqual(raised.exception.code, 0)


class LoggingAndPowerShellDiagnosticsTests(unittest.TestCase):
    def test_log_record_contains_active_operation_id(self):
        token = app.OPERATION_ID.set("operation-test-456")
        try:
            record = app.logging.LogRecord(
                "test", app.logging.INFO, __file__, 1, "message", (), None
            )
            app.OperationIdFilter().filter(record)
            self.assertEqual(record.operation_id, "operation-test-456")
        finally:
            app.OPERATION_ID.reset(token)

    def test_powershell_error_preserves_first_2000_characters(self):
        diagnostic = "Initial PowerShell failure context. " + ("detail " * 500)
        completed = SimpleNamespace(returncode=1, stderr=diagnostic, stdout="")
        with mock.patch.object(app, "require_windows"), \\
             mock.patch.object(app, "_powershell_executable", return_value=Path("powershell.exe")), \\
             mock.patch.object(app.subprocess, "run", return_value=completed):
            with self.assertRaises(app.PowerShellError) as raised:
                app.run_powershell({"mode": "check"})
        self.assertEqual(str(raised.exception), diagnostic[:2000])


if __name__ == "__main__":
    unittest.main()
