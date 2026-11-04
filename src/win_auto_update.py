#!/usr/bin/env python3
"""Windows Update Manager using the built-in Windows Update Agent COM API."""

from __future__ import annotations

import argparse
import contextvars
import ctypes
import json
import logging
import os
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

APP_NAME = "WinAutoUpdate"
VERSION = "1.0.0"
COMMAND_TIMEOUT_SECONDS = 60 * 60

LOGGER = logging.getLogger(APP_NAME)


class UpdateManagerError(Exception):
    """Base exception for expected update-manager failures."""


class UnsupportedPlatformError(UpdateManagerError):
    """Raised when the host is not a supported Windows platform."""


class PrivilegeError(UpdateManagerError):
    """Raised when an operation requires an elevated process."""


class PowerShellError(UpdateManagerError):
    """Raised when the Windows Update Agent bridge fails."""


class UpdateSelectionError(UpdateManagerError):
    """Raised when requested update identities are invalid or unavailable."""


@dataclass(frozen=True)
class UpdateRecord:
    update_id: str
    revision: int
    title: str
    description: str
    downloaded: bool
    requires_user_input: bool
    reboot_behavior: str
    categories: List[str]


POWERSHELL_BRIDGE = r"""
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$inputData = [Console]::In.ReadToEnd() | ConvertFrom-Json
$session = New-Object -ComObject Microsoft.Update.Session
$session.ClientApplicationID = 'WinAutoUpdate'
$searcher = $session.CreateUpdateSearcher()
function Emit($value) {
    if ($null -eq $value) { $value = @{} }
    $json = ConvertTo-Json -InputObject $value -Depth 7 -Compress
    [Console]::Out.WriteLine($json)
}
function Get-UpdateRecords($updates) {
    $records = @()
    for ($i = 0; $i -lt $updates.Count; $i++) {
        $u = $updates.Item($i)
        $cats = @()
        for ($j = 0; $j -lt $u.Categories.Count; $j++) {
            $cats += $u.Categories.Item($j).Name
        }
        $records += [PSCustomObject]@{
            update_id = [string]$u.Identity.UpdateID
            revision = [int]$u.Identity.RevisionNumber
            title = [string]$u.Title
            description = [string]$u.Description
            downloaded = [bool]$u.IsDownloaded
            requires_user_input = [bool]$u.InstallationBehavior.CanRequestUserInput
            reboot_behavior = [string]$u.InstallationBehavior.RebootBehavior
            categories = @($cats)
        }
    }
    return ,$records
}
if ($inputData.mode -eq 'status') {
    $services = @()
    foreach ($name in @('wuauserv', 'BITS')) {
        $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
        if ($null -eq $svc) {
            $services += [PSCustomObject]@{ name = $name; status = 'not-found' }
        } else {
            $services += [PSCustomObject]@{ name = $name; status = [string]$svc.Status }
        }
    }
    Emit ([PSCustomObject]@{
        computer = $env:COMPUTERNAME
        os_caption = (Get-WmiObject Win32_OperatingSystem -ErrorAction SilentlyContinue).Caption
        services = @($services)
        client_application_id = [string]$session.ClientApplicationID
    })
    exit 0
}
$result = $searcher.Search("IsInstalled=0 and IsHidden=0")
if ($inputData.mode -eq 'check') {
    Emit (Get-UpdateRecords $result.Updates)
    exit 0
}
if ($inputData.mode -eq 'install') {
    $requested = @($inputData.updates)
    if ($requested.Count -eq 0) { throw 'No update identities were supplied.' }
    $selected = New-Object -ComObject Microsoft.Update.UpdateColl
    foreach ($identity in $requested) {
        $parts = $identity -split ':', 2
        if ($parts.Count -ne 2) { throw "Invalid update identity: $identity" }
        $found = $false
        for ($i = 0; $i -lt $result.Updates.Count; $i++) {
            $u = $result.Updates.Item($i)
            if ($u.Identity.UpdateID -eq $parts[0] -and
                [string]$u.Identity.RevisionNumber -eq $parts[1]) {
                if ($u.InstallationBehavior.CanRequestUserInput) {
                    throw "Update requires interactive user input and cannot be installed unattended: $($u.Title)"
                }
                if (-not $u.EulaAccepted) {
                    if (-not [bool]$inputData.accept_eula) {
                        throw "EULA not accepted for update: $($u.Title). Review and pass --accept-eula to proceed."
                    }
                    $u.AcceptEula()
                }
                [void]$selected.Add($u)
                $found = $true
                break
            }
        }
        if (-not $found) { throw "Update is no longer available or its revision changed: $identity" }
    }
    $downloader = $session.CreateUpdateDownloader()
    $downloader.Updates = $selected
    $downloadResult = $downloader.Download()
    if ([int]$downloadResult.ResultCode -ne 2) {
        throw "Download failed with Windows Update result code $($downloadResult.ResultCode)."
    }
    $installer = $session.CreateUpdateInstaller()
    $installer.Updates = $selected
    $installResult = $installer.Install()
    $outcomes = @()
    for ($i = 0; $i -lt $selected.Count; $i++) {
        $item = $installResult.GetUpdateResult($i)
        $outcomes += [PSCustomObject]@{
            update_id = $selected.Item($i).Identity.UpdateID
            title = $selected.Item($i).Title
            result_code = [int]$item.ResultCode
            hresult = ('0x{0:X8}' -f ([uint32]$item.HResult))
        }
    }
    Emit ([PSCustomObject]@{
        download_result_code = [int]$downloadResult.ResultCode
        install_result_code = [int]$installResult.ResultCode
        reboot_required = [bool]$installResult.RebootRequired
        updates = @($outcomes)
    })
    exit 0
}
throw "Unknown operation: $($inputData.mode)"
"""


