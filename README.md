# WinAutoUpdate

A cautious Windows Update manager that inspects applicable updates and installs only the exact updates explicitly selected by the operator. It uses the Windows Update Agent (WUA) COM API built into Windows; it does not download update packages from third-party sites.

## What This Repository Is For

WinAutoUpdate provides a transparent command-line interface for:
- Checking Windows Update service status.
- Searching for applicable, non-hidden updates.
- Reviewing update titles, categories, descriptions, and stable update identities.
- Previewing an installation plan without changing the system.
- Downloading and installing only explicitly selected update identities after administrator and licence-confirmation checks.
- Reporting per-update result codes and whether Windows requires a restart.

This is not an automatic background updater, driver updater, WSUS replacement, or Windows servicing repair tool. It does not silently install all available updates, hide updates, change update policies, or restart the computer.

## How To Use

### Requirements

- Windows 7 SP1 or later. Windows 11 is supported on its supported architectures; Windows 11 has no x86 edition.
- Windows PowerShell 5.1 available at the standard Windows installation path.
- Python 3.8 or later. Python 3.8 is the final CPython line with official Windows 7 support and is end-of-life; use a maintained Python release on supported modern Windows versions.
- Administrator privileges are required only for installation.
- Internet access and a functioning Windows Update Agent/service configuration.

Windows 7 SP1 is a legacy platform. Microsoft support, update catalog availability, TLS configuration, servicing-stack prerequisites, and the installed Windows Update Agent can affect whether updates are offered. Compatibility is not a guarantee that Microsoft still publishes updates for a particular Windows release.

### Run

From the repository root, run:

    python src\win_auto_update.py status
    python src\win_auto_update.py check

The check command is read-only. Copy the exact identity printed for an update, in the form UPDATE_GUID:REVISION, then preview the installation:

    python src\win_auto_update.py install --update-id "UPDATE_GUID:REVISION"

To download and install that exact update, open an elevated PowerShell or Command Prompt, review the update's applicable licence terms, and explicitly confirm both execution and EULA acceptance:

    python src\win_auto_update.py install --update-id "UPDATE_GUID:REVISION" --execute --accept-eula

Repeat --update-id to select multiple exact update identities. The tool re-runs the Windows Update search and verifies that every requested ID and revision is still available before installation. If an update has changed or disappeared, the operation stops rather than silently substituting another update.

### Safety model

- Status and check operations do not install updates or change Windows Update policy.
- Installation is preview-only unless --execute is supplied.
- Actual installation requires an elevated Administrator process and explicit --accept-eula.
- Updates that request interactive user input are rejected by the unattended installation path.
- No automatic reboot is initiated. If Windows reports that a restart is required, the tool reports it and leaves the decision to the operator.
- Commands are launched without a shell; the PowerShell bridge is a fixed script and update identities are passed as JSON input.
- A one-hour process timeout prevents a hung servicing operation from running indefinitely.

Windows Update can perform system-level changes once an installation is explicitly confirmed. Back up important data and use an appropriate maintenance window before installing updates.

## Project Structure

    src/
      win_auto_update.py       Application and Windows Update Agent bridge
    tests/
      test_win_auto_update.py  Unit tests for parsing and safety controls
    .github/
      workflows/
        ci.yml                 Windows CI test matrix

## Development and Tests

The application and tests use only the Python standard library. No pip dependencies are required.

    python -m unittest discover -s tests -v

CI runs the test suite on Windows with Python 3.8, 3.12, and 3.14. Hosted CI validates Python-level behavior; it does not certify real Windows Update installation behavior on every Windows release. Test actual update servicing only in disposable virtual machines with snapshots.

## Limitations

- Windows Update Agent COM behavior depends on the local Windows edition, servicing stack, policy, network, and Microsoft update services.
- The tool does not bypass WSUS, Group Policy, metered-connection settings, update deferrals, or other administrator policies.
- It does not promise that an update is safe for every device or application.
- Installation results are the result codes returned by Windows Update Agent; consult Microsoft documentation for their meaning.
- Windows 7/8/8.1 are legacy operating systems. Modern CI runners cannot validate those operating systems directly.

## Security

See [SECURITY.md](SECURITY.md) for vulnerability reporting and the security model.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Community

Join the Telegram community: https://t.me/githubakc251002lko

## License

MIT. See [LICENSE](LICENSE).
