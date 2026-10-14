"""Download selected files from a Telegram chat into per-sender folders."""

try:
    # Written by the release build (see .github/workflows/build-windows.yml).
    from ._build import VERSION as __version__
except ImportError:
    __version__ = "dev"
