"""Copy ONYX BOOX note exports onto a Mac and render them to PDF."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("boox-sync")
except PackageNotFoundError:
    # A source checkout must be installed before building a release.
    __version__ = "0+unknown"
