"""DimFort: a dimensional homogeneity checker for Fortran."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("dimfort")
except PackageNotFoundError:
    # Running from a checkout without an install (e.g. tests in CI
    # before `pip install -e .` ran). Fall back to a sentinel so the
    # LSP startup string is still well-formed.
    __version__ = "0+unknown"

__all__ = ["__version__"]
