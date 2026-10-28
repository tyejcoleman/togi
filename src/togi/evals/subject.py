"""Subjects: what is being tested = a model (via a provider) x a harness config x a surface (EVAL-SPEC section 5).

A subject has a stable hash over exactly what can change its behaviour (provider, model, surface, the harness config that
provider actually uses, sampling options, the skill variant's file contents). Labels, sizes, prices and endpoints are not in
it: moving a local server or renaming a subject must not invalidate cached results. A run's results are keyed by
item hash x subject hash x seed.

Subjects come from a TOML file (`[[subject]]`, optionally with a `[subject.matrix]` of lists that expands to the cross
product, e.g. all four loops) or from a comma-separated shorthand: `provider:model[@surface][+key=value...]`, e.g.
`ollama:qwen2.5:7b+loop=operator@cli,claude-cli:haiku,script`.
"""
from __future__ import annotations

import itertools
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import TogiError
from .canon import file_sha256, hash_obj

PROVIDERS = ("claude-cli", "claude-step", "script", "ollama", "openai-compatible")
LOOP_PROVIDERS = ("claude-step", "ollama", "openai-compatible")       # run OUR agent loop, the model is the per-step policy
LOCAL_PROVIDERS = ("ollama", "openai-compatible")
SURFACES = ("cli", "run", "mcp")
ROLES = ("candidate", "reference", "target", "fallback")
SANITY_ROLES = ("reference", "target")          # failing an item flags the item as suspect (integrity)

LOOPS = ("react", "operator", "plan-act-verify", "code-first")
LOOP_CONTEXT = {"react": "full", "operator": "state-only", "plan-act-verify": "window:6", "code-first": "full"}

HARNESS_DEFAULTS: dict[str, Any] = {
    "skills": "index",                 # none | index | full
    "next_hints": True,
    "compact_output": True,
    "constrained_decoding": True,      # local providers only
    "reasoning_field": False,          # a short structured `think` field before each call
    "examples": False,                 # command examples in the system prompt
    "skill_version": "default",        # label of a skill-text variant; skill_files below say which files
    "loop": "react",                   # loop providers only
    "context_policy": "auto",          # full | window:N | state-only | auto (the loop's default)
    "step_budget": 20,                 # model calls per session (loop providers only)
    "tools": "single",                 # single (one `togi` tool) | per-command (loop providers only)
}
# which harness keys each provider actually reads (the rest are dropped from the hash so they cannot fork it)
USES = {
    "claude-cli": ("skills", "next_hints", "compact_output", "examples", "skill_version"),
    "script": (),
    "claude-step": ("skills", "next_hints", "compact_output", "reasoning_field", "examples", "skill_version", "loop",
                    "context_policy", "step_budget", "tools"),
    "ollama": tuple(HARNESS_DEFAULTS), "openai-compatible": tuple(HARNESS_DEFAULTS),
}
SKILLS = ("none", "index", "full")
IDENTITY_OPTIONS = ("temperature", "top_p", "top_k", "max_tokens", "num_ctx", "variant")   # change behaviour: in the hash
INFRA_OPTIONS = ("base_url", "api_key_env", "timeout_s", "keep_alive")                       # where it runs: not in the hash
SUBJECT_KEYS = ("id", "provider", "model", "surface", "role", "size", "price", "harness", "options", "skill_files", "matrix")


@dataclass
class Subject:
    id: str
    provider: str
    model: str | None = None
    surface: str = "cli"
    harness: dict = field(default_factory=dict)       # normalised: only the keys this provider uses, all filled in
    options: dict = field(default_factory=dict)
    role: str = "candidate"
    size: float | None = None                         # parameters in billions (scaling curves); None if unknown
    price: dict | None = None                         # {"in_per_mtok": usd, "out_per_mtok": usd} to cost token counts
    skill_files: dict = field(default_factory=dict)   # bundle -> path of a SKILL.md variant (skill_version)
    ignored: list[str] = field(default_factory=list)  # harness keys given but not used by this provider

    def identity(self) -> dict:
        h = dict(self.harness)
        if self.skill_files:
            h["skill_digests"] = {b: file_sha256(Path(p).expanduser()) for b, p in sorted(self.skill_files.items())}
        return {"provider": self.provider, "model": self.model, "surface": self.surface, "harness": h,
                "options": {k: self.options[k] for k in IDENTITY_OPTIONS if k in self.options}}

    @property
    def hash(self) -> str:
        return hash_obj(self.identity())

    @property
    def uses_loop(self) -> bool:
        return self.provider in LOOP_PROVIDERS

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    def harness_key(self) -> str:
        return harness_key(self.to_dict())

    def to_dict(self) -> dict:
        return {"id": self.id, "provider": self.provider, "model": self.model, "surface": self.surface,
                "harness": dict(self.harness), "options": dict(self.options), "role": self.role, "size": self.size,
                "price": self.price, "skill_files": dict(self.skill_files), "hash": self.hash}


