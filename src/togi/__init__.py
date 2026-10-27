"""Togi: an agent-native CLI host.

    from togi import Bundle, TogiError, Context

    bundle = Bundle("crowd")

    @bundle.command("drop status", effect="read", examples=["togi crowd drop status --drop d_7f3a"])
    def drop_status(drop: str, verbose: bool = False) -> dict:
        \"\"\"Show a drop's current price, buyers, time left and state.\"\"\"
        ...
"""
from .bundle import Bundle, Command
from .context import Context
from .errors import NeedsApproval, TogiError

__version__ = "0.1.0"
__all__ = ["Bundle", "Command", "Context", "NeedsApproval", "TogiError", "__version__"]
