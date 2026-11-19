"""Weio coding-agent TUI — an autonomous, tool-using REPL like Claude Code / Codex.

A scrolling terminal interface: you give a task, the agent reads/edits files and
runs commands (gated by your approval mode), with tool calls rendered as panels,
colored diffs, and a live spinner. prompt_toolkit drives input; plain fallback
when there's no TTY.
"""
from __future__ import annotations

import itertools
import sys
import threading
import time
from pathlib import Path

from . import __version__, settings as settings_mod, themes
from .agent import Agent, Callbacks, WRITE_TOOLS
from .client import WeioClient
from .tools import ToolResult

try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.formatted_text import FormattedText
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style
    _HAS_PTK = True
except Exception:  # pragma: no cover
    _HAS_PTK = False

_TTY = sys.stdout.isatty()


def _c(s: str, role: str, theme: themes.Theme | None = None) -> str:
    return f"\033[{themes.code(role, theme)}m{s}\033[0m" if _TTY else s


BRAND = "brand"
DIM = "dim"
BOLD = "bold"
CYAN = "prompt"
GREEN = "success"
YELLOW = "warning"
RED = "error"
BLUE = "tool"
MAG = "assistant"


def _submit_input(event) -> None:
    event.current_buffer.validate_and_handle()


def _insert_newline(event) -> None:
    event.current_buffer.insert_text("\n")


def _build_input_bindings():
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        _submit_input(event)

    @kb.add("escape", "enter")
    def _(event):
        _insert_newline(event)

    return kb


def _ptk_style(theme: themes.Theme):
    return Style.from_dict(dict(theme.prompt_toolkit))


def _toolbar(text: str):
    return FormattedText([("class:bottom-toolbar", text)])


def _prompt():
    return FormattedText([("class:prompt", "\nweio › ")])


class Spinner:
    def __init__(self, label: str = "thinking"):
        self.label = label
        self._stop = threading.Event()
        self._t = None

    def _run(self):
        for f in itertools.cycle("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"):
            if self._stop.is_set():
                break
            sys.stdout.write("\r" + _c(f"  {f} {self.label}…", DIM))
            sys.stdout.flush()
            time.sleep(0.08)
        sys.stdout.write("\r" + " " * (len(self.label) + 8) + "\r")
        sys.stdout.flush()

    def start(self):
        if _TTY:
            self._stop.clear()
            self._t = threading.Thread(target=self._run, daemon=True)
            self._t.start()

    def stop(self):
        if self._t:
            self._stop.set()
            self._t.join(timeout=0.3)
            self._t = None


def _color_diff(old: str, new: str, path: str) -> str:
    import difflib
    lines = []
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(),
                                    lineterm="", n=2):
        if ln.startswith("+++") or ln.startswith("---"):
            continue
        gutter = _c("│ ", DIM)
        if ln.startswith("+"):
            lines.append(gutter + _c(ln, "diff_add"))
        elif ln.startswith("-"):
            lines.append(gutter + _c(ln, "diff_remove"))
        elif ln.startswith("@@"):
            lines.append(gutter + _c(ln, "diff_hunk"))
        else:
            lines.append(gutter + _c(ln, DIM))
    return "\n".join(lines)


HELP = """Commands:
  /help                 show this help
  /model [low|mid|pro|auto]  pick a model — low=cheapest, pro=deepest (auto=balanced)
  /approval <mode>      suggest | auto-edit | full-auto
  /yolo                 toggle full-auto (skip ALL approval prompts)
  /steps <n>            max agent steps per task
  /settings             show current settings
  /ledger               show org work ledger rollup + open entries
  /undo                 revert the last applied file change
  /clear                start a fresh conversation
  /cwd [path]           show or change the project directory
  /exit, /quit          leave (Ctrl-D too)

Approval modes:
  suggest    ask before every file write AND command   (default, safest)
  auto-edit  apply file writes automatically; still ask before commands
  full-auto  skip every prompt — writes and commands run unattended

Type a task (e.g. "add a /health route to app.py and run the tests").
Enter submits. Alt+Enter inserts a newline; pasted multiline text stays editable.
Tip: start with `weio --dangerously-skip-permissions` (or run /yolo) to skip
approvals entirely — only in a directory you trust."""


