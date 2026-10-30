# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please **do not** open a public issue for security problems.

Report privately through GitHub: **Security ▸ Advisories ▸ Report a vulnerability**
(https://github.com/gstvslzr/GSExplorer/security/advisories/new).

Include the version (Help ▸ About ▸ System info ▸ Copy to clipboard), steps to reproduce and
the impact you expect. You should get a first response within 7 days. Please allow up to
90 days for a fix before public disclosure.

## Scope notes

- GSExplorer+ bundles its own Python, Qt and FFmpeg under `/opt/gsexplorer-plus`. Security
  updates from your distribution do **not** reach these copies; they are updated by new
  GSExplorer+ releases. Vulnerabilities in those components are in scope when they are
  reachable through GSExplorer+.
- Passwords for network connections are stored only in the system keyring (Secret Service via
  `secret-tool`), never in GSExplorer+ files.
- Release files are published with SHA-256 checksums (`SHA256SUMS`). When a GPG signature
  (`SHA256SUMS.asc`) is attached, its key fingerprint is listed in the release notes.
