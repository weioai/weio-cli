"""Agent tools: the actions the coding agent can take on the user's machine.

Every filesystem path is confined to the project root (no escaping via .. or
absolute paths outside root). Writes and shell commands are gated by the agent's
approval mode (handled in agent.py); the tools themselves just execute.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ToolResult:
    ok: bool
    output: str                 # fed back to the model (observation)
    display: str = ""           # short human line for the TUI (defaults to output)
    # For edits/writes, carry the diff/preview so the UI can render it:
    path: Optional[str] = None
    old_text: Optional[str] = None
    new_text: Optional[str] = None
    created: bool = False

    def __post_init__(self):
        if not self.display:
            self.display = self.output[:200]


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #

def _resolve(root: Path, rel: str) -> Optional[Path]:
    """Resolve rel under root; return None if it escapes root."""
    if rel is None:
        return None
    p = (root / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


# --------------------------------------------------------------------------- #
# Read-only tools
# --------------------------------------------------------------------------- #

def read_file(root: Path, path: str, max_bytes: int = 40_000,
              start: int = None, end: int = None) -> ToolResult:
    fp = _resolve(root, path)
    if fp is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    if not fp.is_file():
        return ToolResult(False, f"Not a file: {path}")
    try:
        text = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return ToolResult(False, f"Cannot read {path}: {e}")
    # Optional 1-based line range — read just part of a large file.
    if start or end:
        lines = text.splitlines()
        s = max(1, int(start or 1))
        e = min(len(lines), int(end or len(lines)))
        body = "\n".join(f"{i}\t{lines[i-1]}" for i in range(s, e + 1))
        return ToolResult(True, body or "(no lines in range)",
                          display=f"read {path} L{s}-{e}")
    truncated = len(text) > max_bytes
    body = text[:max_bytes] + ("\n… [truncated — use start/end line args to read more]" if truncated else "")
    return ToolResult(True, body, display=f"read {path} ({len(text)} bytes)")


def list_dir(root: Path, path: str = ".") -> ToolResult:
    dp = _resolve(root, path or ".")
    if dp is None or not dp.is_dir():
        return ToolResult(False, f"Not a directory: {path}")
    entries = []
    for child in sorted(dp.iterdir(), key=lambda c: (c.is_file(), c.name)):
        if child.name.startswith(".") and child.name not in (".env.example",):
            continue
        entries.append(child.name + ("/" if child.is_dir() else ""))
    rel = dp.relative_to(root.resolve())
    return ToolResult(True, "\n".join(entries) or "(empty)",
                      display=f"list {rel or '.'} ({len(entries)} entries)")


# Directories that are never worth grepping (huge, generated, or system). Keeps the
# search from hanging when the agent is run in a home dir (AppData, Library, …).
_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env", "__pycache__",
    "dist", "build", ".next", ".nuxt", ".cache", ".pytest_cache", ".mypy_cache",
    "target", "vendor", ".gradle", ".idea", ".vs", "bin", "obj", "Pods", ".tox",
    "site-packages", ".cargo", ".rustup", ".npm", ".yarn", ".pnpm-store", ".terraform",
    # Windows / macOS user-dir black holes
    "AppData", "Application Data", "Library", "$RECYCLE.BIN", "OneDrive", "Pictures",
    "Music", "Videos", "Saved Games", "NTUSER.DAT",
}
_SEARCH_TIME_BUDGET = 8.0       # seconds, wall-clock
_SEARCH_MAX_FILES   = 4000      # files actually opened
_SEARCH_MAX_FILE_BYTES = 2 * 1024 * 1024   # skip files larger than this


def search_files(root: Path, query: str, path: str = ".", max_hits: int = 40) -> ToolResult:
    import time as _time
    base = _resolve(root, path or ".")
    if base is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    if not query:
        return ToolResult(False, "search needs a non-empty query.")
    hits = []
    scanned = 0
    t0 = _time.time()
    stopped = None
    for dirpath, dirnames, filenames in os.walk(base):
        # Prune skip dirs + hidden dirs (don't descend into .foo) in place.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if _time.time() - t0 > _SEARCH_TIME_BUDGET:
                stopped = "time"; break
            if scanned >= _SEARCH_MAX_FILES:
                stopped = "files"; break
            fp = Path(dirpath) / fn
            try:
                if fp.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                    continue
                with open(fp, "rb") as fh:
                    head = fh.read(1024)
                    if b"\x00" in head:        # binary file — skip
                        continue
                    rest = fh.read()
                text = (head + rest).decode("utf-8", errors="ignore")
                scanned += 1
                for i, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        rel = fp.relative_to(root.resolve())
                        hits.append(f"{rel}:{i}: {line.strip()[:160]}")
                        if len(hits) >= max_hits:
                            stopped = "hits"; break
            except (OSError, ValueError):
                continue
            if stopped:
                break
        if stopped:
            break
    note = ""
    if stopped == "time":
        note = f"\n[stopped after {_SEARCH_TIME_BUDGET:.0f}s — narrow the path or query]"
    elif stopped == "files":
        note = f"\n[stopped after scanning {_SEARCH_MAX_FILES} files — narrow the path]"
    body = ("\n".join(hits) or "(no matches)") + note
    return ToolResult(True, body, display=f"search '{query}' ({len(hits)} hits, {scanned} files)")


def find_files(root: Path, name: str, path: str = ".", max_hits: int = 60) -> ToolResult:
    """Locate files/directories whose NAME matches `name` (substring or glob like
    *.py / weio_ops). Fast — only inspects names, never file contents — so it's the
    right way to LOCATE a project/file. Bounded by time + entry count."""
    import time as _time, fnmatch
    base = _resolve(root, path or ".")
    if base is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    if not name:
        return ToolResult(False, "find needs a name or pattern.")
    pat = name if any(c in name for c in "*?[") else f"*{name}*"
    low = pat.lower()
    hits, scanned, t0, stopped = [], 0, _time.time(), None
    rootres = root.resolve()
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for d in dirnames:
            if fnmatch.fnmatch(d.lower(), low):
                hits.append(str((Path(dirpath) / d).relative_to(rootres)) + "/")
        for f in filenames:
            if fnmatch.fnmatch(f.lower(), low):
                hits.append(str((Path(dirpath) / f).relative_to(rootres)))
        scanned += 1
        if len(hits) >= max_hits:
            stopped = "hits"; break
        if _time.time() - t0 > _SEARCH_TIME_BUDGET or scanned > 20000:
            stopped = "limit"; break
    note = "\n[partial — narrow the path]" if stopped == "limit" else ""
    body = ("\n".join(hits) or f"(nothing named like '{name}' found)") + note
    return ToolResult(True, body, display=f"find '{name}' ({len(hits)} matches)")


# --------------------------------------------------------------------------- #
# Mutating tools (approval-gated by the agent)
# --------------------------------------------------------------------------- #

def write_file(root: Path, path: str, content: str) -> ToolResult:
    fp = _resolve(root, path)
    if fp is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    existed = fp.exists()
    old = fp.read_text(encoding="utf-8") if existed else ""
    return ToolResult(True, f"{'overwrote' if existed else 'created'} {path}",
                      display=f"write {path}", path=path,
                      old_text=old, new_text=content, created=not existed)


def edit_file(root: Path, path: str, search: str, replace: str) -> ToolResult:
    fp = _resolve(root, path)
    if fp is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    if not fp.exists():
        return ToolResult(False, f"File not found: {path}. It doesn't exist yet — "
                          f"use write_file to create it (with full content) instead of edit_file.")
    try:
        original = fp.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        return ToolResult(False, f"Cannot read {path}: {e}")
    if search not in original:
        return ToolResult(False, f"SEARCH text not found in {path}; re-read it and retry.")
    new_text = original.replace(search, replace, 1)
    return ToolResult(True, f"edited {path}", display=f"edit {path}", path=path,
                      old_text=original, new_text=new_text, created=False)


def commit_write(root: Path, path: str, new_text: str) -> bool:
    """Actually persist a previously-approved write/edit."""
    fp = _resolve(root, path)
    if fp is None:
        return False
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(new_text, encoding="utf-8")
    return True


def make_dir(root: Path, path: str) -> ToolResult:
    """Create a directory (and parents) without the shell — avoids cross-platform
    quoting pitfalls (e.g. spaces in Windows paths)."""
    dp = _resolve(root, path)
    if dp is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    try:
        dp.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return ToolResult(False, f"Cannot create {path}: {e}")
    return ToolResult(True, f"created directory {path}", display=f"mkdir {path}")


def run_command(root: Path, command: str, timeout: float = 120.0) -> ToolResult:
    """Run a shell command via the system shell, with `root` as cwd.

    Unlike the file tools above (read_file, write_file, edit_file, etc.), this
    has NO path confinement: the command can read, write, or execute anything
    the invoking user's OS permissions allow, not just files under `root`. The
    only safeguard is the approval flow upstream (suggest/auto-edit prompts
    the user; full-auto/--yolo runs it unattended) — there is no sandboxing
    here.
    """
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(root), capture_output=True,
            text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return ToolResult(False, f"Command timed out after {timeout}s: {command}")
    except Exception as e:
        return ToolResult(False, f"Command failed to start: {e}")
    out = (proc.stdout or "")[-6000:]
    err = (proc.stderr or "")[-2000:]
    body = f"exit={proc.returncode}\n"
    if out:
        body += f"stdout:\n{out}\n"
    if err:
        body += f"stderr:\n{err}"
    return ToolResult(proc.returncode == 0, body.strip(),
                      display=f"$ {command}  (exit {proc.returncode})")


def git_diff(root: Path, path: str = "") -> ToolResult:
    """Show the working-tree diff (read-only) — what's changed since the last commit.
    Optionally scope to a path."""
    tgt = _resolve(root, path) if path else None
    if path and tgt is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    cmd = ["git", "diff"]
    if path:
        cmd += ["--", str(tgt)]
    try:
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=30)
    except Exception as e:
        return ToolResult(False, f"git diff failed: {e}")
    if proc.returncode != 0 and not proc.stdout:
        return ToolResult(False, (proc.stderr or "not a git repository").strip()[:300])
    out = (proc.stdout or "")[:12000]
    return ToolResult(True, out or "(no uncommitted changes)", display="git diff")


def tree(root: Path, path: str = ".", max_depth: int = 3, max_entries: int = 400) -> ToolResult:
    """A compact recursive view of the directory structure (read-only, bounded)."""
    base = _resolve(root, path or ".")
    if base is None or not base.is_dir():
        return ToolResult(False, f"Not a directory: {path}")
    lines, count = [], 0
    skip = _SKIP_DIRS
    base_depth = len(base.resolve().parts)
    for dirpath, dirnames, filenames in os.walk(base):
        depth = len(Path(dirpath).resolve().parts) - base_depth
        if depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        indent = "  " * depth
        rel = Path(dirpath).relative_to(base.resolve())
        if str(rel) != ".":
            lines.append(f"{indent}{Path(dirpath).name}/")
        for f in sorted(filenames):
            if f.startswith(".") and f != ".env.example":
                continue
            lines.append(f"{indent}  {f}")
            count += 1
            if count >= max_entries:
                lines.append("  … [truncated]");
                return ToolResult(True, "\n".join(lines), display=f"tree {path} ({count}+ files)")
    return ToolResult(True, "\n".join(lines) or "(empty)", display=f"tree {path} ({count} files)")


def delete_file(root: Path, path: str) -> ToolResult:
    """Delete a file (approval-gated by the agent). Refuses directories — use the shell for those."""
    fp = _resolve(root, path)
    if fp is None:
        return ToolResult(False, f"Refused: '{path}' is outside the project.")
    if not fp.exists():
        return ToolResult(False, f"Not found: {path}")
    if fp.is_dir():
        return ToolResult(False, f"{path} is a directory; use run_command to remove directories.")
    try:
        fp.unlink()
    except OSError as e:
        return ToolResult(False, f"Cannot delete {path}: {e}")
    return ToolResult(True, f"deleted {path}", display=f"delete {path}")


def move_file(root: Path, src: str, dst: str) -> ToolResult:
    """Move/rename a file or directory within the project (approval-gated)."""
    sp = _resolve(root, src)
    dp = _resolve(root, dst)
    if sp is None or dp is None:
        return ToolResult(False, "Refused: source or destination is outside the project.")
    if not sp.exists():
        return ToolResult(False, f"Not found: {src}")
    try:
        dp.parent.mkdir(parents=True, exist_ok=True)
        sp.replace(dp)
    except OSError as e:
        return ToolResult(False, f"Cannot move {src} → {dst}: {e}")
    return ToolResult(True, f"moved {src} → {dst}", display=f"move {src} → {dst}")
