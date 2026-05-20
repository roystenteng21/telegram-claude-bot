#!/usr/bin/env python3
"""
deploy.py — Em deployment script
Usage: python3 ~/telegram-claude-bot/deploy.py "commit msg" "Session N" "built" "fixed" "pending"
- Auto-detects all module .py files in repo (excludes non-module files)
- Runs cross-reference check: verifies all imported functions exist in their source files
- Writes session_meta.json to repo (Railway reads this on boot to update Em Log + Module Registry)
- Commits and pushes to GitHub
- Railway auto-deploys on push
"""

import os
import sys
import ast
import json
import subprocess
from datetime import date

REPO_DIR = os.path.expanduser("~/telegram-claude-bot")
SESSION_META_PATH = os.path.join(REPO_DIR, "session_meta.json")

# Files that live in the repo but are not Em modules
NON_MODULE_FILES = {
    "deploy.py",
    "reauth.py",
    "session_meta.json",
    "requirements.txt",
    "Procfile",
    "runtime.txt",
    ".env",
}


def get_module_files():
    """Auto-detect all .py module files in the repo directory."""
    files = []
    for f in sorted(os.listdir(REPO_DIR)):
        if f.endswith(".py") and f not in NON_MODULE_FILES and not f.startswith("."):
            files.append(f)
    return files


def run(cmd, cwd=None):
    result = subprocess.run(cmd, shell=True, cwd=cwd or REPO_DIR, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"CMD: {cmd}")
        print(f"STDOUT: {result.stdout}")
        print(f"STDERR: {result.stderr}")
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            return
        raise RuntimeError(f"Command failed: {cmd}")
    return result.stdout.strip()


def check_modules():
    """Detect and report all module files. Abort if none found."""
    modules = get_module_files()
    if not modules:
        print("❌ No module files found in repo — aborting.")
        sys.exit(1)
    print(f"✅ {len(modules)} module files detected: {', '.join(modules)}")
    return modules


PARSE_FAILED = object()  # sentinel: parse failed, skip cross-ref checks against this module

def _get_defined_names(filepath):
    """Return set of all names available from a module — defined or imported.
    Returns PARSE_FAILED sentinel if the file cannot be parsed."""
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            content = f.read()
        tree = ast.parse(content, filename=filepath)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        names.add(alias.asname or alias.name)
        return names
    except Exception as e:
        print(f"  ⚠️  Could not parse {os.path.basename(filepath)}: {e} — skipping cross-ref checks for this file")
        return PARSE_FAILED


def _get_from_imports(filepath):
    """Return list of (module_name, [imported_names]) for all 'from X import ...' statements."""
    try:
        with open(filepath) as f:
            tree = ast.parse(f.read(), filename=filepath)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and not node.module.startswith("."):
                    names = [alias.name for alias in node.names if alias.name != "*"]
                    if names:
                        imports.append((node.module, names))
        return imports
    except Exception:
        return []


def get_changed_files():
    """Return set of .py filenames with uncommitted or unpushed changes."""
    changed = set()
    try:
        # Unstaged changes
        r1 = subprocess.run("git diff --name-only", shell=True, cwd=REPO_DIR, capture_output=True, text=True)
        # Staged changes
        r2 = subprocess.run("git diff --name-only --cached", shell=True, cwd=REPO_DIR, capture_output=True, text=True)
        # Unpushed commits
        r3 = subprocess.run("git diff --name-only @{u} HEAD", shell=True, cwd=REPO_DIR, capture_output=True, text=True)
        for r in [r1, r2, r3]:
            for line in r.stdout.splitlines():
                if line.strip().endswith(".py"):
                    changed.add(line.strip())
    except Exception as e:
        print(f"  ⚠️  Could not determine changed files: {e}")
    return changed


def check_module_freshness(modules):
    """Print timestamps only for files with uncommitted or unpushed changes."""
    from datetime import datetime
    changed = get_changed_files()
    relevant = [f for f in modules if f in changed]
    if not relevant:
        return changed
    now = datetime.now()
    print("\n📋 Changed file timestamps:")
    for f in relevant:
        filepath = os.path.join(REPO_DIR, f)
        try:
            mtime = os.path.getmtime(filepath)
            dt = datetime.fromtimestamp(mtime)
            age_mins = (now - dt).total_seconds() / 60
            age_str = f"{int(age_mins)}m ago" if age_mins < 60 else f"{int(age_mins/60)}h {int(age_mins%60)}m ago"
            print(f"   {f:<30} {dt.strftime('%Y-%m-%d %H:%M:%S')}  ({age_str})")
        except Exception as e:
            print(f"   {f:<30} ⚠️  Could not read timestamp: {e}")
    return changed


