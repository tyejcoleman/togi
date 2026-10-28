"""Code-mode child process. Runs under `python -I`, stdlib only.

It never imports a bundle. Every call is forwarded as one JSON line over a socket to the parent host, which
enforces the guards, journals (surface "run") and executes. `import togi` inside the script resolves to a tiny
module holding only TogiError and NeedsApproval.

argv: <errors.py path> <socket fd> <script path> <json list of bundle names>
"""
import importlib.util
import itertools
import json
import socket
import sys
import traceback
import types


MAX_MESSAGE = 1000   # an error message the child reports to the host; the host caps it again


def cut(text, limit=MAX_MESSAGE):
    s = text if isinstance(text, str) else str(text)
    return s if len(s) <= limit else s[:limit] + f"...[+{len(s) - limit} chars]"


def main():
    errors_path, fd, script_path, names_json = sys.argv[1:5]
    spec = importlib.util.spec_from_file_location("togi", errors_path)
    togi = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(togi)
    sys.modules["togi"] = togi
    sock = socket.socket(fileno=int(fd))
    rf = sock.makefile("r", encoding="utf-8", newline="\n")
    wf = sock.makefile("w", encoding="utf-8", newline="\n")
    ids = itertools.count(1)

    def default(o):
        import datetime
        import decimal

        if isinstance(o, decimal.Decimal):
            return str(o)
        if isinstance(o, (datetime.datetime, datetime.date, datetime.time)):
            return o.isoformat()
        if isinstance(o, (set, frozenset)):
            return sorted(o, key=str)
        return str(o)

    def rpc(msg):
        msg["id"] = next(ids)
        wf.write(json.dumps(msg, default=default) + "\n")
        wf.flush()
        line = rf.readline()
        if not line:
            raise RuntimeError("the togi host closed the connection")
        return json.loads(line)

    def unwrap(resp):
        if resp.get("ok"):
            return resp.get("data")
        err = resp.get("error") or {}
        status = resp.get("status", "error")
        if status == "needs_approval":
            appr = resp.get("approval") or {}
            raise togi.NeedsApproval(appr.get("id", "?"), err.get("message"), approval=appr, next=resp.get("next"))
        raise togi.TogiError(err.get("code", "error"), err.get("message", ""), next=resp.get("next"),
                             status=status if status in ("error", "refused") else "error")

    class BundleProxy(types.ModuleType):
        def __init__(self, name):
            super().__init__(name, f"togi bundle {name}: calls run in the host, with its guards")
            self.__dict__["_loaded"] = False

        def _load(self):
            if self.__dict__["_loaded"]:
                return
            resp = rpc({"op": "describe", "bundle": self.__name__})
            if not resp.get("ok"):
                unwrap(resp)
            for meta in resp["data"]:
                self.__dict__[meta["api"]] = make_fn(self.__name__, meta)
            self.__dict__["_loaded"] = True

        def __getattr__(self, item):
            if item.startswith("__"):
                raise AttributeError(item)
            self._load()
            try:
                return self.__dict__[item]
            except KeyError:
                names = sorted(k for k in self.__dict__ if not k.startswith("_"))
                raise AttributeError(f"bundle {self.__name__!r} has no command {item!r}; commands: "
                                     + ", ".join(names)) from None

        def __dir__(self):
            self._load()
            return sorted(k for k in self.__dict__ if not k.startswith("_"))

    def make_fn(bundle, meta):
        names = [p["name"] for p in meta["params"]]

        def fn(*args, approval=None, dry_run=False, **kwargs):
            if len(args) > len(names):
                raise TypeError(f"{bundle}.{meta['api']}() takes {len(names)} arguments ({', '.join(names)})")
            for n, v in zip(names, args):
                if n in kwargs:
                    raise TypeError(f"{bundle}.{meta['api']}() got multiple values for {n!r}")
                kwargs[n] = v
            return unwrap(rpc({"op": "call", "bundle": bundle, "command": meta["command"], "args": kwargs,
                               "approval": approval, "dry_run": dry_run}))

        fn.__name__ = meta["api"]
        fn.__qualname__ = f"{bundle}.{meta['api']}"
        fn.__doc__ = f"{meta['summary']}\n\neffect: {meta['effect']}   signature: {meta['stub']}"
        return fn

    def togi_answer(obj):
        """Record your final structured answer (the same as `togi answer --json`). Raises togi.TogiError if it is refused."""
        return unwrap(rpc({"op": "answer", "answer": obj}))

    def task_fn(sub):
        def fn(**kwargs):
            return unwrap(rpc({"op": "task", "sub": sub, "args": kwargs}))
        fn.__name__ = f"togi_task_{sub}"
        return fn

    names = json.loads(names_json)
    scope = {"__name__": "__main__", "__file__": script_path, "togi": togi, "togi_answer": togi_answer,
             "togi_task_show": task_fn("show"), "togi_task_start": task_fn("start"), "togi_task_update": task_fn("update")}
    for name in names:
        alias = name.replace("-", "_")
        proxy = BundleProxy(name)
        scope[alias] = proxy
        if alias not in sys.stdlib_module_names and alias not in sys.modules:
            sys.modules[alias] = proxy  # so `import crowd` works too
    code = None
    try:
        with open(script_path, encoding="utf-8") as f:
            code = compile(f.read(), script_path, "exec")
        exec(code, scope)
    except SystemExit:
        raise
    except togi.TogiError as e:
        rpc_fatal = {"op": "fatal", "kind": "needs_approval" if isinstance(e, togi.NeedsApproval) else "togi_error",
                     "code": cut(e.code, 80), "message": cut(e.message), "next": cut(e.next) if e.next else None,
                     "status": e.status, "approval": getattr(e, "approval", None)}
        wf.write(json.dumps(rpc_fatal) + "\n")
        wf.flush()
        sys.exit(3 if e.status == "needs_approval" else (2 if e.status == "refused" else 1))
    except BaseException as e:  # script bug: show the traceback without togi's own frames
        frames = [f for f in traceback.extract_tb(e.__traceback__) if f.filename != __file__]
        sys.stderr.write(cut("Traceback (most recent call last):\n" + "".join(traceback.format_list(frames))
                             + "".join(traceback.format_exception_only(type(e), e)), 8000) + "\n")
        wf.write(json.dumps({"op": "fatal", "kind": "exception", "code": "script_error",
                             "message": cut(f"{type(e).__name__}: {e}")}) + "\n")
        wf.flush()
        sys.exit(1)


main()
