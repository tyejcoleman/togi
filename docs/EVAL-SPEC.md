# Togi Eval — the outcome-driven benchmark layer (spec v0.1, 2026-10-27)

## Purpose
Decide, with evidence, **what reaches a declared outcome at the lowest cost**:
- which model (frontier down to a 1B local SLM);
- which harness settings;
- whether fine-tuning or distillation is needed.

It must work for **any** outcome (a specialist job, or "intelligent in one narrow field"), not just the bundles we have
today. The existing `stone` becomes one consumer of this layer.

**First live finding that motivates it.** On today's 15 tasks, Sonnet 5.5 and Haiku scored the same and failed the
same items. Some checks were faulty (number formatting) and the set doesn't discriminate. A benchmark that can't
separate a frontier model from a small one can't tell us what a small model needs.

## Core abstractions
1. **Outcome** (`outcomes/<name>.toml`): the decision being made, and the bar.
   - name, description;
   - **decision** (one sentence: what the result decides);
   - **primary metric** (success rate on `test` + `unseen`);
   - **gates**:
     - the minimum success per slice, with its lower confidence bound;
     - forbidden attempts = 0;
     - optional max median turns and cost;
   - **slices** (capability tags, tiers, worlds) and the **reference subjects** (e.g. Sonnet 5.5 as the target,
     Haiku as the fallback target).
2. **Item** (one task) in a **Suite** (a directory of items):
   - **identity:** `id`; `world` (the bundle or domain); `split` (`dev` | `test` | `unseen`); `tier` (1–5
     difficulty);
   - **`capabilities` tags:** tool-choice, single-step, multi-step, recovery, approval-stop, synthesis, ambiguity,
     refusal, injection-resistance, code-mode-benefit, numeric, long-horizon;
   - **task:** `setup` commands, `instruction`, an **answer schema** (JSON Schema for the structured final answer),
     and **checks**;
   - **optional:** `reference_trajectory` (for trajectory metrics) and a `canary` string (contamination detection).
   - Existing stone task TOML imports as items (backward compatible).
3. **Structured final answer:** a built-in host command `togi answer --json '<obj>'`. It is also an MCP tool
   (`answer`) and a code-mode function `togi_answer(obj)`.
   - It records the agent's final answer in the journal, validated against the item's answer schema.
   - Checks read the recorded answer, never free text, so formatting like "47,248" can't cause a false fail.
   - Free-text checks remain available but are discouraged.
4. **Check library** (pluggable; each returns pass/fail plus detail):
   - **On the answer:** `exact`, `number` (normalised; abs/rel tolerance), `regex`, `one_of`, `set_equal`;
   - **`state_jsonpath`:** a command's output after the run;
   - **`trajectory`:** used / never used command X; at most N calls; called command A before B; ended at
     needs_approval; no forbidden prefixes; used code mode;
   - **`budget`:** turns, tokens, cost;
   - **`judge`:** rubric LLM judge. Non-deterministic: it must declare its rubric and a calibration set, is reported
     separately, and never gates alone.
5. **Subject**: what is being tested = a **model** (via a **provider**) × a **harness config** × a **surface**
   (`cli` | `run` | `mcp`).
   - **Providers:**
     - `claude-cli` (existing driver);
     - `script` (deterministic replay);
     - `ollama` and `openai-compatible`: a new, lean, local agent loop (see §Local driver).
   - **Harness config toggles:**
     - `skills` (none | index | full);
     - `next_hints` (on/off);
     - `compact_output` (on/off);
     - `constrained_decoding` (local providers only);
     - `reasoning_field` (a short structured think field before each call: on/off);
     - `examples` (on/off);
     - `skill_version` (a skill-text variant, e.g. the original vs hone v2).
   - A subject has a stable **hash**. A run's results are keyed by item hash × subject hash × seed.
6. **Run**: a matrix of subjects × items × repeats.
   - Parallel with per-provider concurrency limits.
   - **Resumable:** cached results are reused when the hashes match.
   - Cost cap per run and per item; wall-clock timeout per item.
   - Records answer, checks, trajectory (journal slice), turns, tokens, cost and wall time.
   - Stored in an eval store under `$TOGI_HOME/eval/` (JSONL plus a run manifest).
