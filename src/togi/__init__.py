"""Togi: an agent-native CLI host.

    from togi import Bundle, TogiError, Context

    bundle = Bundle("crowd")

    @bundle.command("drop status", effect="read", examples=["togi crowd drop status --drop d_7f3a"])
    def drop_status(drop: str, verbose: bool = False) -> dict:
        \"\"\"Show a drop's current price, buyers, time left and state.\"\"\"
        ...
"""


def _hand_over_to_isolated_launcher() -> None:
    """`python3 -m togi` puts the current directory first on sys.path and honours PYTHONPATH, so a stdlib-named file
    planted in the directory a person stands in would run inside their `togi approve`. Importing this package is the
    first Togi code that runs under `-m`, so when that is how the interpreter was started, hand over to the isolated
    launcher right here, BEFORE this package imports any other module (every one of them is a chance for a planted
    file to be picked up). Uses only `sys` and the already-loaded `os`. Anything else that starts Togi in a normal
    interpreter (a pip console script, `python -c`) is caught by `cli.main` on the way in."""
    import sys as _sys

    if _sys.flags.isolated or not _sys.executable:
        return
    argv, i = list(getattr(_sys, "orig_argv", None) or []), 1
    rest = None
    while i < len(argv):
        tok = argv[i]
        if tok == "-m":
            rest = argv[i + 2:] if argv[i + 1: i + 2] == ["togi"] else None
            break
        if tok == "-mtogi":
            rest = argv[i + 1:]
            break
        if tok in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
        elif tok.startswith("-") and tok not in ("-c", "-"):
            i += 1
        else:
            break
    _os = _sys.modules.get("os")
    if rest is None or _os is None:
        return
    launcher = _os.path.join(_os.path.dirname(_os.path.realpath(__file__)), "_launch.py")
    try:
        _os.execv(_sys.executable, [_sys.executable, "-I", launcher, *rest])
    except OSError:
        pass   # the human gates still refuse a non-isolated interpreter


_hand_over_to_isolated_launcher()

from .bundle import Bundle, Command
from .context import Context
from .errors import NeedsApproval, TogiError

__version__ = "0.1.0"
__all__ = ["Bundle", "Command", "Context", "NeedsApproval", "TogiError", "__version__"]