def harness_key(d: dict) -> str:
    """Hash of the harness config alone (everything but the model): groups subjects into one scaling curve. Loop providers
    share a kind, so a Claude model and a local model under the same loop and settings sit on the same curve."""
    kind = "loop" if d.get("provider") in LOOP_PROVIDERS else d.get("provider")
    return hash_obj({"kind": kind, "surface": d.get("surface"), "harness": d.get("harness")})[:12]


def _bad(msg: str) -> TogiError:
    return TogiError("subject_invalid", msg, next="togi eval run --help")


def normalize_harness(provider: str, given: dict) -> tuple[dict, list[str]]:
    unknown = [k for k in given if k not in HARNESS_DEFAULTS]
    if unknown:
        raise _bad(f"unknown harness option {unknown[0]!r}; known: {', '.join(HARNESS_DEFAULTS)}")
    used = USES[provider]
    full = {**HARNESS_DEFAULTS, **given}
    if provider == "claude-step":
        full["constrained_decoding"] = False       # `claude -p` has no schema-constrained decoding: not a knob here
    if full["skills"] not in SKILLS:
        raise _bad(f"skills must be one of {', '.join(SKILLS)}")
    if full["loop"] not in LOOPS:
        raise _bad(f"loop must be one of {', '.join(LOOPS)}")
    cp = str(full["context_policy"])
    if cp == "auto":
        cp = LOOP_CONTEXT[full["loop"]]
    if not (cp in ("full", "state-only") or re.fullmatch(r"window:[1-9]\d{0,2}", cp)):
        raise _bad("context_policy must be full, window:N or state-only")
    full["context_policy"] = cp
    if full["tools"] not in ("single", "per-command"):
        raise _bad("tools must be single or per-command")
    if not isinstance(full["step_budget"], int) or isinstance(full["step_budget"], bool) or not 1 <= full["step_budget"] <= 200:
        raise _bad("step_budget must be an integer 1..200")
    for k in ("next_hints", "compact_output", "constrained_decoding", "reasoning_field", "examples"):
        if not isinstance(full[k], bool):
            raise _bad(f"{k} must be true or false")
    ignored = [k for k in given if k not in used]
    return {k: full[k] for k in used}, ignored


def make_subject(d: dict, base: Path | None = None) -> Subject:
    unknown = [k for k in d if k not in SUBJECT_KEYS]
    if unknown:
        raise _bad(f"unknown subject key {unknown[0]!r}; known: {', '.join(SUBJECT_KEYS)}")
    provider = d.get("provider")
    if provider not in PROVIDERS:
        raise _bad(f"provider must be one of {', '.join(PROVIDERS)} (got {provider!r})")
    surface = d.get("surface", "cli")
    if surface not in SURFACES:
        raise _bad(f"surface must be one of {', '.join(SURFACES)}")
    role = d.get("role", "candidate")
    if role not in ROLES:
        raise _bad(f"role must be one of {', '.join(ROLES)}")
    model = d.get("model")
    if provider in LOCAL_PROVIDERS + ("claude-step",) and not model:
        raise _bad(f"provider {provider} needs a model")
    harness, ignored = normalize_harness(provider, dict(d.get("harness") or {}))
    options = dict(d.get("options") or {})
    bad = [k for k in options if k not in IDENTITY_OPTIONS + INFRA_OPTIONS]
    if bad:
        raise _bad(f"unknown option {bad[0]!r}; known: {', '.join(IDENTITY_OPTIONS + INFRA_OPTIONS)}")
    skill_files = {}
    for b, p in (d.get("skill_files") or {}).items():
        q = Path(p).expanduser()
        q = q if q.is_absolute() or base is None else (base / q)
        if not q.is_file():
            raise _bad(f"skill_files.{b}: {p} is not a file")
        skill_files[str(b)] = str(q)
    size = d.get("size")
    if size is None and provider in LOCAL_PROVIDERS and model:
        m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model.lower())
        size = float(m.group(1)) if m else None
    sid = str(d.get("id") or (f"{provider}:{model}" if model else provider))
    if provider in LOOP_PROVIDERS and "harness" in d and "loop" in d["harness"] and "id" not in d:
        sid += f"+loop={harness['loop']}"
    return Subject(id=sid, provider=provider, model=model, surface=surface, harness=harness, options=options, role=role,
                   size=float(size) if size is not None else None, price=d.get("price"), skill_files=skill_files,
                   ignored=ignored)


