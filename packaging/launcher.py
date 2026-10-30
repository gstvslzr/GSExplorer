# SPDX-FileCopyrightText: 2026 Gus Salazar
# SPDX-License-Identifier: GPL-3.0-or-later
"""PyInstaller entry point for GSExplorer+."""
import sys

from gsexplorer.main import main

if __name__ == "__main__":
    sys.exit(main())
