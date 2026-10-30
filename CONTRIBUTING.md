# Contributing to GSExplorer+

Bug reports and pull requests are welcome at https://github.com/gstvslzr/GSExplorer.

## License of contributions

GSExplorer+ is licensed under the GNU General Public License v3.0 or later. By submitting a
contribution you agree that it is licensed under the same terms (inbound = outbound), and you
certify the [Developer Certificate of Origin 1.1](https://developercertificate.org/) by adding a
`Signed-off-by:` line to each commit (`git commit -s`).

Add these two lines at the top of every new source file:

```python
# SPDX-FileCopyrightText: <year> <your name>
# SPDX-License-Identifier: GPL-3.0-or-later
```

## AI-generated contributions

Say so in the pull request if a contribution contains AI-generated code or text, and roughly how
much. You remain responsible for having reviewed and tested it. See [AI-DISCLOSURE.md](AI-DISCLOSURE.md).

## Before opening a pull request

```bash
for f in tests/test_*.py; do QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 "$f" || break; done
```

All suites must pass. New features need tests. No telemetry, no network calls outside
Connect, and no new bundled components without an entry the license inventory recognises.
