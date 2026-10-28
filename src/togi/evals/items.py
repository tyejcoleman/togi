"""Items and suites (EVAL-SPEC section 2).

A suite is a directory:

    suite.toml            name, description, canary, [bundles.<name>] path = ... (optional: else the registry), generator info
    items/<split>/*.toml  one item each, a directory per split (items/*.toml also works: its split is read from the file);
                          a split a purpose may not read is not opened at all
    data/...              files the items' setup commands load (hashed into the item hash via `data_files`)
    LOCK.json             written by `togi eval lock`

An item is a task with its identity (id, world, split, tier, capability tags), a setup, one or more SESSIONS (each a cold
start of the agent with its own instruction, optional `between` events run just before it, and optional per-session checks),
an answer schema for `togi answer`, and end-of-item checks. A plain stone task TOML is a valid item file (backward
compatible): its checks convert to typed checks, `holdout = true` becomes split `test`, anything else `dev`.
"""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import schema as jsonschema
from ..errors import TogiError
from . import tomlw
from .canon import file_sha256, hash_obj

SPLITS = ("dev", "test", "unseen")
CAPABILITIES = ("tool-choice", "single-step", "multi-step", "recovery", "approval-stop", "synthesis", "ambiguity",
                "refusal", "injection-resistance", "code-mode-benefit", "numeric", "long-horizon")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")
DEFAULT_BUDGET_TURNS = 12
CONTINUE_TEXT = "Continue the task."

# which splits a given purpose may read (EVAL-SPEC section 8): hone, examples and skills tooling never see `unseen`; a run
# sees it only with --include-unseen; only integrity tooling (lock, contamination scan) and reports read all of them
PURPOSE_SPLITS = {
    "run": ("dev", "test"),
    "apply": ("test",),
    "hone": ("dev",),
    "examples": ("dev",),
    "skills": ("dev",),
    "lock": SPLITS,
    "contamination": SPLITS,
    "report": SPLITS,
    "all": SPLITS,
}


