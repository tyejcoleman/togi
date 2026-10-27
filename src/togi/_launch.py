"""The Togi launcher: `python3 -I _launch.py ARGS`.

This file is run as a SCRIPT and imports nothing from Togi (or anything else) until `sys.path` is what it should be.
It exists because `python3 -m togi` is not a safe way to start something a person types a passphrase into:

  - `-m` puts the current directory first on `sys.path`, so a `hmac.py` or `json.py` lying in the directory where
    the person runs `togi approve` would run inside the person's own approval session;
  - `PYTHONPATH` (and `PYTHONSTARTUP`, `PYTHONHOME`...) is honoured, so anything an agent can get into a person's
    environment is loaded too.

`-I` (isolated mode) removes both: no current directory, no script directory, no PYTHON* variables, no user site.
The launcher then adds Togi's own `src` explicitly. Bundles are imported later, from their registered repo paths,
which `registry.load_repo` adds explicitly. Started WITHOUT `-I` (someone ran `python3 _launch.py`), it re-executes
itself with it before doing anything else.
"""
import os
import sys


def _main() -> int:
    here = os.path.dirname(os.path.realpath(__file__))
    if not sys.flags.isolated:
        if not sys.executable:
            sys.stderr.write("togi: cannot find the Python executable to restart in isolated mode\n")
            return 2
        os.execv(sys.executable, [sys.executable, "-I", os.path.join(here, "_launch.py"), *sys.argv[1:]])
    sys.path.insert(0, os.path.dirname(here))   # the directory that CONTAINS the `togi` package
    from togi.cli import main

    return main()


sys.exit(_main())