class AgentTUI:
    def __init__(self, client: WeioClient, root: Path, st: dict):
        self.client = client
        self.root = root
        self.st = st
        self.theme = themes.set_active_theme(st.get("theme"))
        # client.model is set by the caller (run_tui/run_once) via _agent_model.
        self.undo_stack: list[tuple] = []   # (path, old_text_or_None)
        self._spinner = None
        self._session = None
        if _HAS_PTK and sys.stdin.isatty() and _TTY:
            cfg = Path(settings_mod._dir())
            cfg.mkdir(parents=True, exist_ok=True)
            self._session = PromptSession(
                history=FileHistory(str(cfg / "tui_history")),
                completer=WordCompleter(
                    ["/help", "/model", "/approval", "/yolo", "/steps", "/settings",
                     "/ledger", "/undo", "/clear", "/cwd", "/exit", "/quit"], sentence=True),
                multiline=True,
                key_bindings=_build_input_bindings(),
                style=_ptk_style(self.theme),
            )

    # ---- callbacks for the agent ----
    def _make_callbacks(self) -> Callbacks:
        return Callbacks(
            on_text=self._on_text,
            on_action=self._on_action,
            on_result=self._on_result,
            approve=self._approve,
            on_status=self._on_status,
            on_finish=self._on_finish,
        )

    def _stop_spin(self):
        if self._spinner:
            self._spinner.stop()
            self._spinner = None

    def _on_status(self, status: str):
        self._stop_spin()
        self._spinner = Spinner(status)
        self._spinner.start()

    def _on_text(self, text: str):
        self._stop_spin()
        if text.strip():
            print("\n" + _c("●", MAG) + " " + text.strip())

    def _on_action(self, tool: str, args: dict):
        self._stop_spin()
        icon = {"read_file": "📖", "list_dir": "📂", "tree": "🌳", "search": "🔎",
                "find": "🔍", "git_diff": "±", "write_file": "✎", "edit_file": "✎",
                "delete_file": "🗑", "move_file": "↪", "make_dir": "📁",
                "run_command": "❯"}.get(tool, "•")
        target = (args.get("path") or args.get("command") or args.get("query")
                  or args.get("name") or args.get("src") or "")
        print(_c(f"  {icon} {tool} ", BLUE) + _c(str(target), DIM))

    def _show_change(self, r: ToolResult, preview: bool = False):
        if r.created:
            verb = "will create" if preview else "created"
            n = len((r.new_text or "").splitlines())
            print(_c("│ ", DIM) + _c(f"{verb} {r.path} ({n} lines)", GREEN))
        else:
            print(_color_diff(r.old_text or "", r.new_text or "", r.path))

    def _on_result(self, tool: str, r: ToolResult):
        self._stop_spin()
        if tool in WRITE_TOOLS and r.ok and r.path is not None:
            self.undo_stack.append((r.path, None if r.created else r.old_text))
            # In suggest mode the diff was already shown before the approval prompt.
            if self.st.get("approval") != "suggest":
                self._show_change(r)
            else:
                print(_c(f"  ✓ applied {r.path}", GREEN))
        elif tool == "run_command":
            color = GREEN if r.ok else RED
            for ln in r.output.splitlines()[:20]:
                print(_c("│ ", DIM) + _c(ln, color if ln.startswith("exit=") else DIM))
        elif not r.ok:
            print(_c("  ! " + r.output.splitlines()[0], RED))
        else:
            print(_c("  ✓ " + r.display, DIM))

    def _approve(self, tool: str, args: dict, r) -> bool:
        self._stop_spin()
        if r is not None and r.path is not None:
            # Show what will change BEFORE asking.
            self._show_change(r, preview=True)
            prompt = f"  apply {tool} to {r.path}? [y/N] "
        elif 'command' in args:
            prompt = f"  run `{args.get('command')}`? [y/N] "
        elif tool == 'delete_file':
            prompt = f"  delete {args.get('path')}? [y/N] "
        elif tool == 'move_file':
            prompt = f"  move {args.get('src')} → {args.get('dst')}? [y/N] "
        else:
            prompt = f"  {tool} {args.get('path','')}? [y/N] "
        try:
            ans = (self._session.prompt(prompt, multiline=False) if self._session else input(_c(prompt, YELLOW)))
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        return ans.strip().lower() in ("y", "yes")

    def _on_finish(self, summary: str):
        self._stop_spin()
        print("\n" + _c("✔ ", GREEN) + _c(summary.strip(), BOLD))

    # ---- input + commands ----
    def _read(self):
        try:
            if self._session:
                return self._session.prompt(
                    _prompt(),
                    bottom_toolbar=_toolbar(f" {self.root.name} · {self.st.get('model','auto')} "
                                            f"(/model) · {self.st.get('approval')} · "
                                            f"Alt+Enter newline · /help"))
            return input(_c("\nweio › ", CYAN))
        except (EOFError, KeyboardInterrupt):
            return None

    def _slash(self, text: str) -> bool:
        parts = text.split(maxsplit=1)
        cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
        if cmd in ("/exit", "/quit"):
            return False
        if cmd == "/help":
            print(HELP)
        elif cmd == "/model":
            arg = arg.lower()
            if arg in ("auto", "low", "mid", "pro"):
                settings_mod.set_value("model", arg)
                self.st["model"] = arg
                self.client.model = _agent_model(arg)
                print(_c(f"  model = {arg} — {MODEL_LABELS.get(arg, arg)}", GREEN))
            else:
                cur = self.st.get("model", "auto")
                print(_c("  Choose a model (lower = fewer tokens):", BOLD))
                for k in ("low", "mid", "pro", "auto"):
                    mark = " ◀ current" if k == cur else ""
                    print(f"    /model {k:5} {MODEL_LABELS[k]}{_c(mark, GREEN)}")
        elif cmd == "/approval":
            ok, msg = settings_mod.set_value("approval", arg)
            if ok:
                self.st["approval"] = arg
            print(_c("  " + msg, GREEN if ok else RED))
        elif cmd == "/yolo":
            # Toggle full-auto <-> suggest. Skips ALL approval prompts.
            new_mode = "suggest" if self.st.get("approval") == "full-auto" else "full-auto"
            settings_mod.set_value("approval", new_mode)
            self.st["approval"] = new_mode
            if new_mode == "full-auto":
                print(_c("  ⚠ full-auto ON — writes and commands run WITHOUT asking. "
                         "Only in a directory you trust.", RED))
            else:
                print(_c("  full-auto OFF — back to asking before writes and commands.", GREEN))
        elif cmd == "/steps":
            ok, msg = settings_mod.set_value("max_steps", arg)
            if ok:
                self.st["max_steps"] = int(arg)
            print(_c("  " + msg, GREEN if ok else RED))
        elif cmd == "/settings":
            for k in ("model", "approval", "theme", "max_steps", "max_tokens"):
                print(f"  {k:10} {self.st.get(k)}")
        elif cmd == "/ledger":
            try:
                from .ledger import format_ledger_view
                rollup = self.client.ledger_rollup()
                data = self.client.ledger_entries(state="open", limit=15)
                print(format_ledger_view(rollup, data.get("entries") or []))
            except Exception as e:
                print(_c("  " + str(e), RED))
        elif cmd == "/clear":
            print(_c("  conversation cleared.", GREEN))
            return "clear"
        elif cmd == "/cwd":
            if arg:
                p = (self.root / arg).resolve()
                if p.is_dir():
                    self.root = p
                    print(_c(f"  cwd = {p}", GREEN))
                else:
                    print(_c(f"  not a directory: {arg}", RED))
            else:
                print("  cwd =", self.root)
        elif cmd == "/undo":
            self._undo()
        else:
            print(_c(f"  unknown command: {cmd} (/help)", RED))
        return True

    def _undo(self):
        if not self.undo_stack:
            print(_c("  nothing to undo.", DIM))
            return
        path, old = self.undo_stack.pop()
        fp = self.root / path
        if old is None:
            try:
                fp.unlink()
            except OSError:
                pass
            print(_c(f"  ↩ removed {path}", GREEN))
        else:
            fp.write_text(old, encoding="utf-8")
            print(_c(f"  ↩ reverted {path}", GREEN))

    # ---- main loop ----
    def run(self) -> int:
        ok = self.client.ping()
        print(_c(f"\n  ▟ weio {__version__}", BRAND) + _c("  coding agent", DIM))
        print(_c(f"  {self.root}", DIM))
        print(_c(f"  {self.client.base} · " + ("connected" if ok else "⚠ can't reach gateway"),
                 GREEN if ok else RED))
        try:
            from .updater import update_notice
            n = update_notice()
            if n:
                print(_c("  ⬆ " + n, YELLOW))
        except Exception:
            pass
        _m = self.st.get("model", "auto")
        print(_c(f"  model: {_m} — {MODEL_LABELS.get(_m, _m)}  ·  /model to change (lower = fewer tokens)", DIM))
        print(_c(f"  theme: {self.theme.name}  ·  weio config set theme <weio|terminal>", DIM))
        appr = self.st.get("approval")
        if appr == "full-auto":
            print(_c("  ⚠ approval: full-auto — skipping ALL prompts (writes + commands run unattended)", RED))
            print(_c("    type a task, /yolo to turn off, or /help\n", DIM))
        else:
            print(_c(f"  approval: {appr}  ·  type a task, /yolo to skip approvals, or /help\n", DIM))

        agent = Agent(self.client, self.root, self.st, self._make_callbacks())
        while True:
            text = self._read()
            if text is None:
                print()
                break
            text = text.strip()
            if not text:
                continue
            if text.startswith("/"):
                res = self._slash(text)
                if res is False:
                    break
                if res == "clear":
                    agent = Agent(self.client, self.root, self.st, self._make_callbacks())
                continue
            # New task: refresh agent root/settings, keep conversation across tasks
            agent.root = self.root
            agent.approval = self.st.get("approval", "suggest")
            agent.max_steps = int(self.st.get("max_steps", 25))
            try:
                agent.run_task(text)
            except KeyboardInterrupt:
                self._stop_spin()
                print(_c("\n  (interrupted)", YELLOW))
        print(_c("bye.", DIM))
        return 0