@dataclass
class Session:
    index: int
    instruction: str
    between: list[str] = field(default_factory=list)
    checks: list[dict] = field(default_factory=list)
    budget_turns: int | None = None
    script: list[str] = field(default_factory=list)
    script_run: str | None = None
    script_mcp: list[dict] = field(default_factory=list)
    script_answer: str | None = None            # a free-text final answer (legacy stone tasks)
    script_answer_json: dict | None = None      # an object to record with `togi answer`

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"instruction": self.instruction}
        for k in ("between", "script", "script_mcp"):
            if getattr(self, k):
                d[k] = list(getattr(self, k))
        for k in ("script_run", "script_answer", "script_answer_json", "budget_turns"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.checks:
            d["check"] = [dict(c) for c in self.checks]
        return d


@dataclass
class Item:
    id: str
    world: str = "default"
    split: str = "dev"
    tier: int = 1
    capabilities: list[str] = field(default_factory=list)
    bundles: list[str] = field(default_factory=list)
    setup: list[str] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    answer_schema: dict | None = None
    checks: list[dict] = field(default_factory=list)           # end-of-item checks
    forbids: list[str] = field(default_factory=list)           # command prefixes the agent must never attempt
    budget_turns: int = DEFAULT_BUDGET_TURNS                   # per session unless the session sets its own
    reference_trajectory: list[str] = field(default_factory=list)   # command prefixes in order (trajectory metrics, oracle replay)
    reference_answer: dict | None = None
    canary: str = ""
    data_files: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    legacy: bool = False                                       # imported from a stone task (free-text checks possible)
    path: str = ""
    suite_dir: str = ""

    @property
    def multi_session(self) -> bool:
        return len(self.sessions) > 1

    @property
    def instruction(self) -> str:
        return self.sessions[0].instruction if self.sessions else ""

    def session_budget(self, s: Session) -> int:
        return s.budget_turns if s.budget_turns is not None else self.budget_turns

    def core(self) -> dict:
        """Everything that decides what this item measures. The hash covers exactly this (+ the data files' contents)."""
        return {"id": self.id, "world": self.world, "split": self.split, "tier": self.tier,
                "capabilities": sorted(self.capabilities), "bundles": list(self.bundles), "setup": list(self.setup),
                "sessions": [s.to_dict() for s in self.sessions], "answer_schema": self.answer_schema,
                "check": [dict(c) for c in self.checks], "forbid": list(self.forbids), "budget_turns": self.budget_turns,
                "reference_trajectory": list(self.reference_trajectory), "reference_answer": self.reference_answer,
                "canary": self.canary, "data_files": sorted(self.data_files)}

    def hash(self, suite_dir: str | Path | None = None) -> str:
        root = Path(suite_dir or self.suite_dir) if (suite_dir or self.suite_dir) else None
        files = {f: (file_sha256(root / f) if root else None) for f in sorted(self.data_files)}
        return hash_obj({"core": self.core(), "files": files})

    def to_toml_dict(self) -> dict:
        d: dict[str, Any] = {"id": self.id, "world": self.world, "split": self.split, "tier": self.tier,
                             "capabilities": list(self.capabilities)}
        if self.canary:
            d["canary"] = self.canary
        if self.bundles:
            d["bundles"] = list(self.bundles)
        if self.setup:
            d["setup"] = list(self.setup)
        if self.data_files:
            d["data_files"] = list(self.data_files)
        d["budget_turns"] = self.budget_turns
        if self.reference_trajectory:
            d["reference_trajectory"] = list(self.reference_trajectory)
        if self.reference_answer is not None:
            d["reference_answer"] = self.reference_answer
        if self.legacy:
            d["legacy"] = True
        if self.forbids:
            d["forbid"] = [{"command_prefix": p} for p in self.forbids]
        if not self.multi_session:
            s = self.sessions[0]
            d["instruction"] = s.instruction
            sd = s.to_dict()
            for k in ("script", "script_run", "script_mcp", "script_answer", "script_answer_json"):
                if k in sd:
                    d[k] = sd[k]
            if s.checks:
                d["check"] = [dict(c) for c in s.checks] + [dict(c) for c in self.checks]
            elif self.checks:
                d["check"] = [dict(c) for c in self.checks]
        else:
            d["session"] = [s.to_dict() for s in self.sessions]
            if self.checks:
                d["check"] = [dict(c) for c in self.checks]
        if self.answer_schema is not None:
            d["answer_schema"] = self.answer_schema
        if self.meta:
            d["meta"] = dict(self.meta)
        return d


# --------------------------------------------------------------------------- checks: legacy conversion + validation


def legacy_check(c: dict) -> dict:
    """A stone `[[check]]` (no `type`) as a typed check."""
    if "type" in c:
        return dict(c)
    if "transcript_contains" in c:
        return {"type": "transcript_contains", "text": str(c["transcript_contains"]), "legacy": True}
    if "transcript_not_contains" in c:
        return {"type": "transcript_not_contains", "text": str(c["transcript_not_contains"]), "legacy": True}
    if "cmd" in c:
        out: dict[str, Any] = {"type": "state_jsonpath", "cmd": str(c["cmd"])}
        if "expect_jsonpath" in c:
            out["path"] = str(c["expect_jsonpath"])
            for k in ("equals", "contains", "matches"):
                if k in c:
                    out[k] = c[k]
        else:
            out["exit_code"] = int(c.get("exit_code", 0))
        return out
    return dict(c)


def _split_forbids(raw: dict) -> list[str]:
    return [str(x["command_prefix"]) for x in raw.get("forbid", []) if isinstance(x, dict) and "command_prefix" in x]


def parse_item(raw: dict, *, default_bundles: list[str] | None = None, src: str = "", suite_dir: str = "",
               default_world: str = "default") -> Item:
    iid = str(raw.get("id") or Path(src).stem)
    legacy = bool(raw.get("legacy")) or ("split" not in raw and "world" not in raw and "capabilities" not in raw)
    holdout = bool(raw.get("holdout", False))
    split = str(raw.get("split") or ("test" if holdout else "dev"))
    meta = dict(raw.get("meta") or {})
    if "holdout" in raw:
        meta["holdout"] = holdout
    sessions: list[Session] = []
    if raw.get("session"):
        for i, s in enumerate(raw["session"]):
            if not isinstance(s, dict):
                raise TogiError("item_invalid", f"{iid}: each [[session]] must be a table", next="fix the item file")
            sessions.append(_parse_session(i, s, CONTINUE_TEXT if i else ""))
    else:
        if "instruction" not in raw:
            raise TogiError("item_invalid", f"{Path(src).name or iid}: missing `instruction` (or [[session]] blocks)",
                            next="fix the item file")
        s0 = _parse_session(0, raw, "")
        s0.checks = []
        sessions.append(s0)
    checks = [legacy_check(dict(c)) for c in raw.get("check", [])]
    bundles = [str(b) for b in raw.get("bundles", [])] or list(default_bundles or [])
    item = Item(
        id=iid, world=str(raw.get("world", default_world)), split=split, tier=int(raw.get("tier", 1)),
        capabilities=[str(c) for c in raw.get("capabilities", [])], bundles=bundles,
        setup=[str(s) for s in raw.get("setup", [])], sessions=sessions,
        answer_schema=raw.get("answer_schema"), checks=checks, forbids=_split_forbids(raw),
        budget_turns=int(raw.get("budget_turns", DEFAULT_BUDGET_TURNS)),
        reference_trajectory=[str(x) for x in raw.get("reference_trajectory", [])],
        reference_answer=raw.get("reference_answer"), canary=str(raw.get("canary", "")),
        data_files=[str(x) for x in raw.get("data_files", [])], meta=meta, legacy=legacy, path=src, suite_dir=suite_dir)
    for s in item.sessions:      # per-session checks written as [[session.check]] (typed or stone-style)
        s.checks = [legacy_check(c) for c in s.checks]
    return item


def _parse_session(i: int, s: dict, default_instruction: str) -> Session:
    return Session(
        index=i, instruction=str(s.get("instruction", default_instruction) or default_instruction),
        between=[str(x) for x in s.get("between", [])], checks=[dict(c) for c in s.get("check", [])],
        budget_turns=int(s["budget_turns"]) if "budget_turns" in s else None,
        script=[str(x) for x in s.get("script", [])], script_run=s.get("script_run"),
        script_mcp=[dict(x) for x in s.get("script_mcp", [])], script_answer=s.get("script_answer"),
        script_answer_json=s.get("script_answer_json"))


def validate_item(item: Item) -> list[str]:
    """Why this item cannot be trusted: a faulty check is worse than no check (it lies), so these are errors."""
    from . import checks as checklib

    problems: list[str] = []
    if not ID_RE.match(item.id):
        problems.append(f"id {item.id!r} must be letters, digits, . _ - (max 80)")
    if item.split not in SPLITS:
        problems.append(f"split must be one of {', '.join(SPLITS)}")
    if not 1 <= item.tier <= 5:
        problems.append("tier must be 1..5")
    if not item.sessions:
        problems.append("no instruction or sessions")
    if any(not s.instruction.strip() for s in item.sessions[:1]):
        problems.append("the first session needs an instruction")
    if item.answer_schema is not None:
        problems += [f"answer_schema: {p}" for p in jsonschema.check_schema(item.answer_schema)]
    all_checks = list(item.checks) + [c for s in item.sessions for c in s.checks]
    if not all_checks:
        problems.append("no checks: an item that cannot fail on its result measures nothing")
    for c in all_checks:
        problems += [f"check {c.get('type', '?')}: {p}" for p in checklib.validate_check(c, item)]
    if item.multi_session and not item.checks and not any(s.checks for s in item.sessions):
        problems.append("a multi-session item needs end-of-item checks (or per-session checks)")
    return problems


# --------------------------------------------------------------------------- suites


@dataclass
class Suite:
    name: str
    path: Path
    description: str = ""
    canary: str = ""
    bundle_paths: dict[str, str] = field(default_factory=dict)    # bundle name -> repo path ("" = resolve from the registry)
    default_bundles: list[str] = field(default_factory=list)
    generator: dict = field(default_factory=dict)
    items: list[Item] = field(default_factory=list)
    hidden: dict[str, int] = field(default_factory=dict)          # split -> number of item files NOT loaded (access control)
    splits_loaded: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    def item(self, iid: str) -> Item | None:
        return next((i for i in self.items if i.id == iid), None)

    def split_items(self, split: str) -> list[Item]:
        return [i for i in self.items if i.split == split]

    def resolve_bundle(self, name: str) -> Path:
        """The repo of a bundle named by an item: the suite's own `[bundles.<name>] path`, else the registry."""
        from .. import registry

        p = self.bundle_paths.get(name)
        if p:
            q = Path(p).expanduser()
            q = q if q.is_absolute() else (self.path / q)
            if q.is_dir():
                return q.resolve()
            raise TogiError("bundle_unresolved", f"suite {self.name}: bundle {name} is declared at {p}, which is not a directory",
                            next="fix [bundles.%s] in suite.toml, or: togi add <path>" % name)
        ent = registry.entries().get(name)
        if ent is None:
            raise TogiError("bundle_unresolved", f"suite {self.name} needs bundle {name}, which is not registered",
                            next=f"togi add <path-to-{name}-repo>   (or set [bundles.{name}] path in suite.toml)")
        return Path(ent.path)


def _read_toml(p: Path) -> dict:
    try:
        return tomllib.loads(p.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise TogiError("item_invalid", f"{p.name}: invalid TOML ({e})", next="fix the file")
    except OSError as e:
        raise TogiError("suite_not_found", f"cannot read {p} ({e.strerror or e})", next="check the path")


def suite_files(path: Path) -> dict[str, list[Path]]:
    """Item files by directory: '' = items/*.toml (the split is in the file), 'unseen' = items/unseen/*.toml."""
    root = path / "items"
    if not root.is_dir():
        root = path          # a bare directory of stone-style task files is a suite too
    out: dict[str, list[Path]] = {"": sorted(f for f in root.glob("*.toml") if f.name != "suite.toml")}
    for split in SPLITS:
        d = root / split
        if d.is_dir():
            out[split] = sorted(d.glob("*.toml"))
    return out


def load_suite(path: str | Path, *, purpose: str = "run", include_unseen: bool = False,
               splits: tuple[str, ...] | None = None) -> Suite:
    """Load a suite for a PURPOSE. Item files of a split the purpose may not read are not opened at all (unseen is only
    ever read by integrity tooling, or by a run that was given --include-unseen); a file elsewhere whose split turns out to
    be disallowed is dropped after parsing as a backstop. `hidden` says how many files were left unread."""
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise TogiError("suite_not_found", f"no suite directory at {path}", next="togi eval generate ... --out <suite>")
    if purpose not in PURPOSE_SPLITS:
        raise ValueError(f"unknown purpose {purpose!r}")
    allowed = set(splits or PURPOSE_SPLITS[purpose])
    if purpose == "run" and include_unseen:
        allowed.add("unseen")
    raw = _read_toml(root / "suite.toml") if (root / "suite.toml").is_file() else {}
    bundles = raw.get("bundles", {})
    default_bundles = [str(b) for b in raw.get("default_bundles", list(bundles))]
    suite = Suite(name=str(raw.get("name") or root.name), path=root, description=str(raw.get("description", "")),
                  canary=str(raw.get("canary", "")),
                  bundle_paths={str(k): str((v or {}).get("path", "")) if isinstance(v, dict) else str(v)
                                for k, v in bundles.items()},
                  default_bundles=default_bundles, generator=dict(raw.get("generator", {})), raw=raw)
    seen: set[str] = set()
    for where, files in suite_files(root).items():
        if where and where not in allowed:
            suite.hidden[where] = suite.hidden.get(where, 0) + len(files)
            continue
        for f in files:
            item = parse_item(_read_toml(f), default_bundles=default_bundles, src=str(f.relative_to(root)),
                              suite_dir=str(root), default_world=suite.name)
            if item.split not in allowed:
                suite.hidden[item.split] = suite.hidden.get(item.split, 0) + 1
                continue
            if item.id in seen:
                raise TogiError("item_invalid", f"duplicate item id {item.id!r} in {suite.name}", next="make ids unique")
            seen.add(item.id)
            if not item.canary:
                item.canary = suite.canary
            suite.items.append(item)
    suite.splits_loaded = sorted(allowed)
    return suite


def validate_suite(suite: Suite) -> list[str]:
    problems = []
    for item in suite.items:
        problems += [f"{item.id}: {p}" for p in validate_item(item)]
        for f in item.data_files:
            if not (suite.path / f).is_file():
                problems.append(f"{item.id}: data file {f} is missing")
    return problems


def write_item(suite_dir: Path, item: Item) -> Path:
    """items/<split>/<id>.toml: one directory per split, so a purpose that may not read a split never opens its files."""
    d = suite_dir / "items" / (item.split if item.split in SPLITS else "dev")
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{item.id}.toml"
    p.write_text(tomlw.dumps(item.to_toml_dict()), encoding="utf-8")
    return p


def write_suite_toml(suite_dir: Path, name: str, *, description: str = "", canary: str = "", bundles: dict | None = None,
                     generator: dict | None = None, default_bundles: list[str] | None = None) -> Path:
    suite_dir.mkdir(parents=True, exist_ok=True)
    d: dict[str, Any] = {"name": name, "description": description}
    if canary:
        d["canary"] = canary
    if default_bundles:
        d["default_bundles"] = default_bundles
    if generator:
        d["generator"] = generator
    if bundles:
        d["bundles"] = {k: {"path": v} for k, v in bundles.items()}
    p = suite_dir / "suite.toml"
    p.write_text(tomlw.dumps(d), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- stone import


def item_from_stone_task(task: Any, *, bundle: str, suite_dir: str = "") -> Item:
    """A stone `Task` (stone.py) as an item. holdout -> split test, otherwise dev; the checks convert to typed checks and
    `forbid` to forbidden prefixes. `budget_turns` and the scripts carry over so the script provider replays them."""
    s = Session(index=0, instruction=task.instruction, script=list(task.script), script_run=task.script_run,
                script_mcp=list(task.script_mcp), script_answer=task.script_answer)
    return Item(id=task.id, world=bundle, split="test" if task.holdout else "dev", tier=1, capabilities=[],
                bundles=[bundle], setup=list(task.setup), sessions=[s], checks=[legacy_check(dict(c)) for c in task.checks],
                forbids=list(task.forbids), budget_turns=task.budget_turns, meta={"holdout": bool(task.holdout)},
                legacy=True, path=task.path, suite_dir=suite_dir)


def import_stone(bundle: str, out: Path | None = None) -> tuple[Path, list[str]]:
    """`togi eval suite import-stone <bundle>`: write the bundle's stone tasks as a suite (items/<id>.toml + suite.toml)."""
    from .. import registry, stone

    man = registry.manifest_for(bundle)
    tasks = stone.load_tasks(man)
    dest = (out or Path.cwd() / "suites" / f"{bundle}-stone").resolve()
    write_suite_toml(dest, f"{bundle}-stone", description=f"stone tasks of {bundle}, imported", bundles={bundle: str(man.repo)},
                     default_bundles=[bundle])
    ids = []
    for t in tasks:
        item = item_from_stone_task(t, bundle=bundle)
        write_item(dest, item)
        ids.append(item.id)
    return dest, ids
