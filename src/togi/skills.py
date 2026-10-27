"""Skills: markdown served by the CLI itself, index -> bundle -> topic (progressive disclosure)."""
from __future__ import annotations

import re
from pathlib import Path

from . import registry
from .manifest import Manifest, ManifestError, load_manifest

INDEX_HEAD = """\
Togi: one CLI host for tool bundles. Learn a tool in one call, use it with compact JSON.
Conventions
- Learn a bundle: `togi skill <bundle>`. One topic: `togi skill <bundle> <topic>`; list them with `--topics`.
- See commands as Python stubs: `togi tools <bundle>` (`--json` for JSON Schema).
- Run one: `togi <bundle> <words> --flag value`. Add `--help` for that command's flags.
- Output is one-line JSON: {"ok":true,"data":...} or {"ok":false,"status","error":{"code","message"},"next"}.
- Exit codes: 0 ok, 1 error, 2 refused, 3 needs approval, 4 internal bug. Every error has a `next`: run it.
- Trim output: `--fields a,b.c`; page lists with `--limit N --offset N` (default 50); `--full` lifts the ~8 KB cap.
- `--dry-run` previews write/money/admin commands without doing them.
- Money commands need a person: you get an approval id and exit 3. Ask them to run `togi approve <id>`, then
  re-run the same command with `--approval <id>`. Never try to approve yourself.
- Many calls at once: `togi run script.py` (or `-` for stdin). Each bundle is a module: `crowd.drop_status(drop="d1")`.
  Only what the script prints comes back. A money call raises togi.NeedsApproval.
Bundles"""


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """YAML-ish frontmatter: `key: value` lines between two `---` fences."""
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)\Z", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("\"'")
    return meta, m.group(2)


def index_text() -> str:
    lines = [INDEX_HEAD]
    ents = registry.entries()
    if not ents:
        lines.append("(none registered) Add one: `togi add <path-to-bundle-repo>`")
    for name in sorted(ents):
        try:
            man = load_manifest(ents[name].path)
            lines.append(f"- {name}: {man.summary or '(no summary)'}")
        except ManifestError:
            lines.append(f"- {name}: (manifest unreadable; run `togi doctor`)")
    return "\n".join(lines) + "\n"


def _topics(man: Manifest) -> list[dict]:
    d = man.repo / man.skills
    out = []
    if d.is_dir():
        for f in sorted(d.glob("*.md")):
            if f.name.lower() == "skill.md":
                continue
            meta, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
            out.append({"topic": meta.get("name") or f.stem, "summary": meta.get("summary", ""), "file": f.name})
    return out


def topics(man: Manifest) -> list[dict]:
    return _topics(man)


def bundle_text(man: Manifest, tools_text: str | None = None) -> str:
    d = man.repo / man.skills
    main = d / "SKILL.md"
    if main.is_file():
        _, body = parse_frontmatter(main.read_text(encoding="utf-8"))
        text = body.strip() + "\n"
    else:
        text = f"# {man.name}\n\n{man.summary}\n\n"
        if tools_text:
            text += "Commands:\n" + tools_text + "\n"
        text += f"\nNo SKILL.md found at {man.skills}/SKILL.md; this page was generated from the commands.\n"
    ts = _topics(man)
    if ts:
        text += "\nTopics (`togi skill " + man.name + " <topic>`):\n" + "\n".join(
            f"- {t['topic']}: {t['summary']}".rstrip(": ") for t in ts) + "\n"
    return text


def topic_text(man: Manifest, topic: str) -> str | None:
    for t in _topics(man):
        if t["topic"] == topic or Path(t["file"]).stem == topic:
            _, body = parse_frontmatter((man.repo / man.skills / t["file"]).read_text(encoding="utf-8"))
            return body.strip() + "\n"
    return None
