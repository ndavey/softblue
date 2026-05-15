"""SoftBlue — multi-modal MF bluebox tone generator."""

from .config import Config, Settings
from .engine import InvalidDigitError, ToneEngine

__version__ = "1.0.0"
__all__ = ["Config", "Settings", "ToneEngine", "InvalidDigitError", "__version__"]
