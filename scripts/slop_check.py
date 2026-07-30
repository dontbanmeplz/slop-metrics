#!/usr/bin/env python3
"""Run SlopCodeBench's erosion/verbosity metrics over a real repository.

Wraps `uvx scb-check`, working around things that silently corrupt results when
you point the upstream tool at a normal project tree:

  1. Basename collisions. scb-check keys symbols by file basename, so a repo with
     several `handler.py` / `utils.py` / `__init__.py` files silently drops
     functions and under-reports erosion. We copy sources into a temp dir under
     flattened, path-derived names so every basename is unique.
  2. Mixed languages. The bundled ast-grep rules are Python-only, so TS/JS files
     contribute LOC to the verbosity denominator but can never contribute
     numerator hits. We bucket by language and score each separately.
  3. Build output. We respect .gitignore by default, so lib/, dist/, release/
     and friends do not get scored as if they were source.

Config: `.slop-check.toml` beside the repo root, or --config PATH.

    [slop-check]
    paths   = ["."]
    exclude = ["*/tests/*"]
    mute    = ["section-banner-comment", "generic-with-any"]

Muting affects the triage listing only. The headline score is always computed
with every rule at --include-all, because that is how the published baselines
were produced; a filtered score is not comparable to anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from fnmatch import fnmatch
from pathlib import Path

# Table 2, arXiv 2603.24755v2. (mean, sd)
BASELINES = {
    "human": {"verbosity": (0.19, 0.11), "erosion": (0.34, 0.22)},
    "agent": {"verbosity": (0.44, 0.18), "erosion": (0.68, 0.20)},
}

LANGS = {
    "python": {".py"},
    "js/ts": {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"},
}

VENDOR = (
    "*/node_modules/*", "*/.venv/*", "*/venv/*", "*/__pycache__/*", "*/.git/*",
    "*/.terraform/*", "*/dist/*", "*/build/*", "*/.next/*", "*/site-packages/*",
    "*/.mypy_cache/*", "*/.pytest_cache/*", "*/.ruff_cache/*", "*/vendor/*",
    "*/coverage/*", "*/.tox/*",
)

CONFIG_NAME = ".slop-check.toml"
FINDING_RE = re.compile(r"^(info|warning|error)\[([a-z0-9-]+)\]:", re.M)


# --------------------------------------------------------------------------- git

def _git(args: list[str], cwd: Path) -> str | None:
    """None on any failure, including git not being installed at all."""
    try:
        proc = subprocess.run(["git", "-C", str(cwd), *args],
                              capture_output=True, text=True)
    except (FileNotFoundError, OSError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_sha(start: Path) -> str | None:
    """Short SHA + dirty marker, so a report can be tied to a tree state."""
    sha = _git(["rev-parse", "--short", "HEAD"], start)
    if sha is None:
        return None
    dirty = _git(["status", "--porcelain"], start)
    return sha.strip() + ("-dirty" if dirty and dirty.strip() else "")


def git_visible(start: Path) -> set[Path] | None:
    """Files git treats as project content: tracked + untracked-but-not-ignored."""
    top = _git(["rev-parse", "--show-toplevel"], start)
    if top is None:
        return None
    root = Path(top.strip())
    listing = _git(["ls-files", "-z", "--cached", "--others", "--exclude-standard"], root)
    if listing is None:
        return None
    return {root / p for p in listing.split("\0") if p}


def is_ignored(path: Path) -> bool:
    parent = path if path.is_dir() else path.parent
    try:
        proc = subprocess.run(["git", "-C", str(parent), "check-ignore", "-q", str(path)],
                              capture_output=True)
    except (FileNotFoundError, OSError):
        return False
    return proc.returncode == 0


# ------------------------------------------------------------------------ config

def load_config(explicit: Path | None, start: Path) -> tuple[dict, Path]:
    """Read .slop-check.toml from `explicit`, else walk up from `start`.

    Returns the table plus the directory it came from -- relative `paths` in the
    config resolve against that directory, not the shell's cwd, so running from a
    subdirectory still works.
    """
    path = explicit
    if path is None:
        current = start.resolve()
        while True:
            candidate = current / CONFIG_NAME
            if candidate.is_file():
                path = candidate
                break
            if (current / ".git").is_dir() or current.parent == current:
                break
            current = current.parent
    if path is None:
        return {}, start.resolve()
    if not path.is_file():
        sys.exit(f"error: config not found: {path}")
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        sys.exit(f"error: invalid TOML in {path}: {exc}")
    table = data.get("slop-check", data)
    return (table if isinstance(table, dict) else {}), path.parent.resolve()


# ----------------------------------------------------------------------- collect

def collect(roots: list[Path], excludes: tuple[str, ...],
            use_gitignore: bool = True) -> dict[str, list[Path]]:
    """Group source files by language, skipping vendored and gitignored trees.

    An explicitly named file, or a directory that is itself gitignored, is always
    scanned -- otherwise you could not measure real source that happens to sit
    behind a .gitignore entry.
    """
    buckets: dict[str, list[Path]] = {name: [] for name in LANGS}
    seen: set[Path] = set()
    # Resolved per root, not once for roots[0]: a leading FILE argument used to
    # leave `visible` as None and silently disable .gitignore for every later
    # directory, pulling build output into the score.
    cache: dict[Path, set[Path] | None] = {}

    for root in roots:
        if root.is_file():
            candidates = [root]  # named explicitly, always scanned
        else:
            visible = None
            if use_gitignore:
                if root not in cache:
                    cache[root] = git_visible(root)
                visible = cache[root]
            found = sorted(p for p in root.rglob("*") if p.is_file())
            if visible is None or is_ignored(root):
                candidates = found
            else:
                candidates = [p for p in found if p.resolve() in visible]

        for path in candidates:
            resolved = path.resolve()
            if resolved in seen:
                continue
            posix = path.as_posix()
            if any(fnmatch(posix, pat) for pat in VENDOR + excludes):
                continue
            if path.name.endswith(".d.ts"):  # type declarations, no callables
                continue
            for name, exts in LANGS.items():
                if path.suffix in exts:
                    buckets[name].append(path)
                    seen.add(resolved)
    return {k: v for k, v in buckets.items() if v}


def flatten(files: list[Path], base: Path, dest: Path) -> None:
    """Copy files under unique, path-derived basenames (see caveat 1 above)."""
    for path in files:
        try:
            rel = path.resolve().relative_to(base)
        except ValueError:
            rel = Path(path.name)
        flat = "_".join(rel.parts)
        target, n = dest / flat, 1
        while target.exists():
            target, n = dest / f"{n}_{flat}", n + 1
        shutil.copy(path, target)


# ------------------------------------------------------------------------- scb

def _scb(directory: Path, *flags: str, report: bool = True) -> str:
    """scb-check exits 1 whenever findings exist, so the code is not an error."""
    cmd = ["uvx", "scb-check", "check", *(["--report"] if report else []),
           *flags, str(directory)]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


REQUIRED_KEYS = ("verbosity", "erosion", "total_loc", "files_scanned",
                 "total_functions", "high_cc_functions")


def measure(files: list[Path], base: Path, detail: bool = True) -> dict | None:
    """Headline metrics; with `detail`, also default-severity and rule counts.

    `detail=False` costs one subprocess instead of three -- worth it for --per-file,
    which would otherwise run scb-check 3x per file.
    """
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "flat"
        dest.mkdir()
        flatten(files, base, dest)

        raw = _scb(dest, "--include-all")
        if not raw.strip():
            return None
        try:
            full = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(full, dict) or any(k not in full for k in REQUIRED_KEYS):
            print(f"  scb-check output missing expected fields "
                  f"({', '.join(k for k in REQUIRED_KEYS if k not in full)}) "
                  f"-- version mismatch?", file=sys.stderr)
            return None
        if not detail:
            return full

        strict_raw = _scb(dest)
        try:
            full["_default_severity"] = json.loads(strict_raw)["verbosity"]
        except (json.JSONDecodeError, KeyError):
            full["_default_severity"] = None

        human = _scb(dest, "--include-all", report=False)
        full["_rules"] = Counter(m.group(2) for m in FINDING_RE.finditer(human))
        return full


# ------------------------------------------------------------------------ output

def sigma(value: float, mean: float, sd: float) -> str:
    return f"{(value - mean) / sd:+.1f} SD"


def report(lang: str, rep: dict, mute: list[str], show_rules: bool) -> None:
    py = lang == "python"
    v, e = rep["verbosity"], rep["erosion"]
    print(f"\n=== {lang} ===")
    print(f"  files {rep['files_scanned']}   loc {rep['total_loc']}   "
          f"functions {rep['total_functions']}   high-CC {rep['high_cc_functions']}")
    print(f"  erosion    {e:.3f}   "
          f"(human {sigma(e, *BASELINES['human']['erosion'])}, "
          f"agent {sigma(e, *BASELINES['agent']['erosion'])})")

    if not py:
        print(f"  verbosity  {v:.3f}   NOT COMPARABLE -- rules are Python-only, "
              f"clone term alone (ast-grep hits: {rep['ast_grep_flagged_loc']})")
        return

    print(f"  verbosity  {v:.3f}   "
          f"(human {sigma(v, *BASELINES['human']['verbosity'])}, "
          f"agent {sigma(v, *BASELINES['agent']['verbosity'])})")
    print(f"             ast-grep {rep['ast_grep_flagged_loc']} loc + "
          f"clones {rep['clone_loc']} loc = {rep['verbosity_flagged_loc']} flagged")
    if rep.get("_default_severity") is not None:
        print(f"             default-severity view: {rep['_default_severity']:.3f} "
              f"(drops info-level findings; not baseline-comparable)")

    rules: Counter = rep.get("_rules", Counter())
    if not rules:
        return
    total = sum(rules.values())
    muted_hits = sum(n for r, n in rules.items() if r in mute)
    if mute:
        # Deliberately phrased as a finding count. These are NOT lines, and the
        # score above is line-based, so this share does not translate into
        # "X% of the verbosity score" -- findings vary hugely in lines covered.
        print(f"             muted: {muted_hits} of {total} findings "
              f"(count, not lines -- score above is unfiltered)")
    if show_rules:
        print("  rules fired:")
        for rule, n in rules.most_common():
            print(f"    {n:4}  {rule}{'   [muted]' if rule in mute else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--config", type=Path, metavar="PATH")
    ap.add_argument("--exclude", action="append", default=[], metavar="GLOB")
    ap.add_argument("--mute", action="append", default=[], metavar="RULE_ID",
                    help="treat a rule as noise in the triage listing")
    ap.add_argument("--rules", action="store_true", help="list every rule that fired")
    ap.add_argument("--no-gitignore", action="store_true",
                    help="score gitignored files too (build output included)")
    ap.add_argument("--per-file", action="store_true",
                    help="also score each file alone (slow: one run per file)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not shutil.which("uv"):
        print("error: `uv` not found; needed to run `uvx scb-check`", file=sys.stderr)
        return 2

    cfg, cfg_dir = load_config(args.config, Path.cwd())
    excludes = tuple(cfg.get("exclude", [])) + tuple(args.exclude)
    mute = list(dict.fromkeys(list(cfg.get("mute", [])) + args.mute))

    if args.paths:
        roots = [p.resolve() for p in args.paths]
    else:  # config paths are relative to the config file, not the cwd
        roots = [(cfg_dir / p).resolve() for p in cfg.get("paths", ["."])]
    for r in roots:
        if not r.exists():
            print(f"error: no such path: {r}", file=sys.stderr)
            return 2

    # Anchor flattened names to the roots' common ancestor rather than the shell's
    # cwd, so the same invocation from a subdirectory produces the same names.
    try:
        base = Path(os.path.commonpath([str(r) for r in roots]))
        if base.is_file():
            base = base.parent
    except ValueError:  # different drives, or an empty list
        base = Path.cwd().resolve()

    buckets = collect(roots, excludes, use_gitignore=not args.no_gitignore)
    if not buckets:
        print("error: no Python or JS/TS source files found", file=sys.stderr)
        return 2

    sha = git_sha(base)
    if not args.json:
        print(f"tree: {sha or 'not a git repo'}"
              + (f"   excludes: {', '.join(excludes)}" if excludes else ""))

    out: dict[str, dict] = {}
    for lang, files in buckets.items():
        rep = measure(files, base)
        if rep is None:
            print(f"  {lang}: scb-check produced no usable output", file=sys.stderr)
            continue
        out[lang] = rep
        if not args.json:
            report(lang, rep, mute, args.rules)

        if args.per_file and not args.json:
            print(f"  per-file ({len(files)} files):")
            rows = []
            for path in files:
                one = measure([path], base, detail=False)
                if one and one["total_loc"]:
                    rows.append((one["verbosity"], one["erosion"], one["total_loc"], path))
            for v, e, loc, path in sorted(rows, reverse=True):
                try:
                    shown = path.relative_to(base)
                except ValueError:
                    shown = path
                print(f"    verbosity={v:.3f}  erosion={e:.3f}  loc={loc:<5} {shown}")

    if args.json:
        for rep in out.values():
            rep["_rules"] = dict(rep.get("_rules", {}))  # Counter -> JSON-safe
        print(json.dumps({"tree": sha, "muted": mute, "results": out}, indent=2))
    else:
        print("\nbaselines: human 0.19 verbosity / 0.34 erosion; "
              "agent 0.44 / 0.68  (arXiv 2603.24755v2, Table 2)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
