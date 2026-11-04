# Contributing

1. Open an issue describing the bug or proposed improvement.
2. Keep the application standard-library-only and compatible with the documented Python baseline.
3. Preserve the default preview-only behavior and explicit installation safeguards.
4. Add unit tests for parsing, selection validation, error handling, and safety invariants.
5. Run python -m unittest discover -s tests -v on Windows before submitting changes.
6. Do not claim legacy Windows compatibility without testing on the actual Windows release in a disposable virtual machine.

Pull requests should explain the user impact, testing performed, and any Windows Update Agent behavior that could vary by operating system.
