# Security policy

Local Install Center is an early project, not a malware detector or sandbox. Install software only from publishers and repositories you trust.

## Reporting an issue

For now, report security concerns privately to the project maintainer rather than posting exploit details publicly. A public contact method will be added before wider distribution.

## Scope and limitations

- Package formats such as Flatpak and AppImage can still run code as the current user.
- pipx, Cargo, and npm packages may execute build or install hooks.
- Update archives are checked for expected paths, structure, metadata, and Python syntax, but are not signed or authenticated.
- The app never claims to verify that software is safe.
