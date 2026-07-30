# slop-metrics

[![arXiv](https://img.shields.io/badge/arXiv-2603.24755-b31b1b.svg)](https://arxiv.org/abs/2603.24755)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Agent Skills](https://img.shields.io/badge/Agent%20Skills-Claude%20Code%20%7C%20Kiro-blueviolet)](https://code.claude.com/docs/en/skills)
[![Languages](https://img.shields.io/badge/scores-Python%20%7C%20JS%2FTS-3776AB)](#language-support)

An agent skill that scores a codebase for "slop" using the two SlopCodeBench metrics —
**structural erosion** (how much complexity is concentrated in already-complex functions) and
**verbosity** (redundant and rule-flagged lines) — from
[arXiv 2603.24755](https://arxiv.org/abs/2603.24755).

The point is the baselines. The paper measured 473 open-source Python repos and 2869 agent
checkpoints, so a single score means something:

| Group | Verbosity | Erosion |
|---|---|---|
| Human panel (473 OSS repos) | 0.19 ± 0.11 | 0.34 ± 0.22 |
| Agent checkpoints | 0.44 ± 0.18 | 0.68 ± 0.20 |

The baselines hold up remarkably well on repos the paper never saw. `psf/requests` — famously
well-maintained, human-written — lands right on the human mean:

```
$ python3 scripts/slop_check.py src
tree: 414f051

=== python ===
  files 19   loc 3588   functions 241   high-CC 14
  erosion    0.435   (human +0.4 SD, agent -1.2 SD)
  verbosity  0.166   (human -0.2 SD, agent -1.5 SD)
             ast-grep 488 loc + clones 136 loc = 594 flagged
             default-severity view: 0.059 (drops info-level findings; not baseline-comparable)
```

And [OpenClaw](https://github.com/openclaw/openclaw) — ~1.2M LOC of famously largely
agent-written TypeScript — lands *exactly* on the agent-checkpoint mean:

```
$ python3 slop_check.py src --exclude '*.test.ts' --exclude '*.spec.ts' ...
tree: 7b581ecc

=== js/ts ===
  files 7542   loc 1213479   functions 67951   high-CC 5384
  erosion    0.685   (human +1.6 SD, agent +0.0 SD)
  verbosity  0.065   NOT COMPARABLE -- rules are Python-only, clone term alone
```

One codebase written by humans, one written mostly by agents, neither in the paper's corpus —
and they land on their respective baselines to within measurement noise.

## How this came to be

I ran across [a thread by @dexhorthy](https://x.com/dexhorthy/status/2080734551912202354) on
SlopCodeBench — Orlanski et al.'s benchmark showing that coding agents don't just fail tasks,
they *degrade* codebases in ways a test suite never catches: structural erosion rose in 77% of
agent trajectories and verbosity in 75.5%. The interesting part of the paper
([arXiv 2603.24755](https://arxiv.org/abs/2603.24755)) isn't the leaderboard — it's that the
authors measured 473 real open-source repos and 2,869 agent checkpoints with the same two
metrics, which turns them into a ruler. A score on *your* repo suddenly means something: you can
say "this codebase is 1.7 SD more eroded than a typical human-maintained one" instead of vibes.

So I turned the paper into a detector: this skill wraps the authors' own `scb-check` analyzer,
fixes the sharp edges you hit when pointing it at a real project tree instead of a benchmark
checkpoint (see below), and teaches the agent how to read the result against the published
baselines — including when *not* to care.

## Why the wrapper exists

It shells out to the authors' own `scb-check` CLI, but pointing that tool straight at a project
tree gives wrong numbers that look plausible. The wrapper fixes four things:

1. **Basename collisions.** `scb-check` keys symbols by file basename, so a repo with several
   `handler.py` / `utils.py` / `__init__.py` files silently drops functions and under-reports
   erosion. Verified repro — identical content, only the name differs: `a/handler.py +
   b/handler.py` gives 30 functions / erosion 0.438; rename one and you get 35 / 0.796.
2. **Python-only rules.** All 214 ast-grep rules are `language: python`. Mixing TS/JS into one
   run inflates the verbosity denominator with lines that can never be flagged.
3. **Build output.** `lib/`, `dist/`, `release/` get scored as source unless you respect
   `.gitignore`.
4. **`--include-all`.** Required for baseline comparability, but ~97% of findings are `info`
   severity, so the wrapper prints the default-severity number alongside as a noise check.

## Language support

Each language is bucketed and scored separately, so mixed repos get one honest report per
language instead of one blended wrong number.

| Language | Extensions | Erosion | Verbosity |
|---|---|---|---|
| Python | `.py` | ✅ baseline-comparable | ✅ baseline-comparable (214 ast-grep rules + clones) |
| JS / TS | `.js` `.jsx` `.ts` `.tsx` `.mjs` `.cjs` | ✅ baseline-comparable | ⚠️ clone detection only — all 214 rules are Python-only, so the score is reported but flagged NOT COMPARABLE |
| everything else | — | not scored | not scored |

Erosion is language-agnostic (cyclomatic complexity × function size), so its baselines apply to
both buckets. `.d.ts` type declarations are skipped — they contain no callables and would only
pad the denominator. Anything not listed (Go, Rust, Java, …) is silently outside scope for now;
upstream `scb-check` would need rules and parsing for it first.

## Requirements

- `uv` on PATH (the script runs `uvx scb-check`; everything else is fetched on demand)
- Python 3.11+ (for `tomllib`)
- `git` (optional — used for `.gitignore` handling and SHA stamping)

## Install

### Claude Code — via the plugin marketplace (recommended)

This repo is its own marketplace (`.claude-plugin/marketplace.json`), so it installs in two
commands:

```
/plugin marketplace add dontbanmeplz/slop-metrics
/plugin install slop-metrics@slop-metrics
```

### Claude Code — as a skills-directory install

Clone into your skills directory and it loads on the next session, no install step:

```bash
git clone https://github.com/dontbanmeplz/slop-metrics ~/.claude/skills/slop-metrics
```

### Claude Code — local test, no install

```bash
claude --plugin-dir ./slop-metrics
```

### Kiro

Kiro implements the same Agent Skills standard, so the same directory works unchanged:

```bash
git clone https://github.com/dontbanmeplz/slop-metrics ~/.kiro/skills/slop-metrics   # global
git clone https://github.com/dontbanmeplz/slop-metrics .kiro/skills/slop-metrics     # per-workspace
```

Kiro can also import from a GitHub repo or local folder through the Agent Steering & Skills
panel. Workspace skills take priority over global ones on a name conflict.

### Any other Agent Skills host

Copy the directory into whatever skills path it uses. The only host-specific thing is where the
folder lives; `SKILL.md` and `scripts/` are portable.

## Usage

```bash
python3 <skill-dir>/scripts/slop_check.py [PATHS] [--rules] [--per-file] [--json]
```

Config is `.slop-check.toml` at the repo root, or `--config PATH`:

```toml
[slop-check]
paths   = [".", "api"]        # name gitignored dirs explicitly to include them
exclude = ["*/tests/*"]
mute    = ["section-banner-comment", "generic-with-any"]
```

`mute` affects the triage listing only. The headline score always uses every rule, because a
filtered score is not comparable to the published baselines.

## A note on reading the output

Erosion is the metric worth acting on — it usually reduces to a handful of named functions.
Verbosity is worth measuring and mostly not worth chasing; roughly 40% of hits in practice come
from taste rules like `section-banner-comment`. Open two or three findings before treating a
high verbosity score as a defect.

(Full disclosure: the wrapper script itself scores +1.7 SD on erosion. Measurement is not
absolution.)

## Credits

Metrics and baselines: Orlanski et al., *SlopCodeBench: Benchmarking How Coding Agents Degrade
Over Long-Horizon Iterative Tasks*, arXiv 2603.24755v2. Underlying analyzer:
[`SprocketLab/slop-code-bench`](https://github.com/SprocketLab/slop-code-bench) (MIT), published
as the `scb-check` CLI.
