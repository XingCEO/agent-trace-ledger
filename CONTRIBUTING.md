# Contributing to Agent Trace Ledger

1. Open an issue describing the input, observed report and expected behavior.
2. Keep fixtures small, synthetic and free of credentials or proprietary data.
3. Add a regression test before changing behavior.
4. Run `python3 -m unittest discover -v` and the example command from the README.
5. Update the input contract and example report if the public interface changes.

Prefer standard-library solutions. Discuss new dependencies and expanded format support first. Do not add network access, automatic code execution or destructive file writes to the analyzer without an explicit design discussion.