def _expand(entry: dict) -> list[dict]:
    """A `[subject.matrix]` of lists -> one subject per combination (keys are harness options)."""
    matrix = entry.get("matrix")
    if not matrix:
        return [{k: v for k, v in entry.items() if k != "matrix"}]
    keys = sorted(matrix)
    for k in keys:
        if not isinstance(matrix[k], list) or not matrix[k]:
            raise _bad(f"matrix.{k} must be a non-empty list")
    out = []
    base = {k: v for k, v in entry.items() if k != "matrix"}
    for combo in itertools.product(*(matrix[k] for k in keys)):
        d = dict(base)
        d["harness"] = {**(base.get("harness") or {}), **dict(zip(keys, combo))}
        stem = str(base.get("id") or (f"{base.get('provider')}:{base.get('model')}" if base.get("model") else base.get("provider")))
        d["id"] = stem + "".join(f"+{k}={v}" for k, v in zip(keys, combo))
        out.append(d)
    return out


def parse_shorthand(text: str) -> dict:
    """`provider:model[+k=v...][@surface]` -> a subject dict. Only the first colon splits provider from model (ollama tags
    like `qwen2.5:7b` contain more)."""
    surface = None
    if "@" in text:
        text, surface = text.rsplit("@", 1)
    head, *opts = text.split("+")
    provider, _, model = head.partition(":")
    d: dict[str, Any] = {"provider": provider.strip()}
    if model.strip():
        d["model"] = model.strip()
    if surface:
        d["surface"] = surface.strip()
    harness: dict[str, Any] = {}
    for o in opts:
        k, eq, v = o.partition("=")
        if not eq:
            raise _bad(f"in {text!r}: +{o} should be +key=value")
        k, v = k.strip(), v.strip()
        if k in ("role", "id"):
            d[k] = v
        elif k == "size":
            d["size"] = float(v)
        else:
            harness[k] = _coerce(v)
    if harness:
        d["harness"] = harness
    return d


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "on", "yes"):
        return True
    if v.lower() in ("false", "off", "no"):
        return False
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return v


def load_subjects(spec: str, base: Path | None = None) -> list[Subject]:
    """`spec` is a TOML file with `[[subject]]` entries, or a comma-separated shorthand list."""
    p = Path(spec).expanduser()
    entries: list[dict]
    if p.is_file():
        try:
            raw = tomllib.loads(p.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise _bad(f"{p.name}: invalid TOML ({e})")
        entries = [e for e in raw.get("subject", []) if isinstance(e, dict)]
        if not entries:
            raise _bad(f"{p.name} has no [[subject]] entries")
        base = base or p.parent
    else:
        entries = [parse_shorthand(s) for s in spec.split(",") if s.strip()]
        if not entries:
            raise _bad("no subjects given")
    subjects = [make_subject(d, base) for e in entries for d in _expand(e)]
    ids = [s.id for s in subjects]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        raise _bad(f"duplicate subject id {sorted(dup)[0]!r}; give each subject its own `id`")
    hashes = [s.hash for s in subjects]
    if len(set(hashes)) != len(hashes):
        same = [s.id for s in subjects if hashes.count(s.hash) > 1]
        raise _bad(f"subjects {', '.join(same)} are identical (same hash); remove the duplicate")
    return subjects
