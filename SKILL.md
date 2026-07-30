---
name: slop-metrics
description: >-
  Score a codebase for "slop" using the SlopCodeBench metrics (structural erosion and
  verbosity) from arXiv 2603.24755, with published human and agent baselines to compare
  against. Use when asked how sloppy / bloated / eroded a repo is, whether code looks
  AI-generated or agent-degraded, where complexity is concentrated, whether a refactor is
  worth doing, or for any request naming slop, erosion, verbosity, SlopCodeBench, scb-check,
  code quality metrics, complexity concentration, or code smells at repo scale. Also use
  when a cyclomatic-complexity question is really a "is this codebase in bad shape" question.
---

# Slop metrics

Two point-in-time metrics from SlopCodeBench (arXiv 2603.24755v2, Orlanski et al.). They were
designed to track agent degradation across iterations, but they work on any snapshot, and the
paper ships baselines from 473 open-source Python repos that make a single score meaningful.

**Structural erosion** — how much of the complexity sits in functions that are already too complex:

```
mass(f) = CC(f) x sqrt(SLOC(f))
Erosion = sum of mass(f) where CC(f) > 10  /  sum of mass(f) over all callables
```

**Verbosity** — `|ast-grep flagged lines UNION clone lines| / LOC`, from 214 hand-written
ast-grep rules plus structural clone detection.

| Group | n | Verbosity | Erosion |
|---|---|---|---|
| Human panel (OSS Python repos, HEAD) | 473 | 0.19 ± 0.11 | 0.34 ± 0.22 |
| Agent checkpoints | 2869 | 0.44 ± 0.18 | 0.68 ± 0.20 |

## Running it

The wrapper is `scripts/slop_check.py`, bundled next to this file. Resolve it in whichever
host you are in — as a Claude Code plugin `"${CLAUDE_PLUGIN_ROOT}"/scripts/slop_check.py`, or
by path: `~/.claude/skills/slop-metrics/scripts/slop_check.py`,
`~/.kiro/skills/slop-metrics/scripts/slop_check.py`, or `.claude/skills/...` /
`.kiro/skills/...` for a project-scoped install.

```bash
python3 <skill-dir>/scripts/slop_check.py [PATHS] [--rules] [--per-file]
```

Requires `uv` on PATH (the script shells out to `uvx scb-check`) and Python 3.11+ for
`tomllib`. Everything else is fetched on demand.

The wrapper handles the failure modes below. Do not call `scb-check` directly on a project
tree without reading the next section — you will get wrong numbers that look plausible.

Useful flags: `--rules` (list every rule that fired), `--per-file` (score each file alone, the
fastest way to find where the slop actually lives), `--json`, `--exclude GLOB`, `--mute RULE`,
`--no-gitignore`.

Config is `.slop-check.toml` at the repo root, or `--config PATH`:

```toml
[slop-check]
paths   = [".", "api"]        # name gitignored dirs explicitly to include them
exclude = ["*/tests/*"]
mute    = ["section-banner-comment", "generic-with-any"]
```

## Four things that silently corrupt results

1. **Basename collisions.** `scb-check` keys symbols by file basename. A repo with several
   `handler.py` / `utils.py` / `__init__.py` files silently drops functions and under-reports
   erosion. Verified minimal repro — identical content, only the basename differs:
   `a/handler.py + b/handler.py` → 30 functions, erosion 0.438; rename one → 35 functions,
   erosion 0.796. The wrapper flattens to unique path-derived names. This also means the
   paper's own human baseline may be understated, since real repos collide on basenames far
   more than flat agent solutions do.
2. **The rules are Python-only.** All 214 rules are `language: python`. TS/JS files contribute
   LOC to the verbosity denominator but can never contribute hits, so a mixed-language run
   deflates verbosity toward zero. The wrapper buckets by language. TS/JS erosion is valid;
   TS/JS verbosity is clone-only and not baseline-comparable.
3. **`--include-all` must stay on.** The paper's harness hardcodes it
   (`slop_code/metrics/checkpoint/driver.py`), so it is the only setting comparable to the
   0.19/0.34 baselines. It is also where nearly all findings come from — typically ~97% are
   `info` severity. The wrapper prints the default-severity number alongside as a noise check;
   expect a large gap (0.35 vs 0.07 is normal). That gap is a property of the metric, not a
   problem with the code.
4. **Build output.** `lib/`, `dist/`, `release/`, `temp/` will happily get scored as source.
   The wrapper respects `.gitignore`. The flip side: genuinely-real source that is gitignored
   must be named explicitly as a path argument.

## Reading the result

Erosion is the metric worth acting on. It usually decomposes into a small number of specific
functions, and `--per-file` plus the flagged-function list names them. A single monolithic
function can dominate: in one repo a 266-line router at CC 84 was 31% of all complexity mass,
and splitting it moved whole-repo erosion from 0.58 to 0.39.

Verbosity is worth measuring and mostly not worth chasing. Roughly 40% of hits in practice come
from taste rules — `section-banner-comment` objects to decorative `# ----` banners,
`dict-get-empty-list-default` objects to `d.get(k, [])` on fresh-allocation grounds that do not
matter at normal call volumes. Before reporting a high verbosity score as a problem, open two or
three findings and confirm they are real. In one audit a `chained-dict-get` finding pointed at
JWT claim extraction that was already correct and fail-closed. Use `mute` to keep the triage
listing honest; the headline score stays unfiltered because that is what the baselines mean.

Report both numbers with their distance from the human mean in SD, say which specific functions
drive erosion, and give a recommendation rather than a list of every finding. A repo at +1 SD on
verbosity and +1 SD on erosion is not in trouble — the agent-checkpoint column is what trouble
looks like.

## Provenance

Metrics and baselines: arXiv 2603.24755v2, Section 2.3 and Table 2. Implementation:
`SprocketLab/slop-code-bench` (MIT), rules in `configs/slop_rules.yaml`, published as the
`scb-check` CLI on PyPI. The paper states 137 rules; the repo has since grown to 214, so a
current run scores slightly stricter than the published figures.

Stamp the commit SHA when reporting — the wrapper prints it. Scores drift as the tree changes,
and a number without a tree state cannot be compared to a later run.
