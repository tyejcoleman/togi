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
import sys

# The three modules that exist only on Windows are closed before anything else is imported. The standard library probes for them
# (`subprocess` does `try: import msvcrt, _winapi`, `mimetypes` does `try: import winreg`) and carries on without; on every other
# system the probe walks all of sys.path, past site-packages to wherever a `.pth` file put another project's source, and a file of
# that name there would run in the process that receives the approver's passphrase (`togi approve`, the approval desk). `-I` does not
# stop that: the path entries are not the current directory or PYTHONPATH. A `None` in sys.modules makes the import fail where it stands.
if sys.platform != "win32":
    sys.modules.update(dict.fromkeys(("winreg", "_winapi", "msvcrt"), None))

# Togi uses `tomllib` and other 3.11 features. On an older python3 (the one that ships with macOS is 3.9) the first import would
# end in a traceback about a missing module, so say what is wrong instead. This runs before any import but `sys`, uses only `sys`,
# and is written so that it parses on old interpreters too.
if sys.version_info < (3, 11):
    sys.stderr.write(
        "togi: Togi needs Python 3.11 or newer, and python3 here is %d.%d.%d (%s).\n"
        "Install a newer Python (on macOS: brew install python@3.12) and make sure `python3` on your PATH is that one.\n"
        % (sys.version_info[0], sys.version_info[1], sys.version_info[2], sys.executable or "unknown path"))
    sys.exit(1)

import os  # noqa: E402


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
