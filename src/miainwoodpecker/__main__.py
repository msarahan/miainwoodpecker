"""
Launch the live viewer with ``python -m miainwoodpecker``.

Provided alongside the ``miainwoodpecker-viewer`` console script because
this path does not depend on the entry-point script being present on
PATH, which makes it a reliable fallback when a stale or partially
installed environment cannot find the script.
"""

from miainwoodpecker.viewer.app import main

if __name__ == "__main__":
    main()