def check_cross_references(modules, changed_files=None):
    """
    For each module file, check that functions imported from other local modules
    actually exist in those source files. Only checks files in changed_files if provided.
    Returns True if all clean, False if issues found.
    """
    print("\n🔍 Running cross-reference check...")

    # Build map of module name -> defined names
    local_modules = {os.path.splitext(f)[0]: f for f in modules}
    defined = {}
    for mod_name, filename in local_modules.items():
        filepath = os.path.join(REPO_DIR, filename)
        defined[mod_name] = _get_defined_names(filepath)

    issues = []
    for mod_name, filename in local_modules.items():
        if changed_files is not None and filename not in changed_files:
            continue  # only check files that changed
        filepath = os.path.join(REPO_DIR, filename)
        from_imports = _get_from_imports(filepath)
        for source_module, imported_names in from_imports:
            if source_module not in local_modules:
                continue  # skip third-party imports
            source_defined = defined.get(source_module, set())
            if source_defined is PARSE_FAILED:
                continue  # skip — parse failed, already warned
            for name in imported_names:
                if name not in source_defined:
                    issues.append(
                        f"  ❌ {filename}: imports '{name}' from {source_module}.py — not found"
                    )

    if issues:
        print(f"⚠️  Cross-reference issues found ({len(issues)}):")
        for issue in issues:
            print(issue)
        print("\n  Fix these before deploying, or the bot will crash on startup.")
        return False
    else:
        print("✅ Cross-reference check passed — all imports verified")
        return True


def _read_deploy_count():
    try:
        if os.path.exists(SESSION_META_PATH):
            with open(SESSION_META_PATH) as f:
                data = json.load(f)
            return data.get("deploy_count", 0)
    except Exception:
        pass
    return 0


def write_session_meta(session_name, built, fixed, pending, modules):
    deploy_count = _read_deploy_count() + 1
    meta = {
        "date": date.today().strftime("%Y-%m-%d"),
        "session": session_name,
        "built": built,
        "fixed": fixed,
        "pending": pending,
        "deploy_count": deploy_count,
        "module_count": len(modules),
        "modules": modules,
    }
    with open(SESSION_META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"✅ session_meta.json written (deploy #{deploy_count})")
    return deploy_count


def git_commit_push(commit_msg):
    try:
        run("git add -A")
        result = subprocess.run(
            f'git commit -m "{commit_msg}"',
            shell=True, cwd=REPO_DIR, capture_output=True, text=True
        )
        if "nothing to commit" in result.stdout or "nothing to commit" in result.stderr:
            print("ℹ️  Nothing new to commit — files unchanged.")
            return False
        if result.returncode != 0:
            print(f"Git commit error: {result.stderr}")
            return False
        print(f"✅ Committed: {commit_msg}")
    except Exception as e:
        print(f"Git commit error: {e}")
        return False
    try:
        run("git push")
        print("✅ Pushed to GitHub — Railway deploying...")
        return True
    except Exception as e:
        print(f"Git push error: {e}")
        return False


def get_commit_hash():
    try:
        result = subprocess.run(
            "git rev-parse --short HEAD",
            shell=True, cwd=REPO_DIR, capture_output=True, text=True
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 deploy.py 'commit msg' ['Session N'] ['built'] ['fixed'] ['pending']")
        sys.exit(1)

    commit_msg = sys.argv[1]
    session_name = sys.argv[2] if len(sys.argv) > 2 else "Manual deploy"
    built = sys.argv[3] if len(sys.argv) > 3 else commit_msg
    fixed = sys.argv[4] if len(sys.argv) > 4 else "None"
    pending = sys.argv[5] if len(sys.argv) > 5 else "None"

    print(f"\n🚀 Deploying Em — {session_name}")
    print(f"   Commit: {commit_msg}\n")

    modules = check_modules()
    changed = check_module_freshness(modules)

    # Cross-reference check — only changed files, abort if broken imports found
    refs_ok = check_cross_references(modules, changed_files=changed)
    if not refs_ok:
        print("\n❌ Deploy aborted — fix cross-reference issues first.")
        sys.exit(1)

    write_session_meta(session_name, built, fixed, pending, modules)
    pushed = git_commit_push(commit_msg)
    commit_hash = get_commit_hash()

    if pushed:
        print(f"\n✅ Deploy complete — {commit_hash}")
        print("   Railway will restart Em in ~30 seconds.")
        print("   Em Log + Module Registry will update automatically on boot.")
    else:
        print(f"\n✅ Files ready. No new commit (files unchanged).")


if __name__ == "__main__":
    main()
