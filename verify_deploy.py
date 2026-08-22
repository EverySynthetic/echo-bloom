#!/usr/bin/env python3
"""Check that every deployed script can actually resolve its imports.

deploy.sh used to report "N script(s) deployed" after copying files, which is
a statement about a copy, not about whether anything can run. On 2026-08-21
roundtable.py was deployed with `import kin_presence` and the module was not
copied with it: the deploy said success, the service crash-looped 155 times in
40 minutes, and six Kin stopped thinking. Nothing in the deploy output was
wrong -- it just described the wrong thing.

This resolves the imports instead. Nothing is executed: the files are parsed,
and each module named by a top-level import is looked up on a path that has
the deploy directory first.

Imports inside `try:` are skipped on purpose. Guarded imports are how this
codebase declares an optional dependency (psutil, cryptography, logging_setup),
and every one of them already has a working fallback.

Exit 0 when everything resolves, 1 otherwise.
"""
import ast
import importlib.util
import sys
from pathlib import Path


def pathlib_dir(f):
    return Path(f).resolve().parent


def top_level_imports(tree: ast.Module):
    """Imports that must work for the module to load at all.

    Module-level only, and not descending into Try -- a guarded import is a
    declared option, not a requirement.
    """
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name.split(".")[0], node.lineno
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import, resolved within a package
                continue
            if node.module:
                yield node.module.split(".")[0], node.lineno


def main(argv):
    if len(argv) < 2:
        print("usage: verify_deploy.py <deployed-scripts-dir> [--require mod,mod]",
              file=sys.stderr)
        return 2

    # Modules that must be present even though every import of them is guarded.
    #
    # Skipping try-wrapped imports is right in general -- that is how this
    # codebase declares an optional dependency -- but the licence gate is
    # imported that way ON PURPOSE so it fails open, which means a deploy that
    # omits license.py passes this checker clean and ships an inert gate. That
    # is precisely the bug on 2026-08-21 that shipped in the release meant to
    # fix it. A guarded import with a fallback is optional; a guarded import
    # whose fallback silently disables a feature is not. Named explicitly by
    # the caller, because only the caller knows which is which.
    required = set()
    if "--require" in argv:
        i = argv.index("--require")
        if i + 1 < len(argv):
            required = {m.strip() for m in argv[i + 1].split(",") if m.strip()}
        argv = argv[:i] + argv[i + 2:]

    if len(argv) != 2:
        print("usage: verify_deploy.py <deployed-scripts-dir> [--require mod,mod]",
              file=sys.stderr)
        return 2
    d = Path(argv[1])
    if not d.is_dir():
        print(f"not a directory: {d}", file=sys.stderr)
        return 2

    # Reconstruct the path a lifecycle script actually runs on, and nothing
    # else. This checker lives in the repo root next to kin_presence.py and
    # license.py, so the interpreter puts that directory on sys.path -- and the
    # first version of this file passed a deploy that was missing kin_presence
    # because it resolved the import against the repo copy, which is exactly
    # the file that is not there at runtime. A verifier that validates against
    # something the target cannot see is the bug it is meant to catch.
    here = str(pathlib_dir(__file__))
    sys.path = [str(d)] + [
        q for q in sys.path
        if q not in ("", ".", here, str(Path.cwd()))
    ]
    missing = []
    checked = 0

    for f in sorted(d.glob("*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
        except SyntaxError as e:
            missing.append((f.name, "<syntax error>", e.lineno, str(e)))
            continue
        checked += 1
        for mod, lineno in top_level_imports(tree):
            if mod == f.stem:
                continue
            try:
                found = importlib.util.find_spec(mod) is not None
            except (ImportError, ValueError):
                found = False
            if not found:
                missing.append((f.name, mod, lineno, "not importable from the deploy dir"))

    for mod in sorted(required):
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            missing.append((f"<required>", mod, 0,
                            "named as required but not present in the deploy dir"))

    if missing:
        print(f"  {len(missing)} unresolvable import(s):")
        for fname, mod, lineno, why in missing:
            print(f"    {fname}:{lineno}  {mod} — {why}")
        print("  These files were copied. They cannot start.")
        return 1

    print(f"  {checked} script(s) import-clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