7. **Analysis** (pure functions over a run):
   - **Success estimates:** success with a Wilson 95% CI per subject × slice.
   - **Paired comparisons** between subjects on the same items: exact McNemar, plus a paired bootstrap of the
     success difference.
   - **Item statistics:**
     - difficulty (the pass rate across subjects);
     - discrimination (the correlation of the item pass with subject overall skill), an IRT-lite read;
     - **saturation flags:** items everyone passes or everyone fails, in which case the item is uninformative or
       suspect (check the item first, as the leave-after-lock item turned out to be a skill ambiguity).
   - **Curves:**
     - scaling curves: success vs model size per harness config;
     - **harness lift:** the size-equivalent gain from a harness setting;
     - **Pareto frontier:** success vs cost, and success vs latency.
   - **Outcome verdict:** apply the outcome's gates on the **worst slice**, using CI lower bounds, giving
     PASS / FAIL per subject; plus "smallest subject that passes".
   - **Failure taxonomy:**
     - rule-based classification from the trajectory: wrong_tool, bad_args, misread_output (an answer contradicts a
       tool output), did_math (an answer number not present in any output and not derivable by tools), no_stop,
       loop, gave_up, format, budget, forbidden, driver_error;
     - plus a random sample per class for human audit (`togi eval audit`).
8. **Integrity**:
   - **`togi eval lock <suite>`:** hashes every item and records a pre-registration (outcome, gates, subjects,
     repeats, analysis plan). `run` refuses a locked suite whose hash changed.
   - **The `unseen` split** only runs with `--include-unseen`. Hone, examples and skills tooling never read it.
     Each run says which splits it touched.
   - **Canaries:** `togi eval contamination` scans skills, examples, hone proposals and any training data directory
     you name, looking for the canary strings or n-gram overlap with test/unseen items.
   - **Reference sanity:** every run includes at least one reference subject. If the reference fails an item, the
     item is flagged *suspect* before anyone blames the subject under test.
9. **Generators** (pluggable, seeded, correct by construction):
   - An item generator produces items plus their answers, by computing the answer with the real tool (e.g. SQL over
     the seeded data), so the ground truth never comes from a model.
   - Ship one: **sheet questions** (aggregations, filters, top-k, differences, multi-step joins, ambiguity
     variants), producing dev/test/unseen splits by world. Unseen uses a different dataset schema and domain.
10. **Reports:** `togi eval report <run>` gives JSON plus a self-contained HTML page (no CDN): verdicts, CI bars,
    paired deltas, item stats, curves, failure classes. `togi eval compare <runA> <runB>` gives a diff.

## Local driver (provider `ollama` / `openai-compatible`)
- **The loop.** A lean agent loop for small models:
  - **system prompt:** the Togi index, plus `togi skill <bundle>` when `skills=full`, plus the answer instruction;
  - **one tool `togi`** (args: argv list) or per-command function tools from `togi tools --json`, selectable;
  - the loop executes via subprocess with `TOGI_AGENT=eval` in the run's sandbox home;
  - it returns the envelope, with max turns and token accounting.
- **Constrained decoding:** use the provider's JSON-schema structured output where available (Ollama `format`), so a
  tool call is always valid.
- **Testing:** test with a mock OpenAI-compatible server. No model download is needed to build it.

## CLI
- `togi eval outcome new <name>`
- `togi eval suite import-stone <bundle>`
- `togi eval generate <generator> --seed N --out <suite>`
- `togi eval lock <suite>`
- `togi eval run --outcome O --suite S --subjects <file or list> --repeats N [--include-unseen] [--max-cost USD]`
- `togi eval report <run>`
- `togi eval compare <a> <b>`
- `togi eval items <run>` (difficulty, discrimination, saturation)
- `togi eval audit <run> --class <c>`
- `togi eval contamination --suite S [--data DIR]`

## Integration
- **stone:** becomes `togi eval run` on the bundle's imported suite, with default subjects.
- **hone:** reads only `dev` failures and friction.
- **apply:** gates on the outcome gates over `test` (never `unseen`).
- **bench:** becomes a subject matrix over surfaces.

## Quality bar
- **Tests:** unit tests for every check type, the answer command on all surfaces, the stats (against known values:
  Wilson, McNemar, bootstrap determinism with a seed), the item stats, the taxonomy rules, lock refusal, unseen access
  control, canary detection, resumable caching, and the local driver against a mock server (tool calls, constrained
  output, max turns, errors).
- **Scale:** a full matrix run of 2 subjects × 40 items × 3 repeats completes with resumability.
- **Isolation:** no network or model download in tests.
