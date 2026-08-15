"""PyInstaller entry point. Not part of the installed package — just the
one-line shim the frozen executable's Analysis starts from, equivalent to
the `factfolio = "mybroker.ui.cli:main"` console-script pip installs use.
"""

import sys

from mybroker.ui.cli import main

if __name__ == "__main__":
    sys.exit(main())
