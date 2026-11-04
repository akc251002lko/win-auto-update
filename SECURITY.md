# Security Policy

## Supported Versions

Security fixes are applied to the latest version on the default branch. Legacy Windows releases may no longer receive security updates from Microsoft.

## Reporting a Vulnerability

Please do not disclose vulnerabilities in public issues. Use GitHub's private vulnerability reporting feature for this repository when available. Include the affected version, Windows and Python versions, reproduction steps, and impact. Do not include personal data, credentials, or private update logs.

## Security Model

- The application uses the local Windows Update Agent COM API and does not contact third-party package mirrors.
- No update is installed unless the operator supplies an exact update identity, --execute, and --accept-eula.
- Installation requires an elevated Administrator process.
- Update IDs are revalidated against a fresh search before the installer is invoked.
- Interactive updates are rejected; automatic restart is not performed.
- The application does not store credentials, tokens, or update payloads.
- The PowerShell bridge is fixed application code; caller-provided values are passed as JSON rather than interpolated into script text.

Only run installation commands after reviewing the selected updates and preparing an appropriate backup and maintenance window.
