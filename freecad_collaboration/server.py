"""Command-line entry point for the local collaboration relay."""

from __future__ import annotations

import argparse

from .relay import run


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the FreeCAD collaboration relay")
    parser.add_argument("--database", default="freecad-collaboration.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    arguments = parser.parse_args(argv)
    run(arguments.database, arguments.host, arguments.port)


if __name__ == "__main__":
    main()