OPERATION_ID = contextvars.ContextVar("win_auto_update_operation_id", default="-")


class OperationIdFilter(logging.Filter):
    """Add the active CLI operation identifier to each log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.operation_id = OPERATION_ID.get()
        return True


def configure_logging() -> Optional[Path]:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s [operation_id=%(operation_id)s]: %(message)s"
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    if LOGGER.handlers:
        for handler in LOGGER.handlers:
            if not any(isinstance(item, OperationIdFilter) for item in handler.filters):
                handler.addFilter(OperationIdFilter())
            handler.setFormatter(formatter)
        return next(
            (Path(handler.baseFilename) for handler in LOGGER.handlers
             if isinstance(handler, logging.FileHandler)),
            None,
        )

    candidates: List[Path] = []
    app_data = os.environ.get("LOCALAPPDATA")
    if app_data:
        candidates.append(Path(app_data) / APP_NAME / "logs")
    candidates.append(Path(tempfile.gettempdir()) / APP_NAME / "logs")
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "win-auto-update.log"
            handlers: List[logging.Handler] = [
                logging.FileHandler(str(path), encoding="utf-8"),
                logging.StreamHandler(sys.stderr),
            ]
            for handler in handlers:
                handler.addFilter(OperationIdFilter())
                handler.setFormatter(formatter)
                LOGGER.addHandler(handler)
            return path
        except OSError:
            continue

    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(OperationIdFilter())
    handler.setFormatter(formatter)
    LOGGER.addHandler(handler)
    return None

def require_windows() -> None:
    if os.name != "nt":
        raise UnsupportedPlatformError("WinAutoUpdate requires Windows 7 SP1 or later.")
    try:
        version = sys.getwindowsversion()
        if version.major == 6 and version.minor == 1:
            service_pack = str(getattr(version, "service_pack", "")).lower()
            service_pack_major = int(getattr(version, "service_pack_major", 0))
            if service_pack_major < 1 and "service pack 1" not in service_pack:
                raise UnsupportedPlatformError("Windows 7 Service Pack 1 is required.")
            return
        if (version.major == 6 and version.minor in (2, 3)) or version.major >= 10:
            return
        raise UnsupportedPlatformError("WinAutoUpdate requires Windows 7 SP1 or later.")
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        raise UnsupportedPlatformError("Unable to identify a supported Windows version.") from exc


def is_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def _powershell_executable() -> Path:
    """Resolve native Windows PowerShell without trusting PATH or WINDIR."""
    if os.name != "nt":
        raise UnsupportedPlatformError("Windows PowerShell requires Windows.")
    kernel32 = ctypes.windll.kernel32
    windows_buffer = ctypes.create_unicode_buffer(32768)
    windows_length = kernel32.GetWindowsDirectoryW(windows_buffer, len(windows_buffer))
    if windows_length == 0 or windows_length >= len(windows_buffer):
        raise PowerShellError("Unable to resolve the Windows installation directory.")
    is_wow64 = ctypes.c_bool(False)
    try:
        wow64 = bool(kernel32.IsWow64Process(
            kernel32.GetCurrentProcess(), ctypes.byref(is_wow64)
        )) and is_wow64.value
    except AttributeError:
        wow64 = False
    system_alias = "Sysnative" if wow64 else "System32"
    executable = (
        Path(windows_buffer.value)
        / system_alias
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    if not executable.is_file():
        raise PowerShellError("Windows PowerShell 5.1 was not found in the Windows system directory.")
    return executable


def run_powershell(payload: Dict[str, Any]) -> Any:
    require_windows()
    powershell = str(_powershell_executable())
    script_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", prefix="win-auto-update-",
            encoding="utf-8", delete=False,
        ) as script_file:
            script_file.write(POWERSHELL_BRIDGE)
            script_path = Path(script_file.name)
        command = [
            powershell, "-NoLogo", "-NoProfile", "-NonInteractive",
            "-ExecutionPolicy", "Bypass", "-File", str(script_path),
        ]
        completed = subprocess.run(
            command,
            input=json.dumps(payload),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            message = (completed.stderr or completed.stdout or "Unknown PowerShell failure").strip()
            raise PowerShellError(message[:2000])
        output = (completed.stdout or "").strip()
        if not output:
            raise PowerShellError("Windows Update Agent returned an empty response.")
        try:
            return json.loads(output.splitlines()[-1])
        except (ValueError, TypeError) as exc:
            raise PowerShellError("Windows Update Agent returned invalid JSON.") from exc
    except subprocess.TimeoutExpired as exc:
        raise PowerShellError("Windows Update operation exceeded the one-hour timeout.") from exc
    except OSError as exc:
        raise PowerShellError("Unable to execute Windows PowerShell: " + str(exc)) from exc
    finally:
        if script_path is not None:
            try:
                script_path.unlink()
            except OSError:
                LOGGER.warning("Could not remove temporary PowerShell script: %s", script_path)


def parse_updates(payload: Any) -> List[UpdateRecord]:
    if isinstance(payload, dict):
        payload = [payload]
    if payload is None:
        return []
    if not isinstance(payload, list):
        raise PowerShellError("Unexpected update-list response.")
    records: List[UpdateRecord] = []
    for item in payload:
        if not isinstance(item, dict):
            raise PowerShellError("Update-list response contains an invalid record.")
        try:
            raw_revision = item.get("revision", 0)
            if isinstance(raw_revision, bool):
                raise TypeError("revision must be an integer")
            revision = int(raw_revision)
            raw_categories = item.get("categories", [])
            downloaded = item.get("downloaded", False)
            requires_user_input = item.get("requires_user_input", False)
            if not isinstance(raw_categories, list):
                raise TypeError("categories must be a list")
            if not isinstance(downloaded, bool) or not isinstance(requires_user_input, bool):
                raise TypeError("update flags must be booleans")
            raw_update_id = item.get("update_id", "")
            if not isinstance(raw_update_id, str):
                raise TypeError("update_id must be a string")
            record = UpdateRecord(
                update_id=raw_update_id,
                revision=revision,
                title=str(item.get("title", "Untitled update")),
                description=str(item.get("description", "")),
                downloaded=downloaded,
                requires_user_input=requires_user_input,
                reboot_behavior=str(item.get("reboot_behavior", "unknown")),
                categories=[str(value) for value in raw_categories],
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise PowerShellError("Update-list response contains invalid field values.") from exc
        if not record.update_id or record.revision < 1:
            raise PowerShellError("Update-list response contains an invalid update identity.")
        records.append(record)
    return records


def update_identity(update: UpdateRecord) -> str:
    return "{0}:{1}".format(update.update_id, update.revision)


def print_updates(updates: Sequence[UpdateRecord]) -> None:
    if not updates:
        print("No applicable updates were found.")
        return
    print("Applicable updates: {0}".format(len(updates)))
    for index, update in enumerate(updates, 1):
        print("\n{0}. {1}".format(index, update.title))
        print("   Identity: " + update_identity(update))
        print("   Downloaded: " + ("yes" if update.downloaded else "no"))
        print("   Interactive input required: " + ("yes" if update.requires_user_input else "no"))
        print("   Categories: " + (", ".join(update.categories) or "not reported"))
        if update.description:
            print("   " + update.description.replace("\r", " ").replace("\n", " ")[:500])


def execute_install(identities: Sequence[str], accept_eula: bool, execute: bool) -> Dict[str, Any]:
    if not identities:
        raise UpdateSelectionError("Select at least one exact update identity from the check output.")
    if len(set(identities)) != len(identities):
        raise UpdateSelectionError("Duplicate update identities are not allowed.")
    if not execute:
        return {
            "status": "planned",
            "updates": list(identities),
            "message": "Preview only. No updates were downloaded or installed.",
        }
    if not is_admin():
        raise PrivilegeError("Installing updates requires an elevated Administrator session.")
    if not accept_eula:
        raise UpdateSelectionError("Pass --accept-eula only after reviewing the applicable update licence terms.")
    return run_powershell({
        "mode": "install",
        "updates": list(identities),
        "accept_eula": True,
    })


def install_exit_code(result: Any) -> int:
    """Validate an installation response and report partial or failed updates."""
    if not isinstance(result, dict):
        raise PowerShellError("Windows Update Agent returned an invalid installation response.")
    if result.get("status") == "planned":
        return 0
    download_code = result.get("download_result_code")
    aggregate = result.get("install_result_code")
    outcomes = result.get("updates")
    if isinstance(download_code, bool) or not isinstance(download_code, int) or download_code != 2:
        raise PowerShellError("Windows Update Agent did not confirm successful downloads.")
    if isinstance(aggregate, bool) or not isinstance(aggregate, int):
        raise PowerShellError("Windows Update Agent omitted a valid installation result code.")
    reboot_required = result.get("reboot_required")
    if not isinstance(reboot_required, bool):
        raise PowerShellError("Windows Update Agent omitted reboot status.")
    if isinstance(outcomes, dict):
        outcomes = [outcomes]
    if not isinstance(outcomes, list) or not outcomes:
        raise PowerShellError("Windows Update Agent returned invalid or empty per-update results.")
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            raise PowerShellError("Windows Update Agent returned an invalid per-update result.")
        result_code = outcome.get("result_code")
        if isinstance(result_code, bool) or not isinstance(result_code, int):
            raise PowerShellError("Windows Update Agent omitted a per-update result code.")
        if result_code != 2:
            return 1
    return 1 if aggregate != 2 else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and selectively install updates using the built-in Windows Update Agent."
    )
    parser.add_argument("--version", action="version", version="WinAutoUpdate " + VERSION)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="Show Windows Update service status.")
    subparsers.add_parser("check", help="Search for applicable updates without installing them.")
    install = subparsers.add_parser("install", help="Install explicitly selected update identities.")
    install.add_argument("--update-id", action="append", required=True, dest="identities",
                         help="Exact UPDATE_GUID:REVISION identity printed by the check command; repeat as needed.")
    install.add_argument("--execute", action="store_true",
                         help="Download and install the selected updates. Without this, preview only.")
    install.add_argument("--accept-eula", action="store_true",
                         help="Accept selected updates' EULAs; use only after reviewing applicable terms.")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    operation_token = OPERATION_ID.set(uuid.uuid4().hex[:12])
    try:
        log_path = configure_logging()
        LOGGER.info("Starting %s %s; log=%s", APP_NAME, VERSION, log_path or "stderr")
        args = build_parser().parse_args(argv)
        require_windows()
        if args.command is None:
            print("WinAutoUpdate {0}".format(VERSION))
            print("Commands: status, check, install")
            print("Start with: python win_auto_update.py check")
            return 0
        if args.command == "status":
            result = run_powershell({"mode": "status"})
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0
        if args.command == "check":
            updates = parse_updates(run_powershell({"mode": "check"}))
            print_updates(updates)
            return 0
        result = execute_install(args.identities, args.accept_eula, args.execute)
        print(json.dumps(result, indent=2, sort_keys=True))
        if result.get("reboot_required"):
            print("\\nA restart is required. WinAutoUpdate will not restart the computer automatically.")
        return install_exit_code(result)
    except UpdateManagerError as exc:
        LOGGER.error("%s", exc)
        print("ERROR: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        LOGGER.warning("Interrupted by user.")
        print("\\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        OPERATION_ID.reset(operation_token)


if __name__ == "__main__":
    sys.exit(main())