# Friendly effort labels (like Claude Code's model menu). 'auto' balances cost/quality.
MODEL_LABELS = {
    "auto": "Auto (balanced)",
    "low":  "Pulse — fast, fewest tokens",
    "mid":  "Focus — balanced (default)",
    "pro":  "Insight — deepest, most tokens",
}


def _agent_model(effort: str) -> str:
    """Map a chosen effort to the gateway model preference. 'auto' → 'mid' (balanced,
    Sonnet-class) — like Claude Code defaulting to Sonnet, not Opus — so agentic work
    doesn't burn the daily quota on the most expensive model. Users pick 'pro' for
    deep tasks or 'low' to stretch their quota. The server clamps to the tier's best."""
    return effort or "auto"


def run_tui(client: WeioClient, root: Path, max_tokens: int = 4096, yolo: bool = False,
            model: str | None = None, theme: str | None = None) -> int:
    st = settings_mod.load()
    st["max_tokens"] = max_tokens or st.get("max_tokens", 4096)
    if model:                       # explicit --model flag wins over saved config
        st["model"] = model
    if theme:                       # explicit --theme flag wins over saved config
        st["theme"] = themes.normalize_theme(theme)
    if yolo:
        st["approval"] = "full-auto"
    client.model = _agent_model(st.get("model", "auto"))
    return AgentTUI(client, root, st).run()


def run_once(client: WeioClient, root: Path, task: str, *, approval: str | None = None,
             max_tokens: int = 4096, model: str | None = None,
             theme: str | None = None) -> int:
    """Non-interactive single task run (for `weio "task"`)."""
    st = settings_mod.load()
    if approval:
        st["approval"] = approval
    if model:
        st["model"] = model
    if theme:
        st["theme"] = themes.normalize_theme(theme)
    st["max_tokens"] = max_tokens or st.get("max_tokens", 4096)
    client.model = _agent_model(st.get("model", "auto"))
    tui = AgentTUI(client, root, st)
    agent = Agent(client, root, st, tui._make_callbacks())
    print(_c(f"  ▟ weio {__version__}", BRAND) + _c(f"  ·  {root}", DIM))
    result = agent.run_task(task)
    return 1 if not isinstance(result, str) or result == "step-limit" or result.startswith("error:") else 0
