"""The Weio coding agent: an autonomous tool-use loop, like Claude Code / Codex.

The model works step by step, emitting tool calls as fenced JSON ```action blocks.
The agent executes them (gated by the approval mode), feeds back observations, and
loops until the model calls `finish` or the step budget is exhausted.

UI is injected via callbacks so the TUI and one-shot runner share this engine.
"""
from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import tools
from .client import WeioClient, WeioError
from .tools import ToolResult

_FENCE_RE = re.compile(r"```(?:action|json)?\s*\n(.*?)```", re.DOTALL)
_BARE_RE = re.compile(r'\{[^{}]*"tool"\s*:\s*"[a-z_]+".*?\}', re.DOTALL)
# Weaker models sometimes name the fence after the TOOL (```finish, ```run_command…)
# with the args object inside and no "tool" key. Accept that shape too — otherwise the
# raw block leaks into the narration and the loop nudges forever (seen live 2026-07-11).
_TOOL_FENCE_RE = re.compile(r"```([a-z_]+)\s*\n(.*?)```", re.DOTALL)
# An action/json fence the model left unterminated (no closing ```), or a leftover
# fragment of one — strip it so malformed tool calls never leak into the narration.
_UNCLOSED_FENCE_RE = re.compile(r"```(?:action|json)\b.*\Z", re.DOTALL)

READONLY_TOOLS = {"read_file", "list_dir", "search", "find", "tree", "git_diff"}
WRITE_TOOLS = {"write_file", "edit_file"}
DESTRUCTIVE_TOOLS = {"delete_file", "move_file"}   # approval-gated like commands
ALL_TOOLS = READONLY_TOOLS | WRITE_TOOLS | DESTRUCTIVE_TOOLS | {"make_dir", "run_command", "finish"}


def _os_line() -> str:
    sysname = platform.system()  # 'Windows' | 'Darwin' | 'Linux'
    if sysname == "Windows":
        return ("Windows (cmd.exe). Use Windows path/command syntax. ALWAYS wrap any "
                "path containing spaces in double quotes. Prefer the make_dir and "
                "write_file tools over shell `mkdir`/redirection — they handle spaces safely.")
    nice = "macOS" if sysname == "Darwin" else sysname
    return (f"{nice} (POSIX shell). Quote any path containing spaces. Prefer make_dir "
            "and write_file over shell mkdir/redirection.")


_CRAFTSMANSHIP_AND_API = """CRAFTSMANSHIP (judged on quality, not just "does it run"):
- NEVER ship unstyled/default-browser output. Any HTML/UI must look modern and intentional: real layout, type scale, spacing, coherent palette; system-font stacks, generous whitespace, cards with subtle borders/shadows, sensible max-width, considered theme. "rich/modern" + plain gray boxes = FAILING.
- Responsive (flex/grid, breakpoints) and accessible (labels, contrast, semantic HTML).
- NEVER hardcode fake data when real data/an API is asked for — fetch it. If unreachable, say so and build against the real endpoint with graceful loading/empty/error states; don't substitute made-up numbers.
- Ship complete: no TODO, no "rest here", no lorem. Wire it end to end. Verify (open/run/curl) before finish.

THE WEIO API (when a task says "connect to the Weio API" / "serve my company data"):
- Base https://api.weio.ai/v1 (OpenAI-compatible). Auth: header `Authorization: Bearer <weio_sk_ key>`. Key is in env WEIO_API_KEY or ~/.weio/config.json — read at runtime, NEVER hardcode.
- Endpoints: GET /v1/models · GET /v1/usage (daily tokens+limits+tier — great for a dashboard) · GET /v1/documents (saved docs/records = "company data") · GET /v1/agents · POST /v1/agents/<name-or-id>/run (body {"input":"..."} → grounded result) · POST /v1/chat/completions (model e.g. "weio-focus").
- Dashboard pattern: pull /v1/usage + /v1/documents for real content; optionally POST an agent run to summarize. Inject the key at serve time or proxy server-side so it isn't exposed in the page. Real loading/empty/error states."""


def system_prompt(root: Path) -> str:
    """Fence-mode system prompt: tool calls are emitted as fenced JSON ```action
    blocks (used when the gateway doesn't advertise native tool_calling support)."""
    return f"""You are Weio, an autonomous coding agent in the user's project directory:
{root}
Environment: {_os_line()}

Complete the task with TOOLS, step by step. Each turn: state in 1-2 sentences what you're about to do, then emit tool call(s) as fenced JSON:

```action
{{"tool": "read_file", "args": {{"path": "src/app.py"}}}}
```

TOOLS (args shown):
- read_file   {{"path","start":1,"end":80}}  (start/end optional, for big files)
- list_dir    {{"path":"."}}
- tree        {{"path":"."}}                  recursive structure (bounded)
- find        {{"name":"weio_ops"}}           locate a file/dir BY NAME (fast — use to find a project)
- search      {{"query":"...","path":"."}}    search file CONTENTS (slow — narrow the path first)
- make_dir    {{"path":"..."}}
- write_file  {{"path":"...","content":"..."}}  create/overwrite
- edit_file   {{"path":"...","search":"...","replace":"..."}}  replace an EXACT snippet
- delete_file {{"path":"..."}}
- move_file   {{"src":"...","dst":"..."}}
- git_diff    {{"path":"..."}}                 (path optional)
- run_command {{"command":"..."}}
- finish      {{"summary":"..."}}              when the task is FULLY done

RULES:
- Locate first: if the task names a project/file not in the current dir, `find` it by name (fast), then work inside it. Never `search` contents over a whole home dir — narrow the path.
- VERIFY IN THE ENVIRONMENT THAT EXISTS: run the project's own test command directly (e.g. `python -m pytest tests/ -q`, `npm test`) with whatever interpreter is already on PATH or in an existing venv/ dir. NEVER create a new virtualenv, install packages, or bootstrap tooling just to verify — if the runner or a dependency is missing, say exactly what's missing and what command the user should run, then finish. A precise "tests need X installed" beats a broken setup attempt.
- Explore before editing (read_file/list_dir/find). BATCH read-only actions (read_file, list_dir, search, find, tree, git_diff) — emit several in ONE reply, each its own ```action block; you get all observations back together. Cheaper than one read per turn; read every file you know you need at once.
- Emit only ONE write/command per turn (write_file, edit_file, run_command, delete_file, move_file) — stop after each to react to its result.
- edit_file "search" must match the file EXACTLY (whitespace included). Prefer edit_file for small changes, write_file for new files/full rewrites.
- Verify after changes when reasonable. Call finish only when complete, never early. Every reply has ≥1 ```action block until finish. Do the work — emit the action, don't just describe it.

{_CRAFTSMANSHIP_AND_API}"""


def system_prompt_native(root: Path) -> str:
    """Native tool-calling system prompt: tools are registered via the API's `tools`
    schema (see tool_schemas()), so there's no fence format to teach the model —
    the RULES below are the same policy as the fence prompt, worded for real
    function calls instead of ```action blocks."""
    return f"""You are Weio, an autonomous coding agent in the user's project directory:
{root}
Environment: {_os_line()}

Complete the task using the provided tools, step by step. Each turn: state in 1-2 sentences what you're about to do, then call the tool(s) you need.

RULES:
- Locate first: if the task names a project/file not in the current dir, call find to locate it by name (fast), then work inside it. Never call search over a whole home dir — narrow the path.
- VERIFY IN THE ENVIRONMENT THAT EXISTS: run the project's own test command directly (e.g. `python -m pytest tests/ -q`, `npm test`) with whatever interpreter is already on PATH or in an existing venv/ dir. NEVER create a new virtualenv, install packages, or bootstrap tooling just to verify — if the runner or a dependency is missing, say exactly what's missing and what command the user should run, then finish. A precise "tests need X installed" beats a broken setup attempt.
- Explore before editing (read_file/list_dir/find). You may call several read-only tools (read_file, list_dir, search, find, tree, git_diff) in the SAME turn — you'll get all their results back together. Cheaper than one call per turn; read every file you know you need at once.
- Call only ONE write/command tool per turn (write_file, edit_file, run_command, delete_file, move_file) — stop after each to react to its result.
- edit_file "search" must match the file EXACTLY (whitespace included). Prefer edit_file for small changes, write_file for new files/full rewrites.
- Verify after changes when reasonable. Call finish only when complete, never early. Every reply should call a tool until finish. Do the work — call the tool, don't just describe it.

{_CRAFTSMANSHIP_AND_API}"""


def tool_schemas() -> list[dict]:
    """OpenAI function-tool schemas for the agent's action vocabulary -- mirrors the
    TOOLS list in system_prompt() exactly, one schema per tool."""
    def _fn(name: str, description: str, properties: dict, required: Optional[list] = None) -> dict:
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required or [],
                },
            },
        }

    return [
        _fn("read_file", "Read a file in the project (optionally a 1-based line range for big files).",
            {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            ["path"]),
        _fn("list_dir", "List a directory's immediate contents.",
            {"path": {"type": "string"}}),
        _fn("tree", "Recursive, bounded directory structure.",
            {"path": {"type": "string"}}),
        _fn("find", "Locate a file/dir BY NAME (fast) -- use this to find a project before working in it.",
            {"name": {"type": "string"}, "path": {"type": "string"}}, ["name"]),
        _fn("search", "Search file CONTENTS (slow -- narrow the path first).",
            {"query": {"type": "string"}, "path": {"type": "string"}}, ["query"]),
        _fn("make_dir", "Create a directory (and any missing parents).",
            {"path": {"type": "string"}}, ["path"]),
        _fn("write_file", "Create or overwrite a file with full content.",
            {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
        _fn("edit_file", "Replace an EXACT snippet (whitespace included) in an existing file.",
            {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}},
            ["path", "search", "replace"]),
        _fn("delete_file", "Delete a file.", {"path": {"type": "string"}}, ["path"]),
        _fn("move_file", "Move or rename a file or directory.",
            {"src": {"type": "string"}, "dst": {"type": "string"}}, ["src", "dst"]),
        _fn("git_diff", "Show the working-tree diff (read-only), optionally scoped to a path.",
            {"path": {"type": "string"}}),
        _fn("run_command", "Run a shell command with the project directory as cwd. NOT confined to "
            "the project -- it can read/write/execute anything the user's OS permissions allow.",
            {"command": {"type": "string"}}, ["command"]),
        _fn("finish", "Call when the task is FULLY done.",
            {"summary": {"type": "string"}}, ["summary"]),
    ]


@dataclass
class Callbacks:
    on_text: Callable[[str], None]                       # model narration
    on_action: Callable[[str, dict], None]               # about to run a tool
    on_result: Callable[[str, ToolResult], None]         # tool finished
    approve: Callable[[str, dict, Optional[ToolResult]], bool]  # ask user (writes/cmds)
    on_status: Callable[[str], None]                     # spinner/status text
    on_finish: Callable[[str], None]                     # final summary


def parse_actions(text: str) -> list[dict]:
    """Extract tool calls. Accepts ```action / ```json fences, and bare JSON
    objects containing a "tool" key (robust to weaker models' formatting)."""
    actions = []
    seen = set()
    for m in _FENCE_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "tool" in obj:
            key = json.dumps(obj, sort_keys=True)
            if key not in seen:
                seen.add(key)
                actions.append(obj)
    if not actions:
        # Tool-named fences: ```finish\n{"summary": …}``` etc. — the fence label IS the tool.
        for m in _TOOL_FENCE_RE.finditer(text):
            label, raw = m.group(1), m.group(2).strip()
            if label in ("action", "json") or label not in ALL_TOOLS:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                norm = obj if "tool" in obj else {"tool": label, "args": obj.get("args", obj)}
                key = json.dumps(norm, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    actions.append(norm)
    if not actions:
        for raw in _balanced_objects(text):
            if '"tool"' not in raw:
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and "tool" in obj:
                key = json.dumps(obj, sort_keys=True)
                if key not in seen:
                    seen.add(key)
                    actions.append(obj)
    return actions


def _balanced_objects(text: str):
    """Yield top-level {...} substrings with balanced braces (handles nested
    args objects and braces inside strings). Lets us recover bare tool calls
    from weaker models that skip the ```action fence."""
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, j, in_str, esc = 0, i, False, False
        while j < n:
            c = text[j]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[i:j + 1]
                        break
            j += 1
        i = j + 1 if j > i else i + 1


def strip_actions(text: str) -> str:
    out = _FENCE_RE.sub("", text)        # well-formed ```action / ```json blocks
    # tool-named fences (```finish …) — strip only real tool names, keep code fences
    out = _TOOL_FENCE_RE.sub(lambda m: "" if m.group(1) in ALL_TOOLS else m.group(0), out)
    out = _UNCLOSED_FENCE_RE.sub("", out)  # an unterminated trailing fence
    out = _BARE_RE.sub("", out)          # bare {"tool": …} objects
    return out.strip()


class Agent:
    def __init__(self, client: WeioClient, root: Path, settings: dict, cb: Callbacks):
        self.client = client
        self.root = root
        self.settings = settings
        self.cb = cb
        # 1.0: native tool-calling mode when the gateway advertises support (probed
        # once, cached on the client). Any probe failure -- including clients (like
        # older/fake test doubles) that don't even have the method -- means False,
        # so the published fence-based path runs UNCHANGED against older gateways.
        probe = getattr(client, "probe_tool_calling", None)
        try:
            self.native = bool(probe()) if callable(probe) else False
        except Exception:
            self.native = False
        sp = system_prompt_native(root) if self.native else system_prompt(root)
        self.messages: list[dict] = [{"role": "system", "content": sp}]
        self.approval = settings.get("approval", "suggest")
        self.max_steps = int(settings.get("max_steps", 25))
        self.max_tokens = int(settings.get("max_tokens", 4096))
        self._failed_cmds: dict[str, int] = {}
        # Smart escalation: the agent runs on the configured workhorse model and
        # spends a frontier (Weio Insight) call only when a step truly needs it —
        # so high-tier credits are used sparingly, not on every routine step.
        self._escalate_next = False
        self._native_nudges = 0

    # -- context compaction (credit efficiency) --
    # The whole transcript is re-sent every step, so an un-pruned history makes input
    # cost grow ~quadratically over a task. We keep the system prompt, the original task,
    # and the most recent exchanges verbatim, and shrink OLDER observations/narration to
    # short stubs — the model rarely needs full old file dumps once it has acted on them.
    _KEEP_RECENT = 6      # most-recent messages kept verbatim
    _STUB_CHARS = 600     # chars retained from an older, long message

    def _compact(self) -> None:
        n = len(self.messages)
        if n <= self._KEEP_RECENT + 2:
            return
        cutoff = n - self._KEEP_RECENT
        for i in range(2, cutoff):            # preserve system[0] + original task[1]
            m = self.messages[i]
            c = m.get("content", "")
            if not isinstance(c, str) or len(c) <= self._STUB_CHARS or c.endswith("⋯]"):
                continue
            # Fence mode stuffs tool output into "OBSERVATIONS:" user turns; native
            # mode carries each tool result as its own role:"tool" message. Both are
            # the same kind of thing for compaction purposes -- old tool output the
            # model has already acted on.
            if m.get("role") == "tool" or (m.get("role") == "user" and c.lstrip().startswith("OBSERVATIONS")):
                self.messages[i] = {**m, "content": c[:self._STUB_CHARS] + "\n… [older observation elided ⋯]"}
            elif m.get("role") == "assistant" and len(c) > self._STUB_CHARS * 2:
                self.messages[i] = {**m, "content": c[:self._STUB_CHARS] + "\n… [elided ⋯]"}

    # -- model call --
    def _complete(self) -> str:
        self._compact()
        # Default to the workhorse model (the client's selected tier); escalate a
        # single step to the frontier tier only when flagged (stuck, or a hard task).
        model = None
        # Keep the caller-selected model throughout all agent turns.
        self._escalate_next = False
        return self.client.chat(self.messages, max_tokens=self.max_tokens,
                                temperature=0.1, model=model)

    def _complete_native(self) -> dict:
        """Native tool-calling model call. Returns the client's
        {"content", "tool_calls", "finish_reason"} dict."""
        self._compact()
        model = None
        # Keep the caller-selected model throughout all agent turns.
        self._escalate_next = False
        return self.client.chat_with_tools(self.messages, tools=tool_schemas(),
                                           max_tokens=self.max_tokens, temperature=0.1, model=model)

    # -- approval policy --
    def _needs_approval(self, tool: str) -> bool:
        if self.approval == "full-auto":
            return False
        if tool in WRITE_TOOLS:
            return self.approval == "suggest"
        if tool == "run_command" or tool in DESTRUCTIVE_TOOLS:
            return True  # commands + delete/move always confirmed unless full-auto
        return False

    # -- execute one tool, return observation string --
    def _execute(self, tool: str, args: dict) -> str:
        self.cb.on_action(tool, args)

        if tool == "read_file":
            r = tools.read_file(self.root, args.get("path", ""),
                                start=args.get("start"), end=args.get("end"))
        elif tool == "list_dir":
            r = tools.list_dir(self.root, args.get("path", "."))
        elif tool == "tree":
            r = tools.tree(self.root, args.get("path", "."))
        elif tool == "find":
            r = tools.find_files(self.root, args.get("name", "") or args.get("query", ""), args.get("path", "."))
        elif tool == "search":
            r = tools.search_files(self.root, args.get("query", ""), args.get("path", "."))
        elif tool == "git_diff":
            r = tools.git_diff(self.root, args.get("path", ""))
        elif tool == "delete_file":
            _p = args.get("path", "")
            if self._needs_approval(tool) and not self.cb.approve(tool, {"path": _p}, None):
                r = ToolResult(False, f"delete {_p} declined by user.", display=f"delete {_p} (declined)")
            else:
                r = tools.delete_file(self.root, _p)
        elif tool == "move_file":
            _s, _d = args.get("src", ""), args.get("dst", "")
            if self._needs_approval(tool) and not self.cb.approve(tool, {"src": _s, "dst": _d}, None):
                r = ToolResult(False, f"move {_s} declined by user.", display=f"move {_s} (declined)")
            else:
                r = tools.move_file(self.root, _s, _d)
        elif tool == "make_dir":
            r = tools.make_dir(self.root, args.get("path", ""))
        elif tool == "write_file":
            r = tools.write_file(self.root, args.get("path", ""), args.get("content", ""))
            r = self._maybe_commit(tool, args, r)
        elif tool == "edit_file":
            r = tools.edit_file(self.root, args.get("path", ""),
                                args.get("search", ""), args.get("replace", ""))
            r = self._maybe_commit(tool, args, r)
        elif tool == "run_command":
            cmd = args.get("command", "")
            if self._needs_approval(tool) and not self.cb.approve(tool, args, None):
                r = ToolResult(False, "Command declined by user.", display=f"$ {cmd} (declined)")
            else:
                r = tools.run_command(self.root, cmd)
                # Loop guard: if this exact command already failed, tell the model
                # to change approach instead of repeating it.
                if not r.ok:
                    n = self._failed_cmds.get(cmd, 0) + 1
                    self._failed_cmds[cmd] = n
                    if n >= 2:
                        self._escalate_next = True  # stuck → let the frontier model unstick it
                        r = ToolResult(False, r.output + (
                            "\n\nNOTE: this exact command has now failed "
                            f"{n} times. Do NOT run it again. Change approach — e.g. use the "
                            "make_dir/write_file tools, or fix quoting (wrap paths with spaces "
                            "in double quotes)."), display=r.display)
        else:
            r = ToolResult(False, f"Unknown tool: {tool}")

        self.cb.on_result(tool, r)
        return f"[{tool} {args.get('path') or args.get('command') or args.get('query') or ''}]\n{r.output}"

    def _maybe_commit(self, tool: str, args: dict, r: ToolResult) -> ToolResult:
        if not r.ok:
            return r
        if self._needs_approval(tool) and not self.cb.approve(tool, args, r):
            return ToolResult(False, f"{tool} on {r.path} declined by user.",
                              display=f"{tool} {r.path} (declined)")
        if tools.commit_write(self.root, r.path, r.new_text):
            return r
        return ToolResult(False, f"Failed to write {r.path}")

    def _with_session_line(self, summary: str, steps: int) -> str:
        """Append an honest session footer (steps + tokens) to the finish summary."""
        toks = getattr(self.client, "session_tokens", 0)
        if not toks:
            return summary
        return f"{summary}\n({steps} step{'s' if steps != 1 else ''} · {toks:,} tokens this session)"

    # -- main loop --
    _CLAIM_WORDS = ("added", "created", "edited", "wrote", "updated", "modified",
                    "implemented", "changed", "inserted", "removed", "deleted")

    _HARD_SIGNALS = ("refactor", "architect", "design", "debug", "why ", "root cause",
                     "trace", "optimi", "concurren", "race condition", "across files",
                     "multiple files", "redesign", "migrate", "rewrite")

    def run_task(self, task: str) -> str:
        """Entry point: dispatch to the native tool-calling loop when the gateway
        supports it, otherwise run the original fence-based loop UNCHANGED — a
        published CLI must keep working against gateways that predate ops #129."""
        if self.native:
            return self._run_task_native(task)
        return self._run_task_fence(task)

    def _run_task_fence(self, task: str) -> str:
        self.messages.append({"role": "user", "content": task})
        # Escalate the FIRST (planning) step for tasks that read as genuinely hard,
        # so the frontier model sets the strategy; routine steps stay on the workhorse.
        tl = task.lower()
        if len(task) > 280 or any(s in tl for s in self._HARD_SIGNALS):
            self._escalate_next = True
        nudges = 0
        for step in range(self.max_steps):
            self.cb.on_status("thinking")
            try:
                response = self._complete()
            except WeioError as e:
                self.cb.on_text(f"[error] {e}")
                return f"error: {e}"

            narration = strip_actions(response)
            if narration:
                self.cb.on_text(narration)

            actions = parse_actions(response)
            self.messages.append({"role": "assistant", "content": response})

            if not actions:
                # The model replied without a tool call. If it sounds like it
                # claimed to make a change (but didn't actually act), push back
                # hard. Allow up to 2 nudges before accepting the reply as final.
                claims_edit = any(w in narration.lower() for w in self._CLAIM_WORDS)
                if nudges < 2:
                    nudges += 1
                    msg = ("You did NOT emit a tool call — nothing changed on disk. "
                           "Nothing happens until you emit a ```action block. "
                           "If you intended to edit a file, emit the edit_file (or write_file) "
                           "action now with the exact content. Otherwise call finish.")
                    if not claims_edit:
                        msg = ("Take the next step now as a ```action block, "
                               "or call finish if the task is already complete.")
                    self.messages.append({"role": "user", "content": msg})
                    continue
                self.cb.on_finish(narration or "(done)")
                return narration
            nudges = 0

            observations = []
            finished = None
            for act in actions:
                tool = act.get("tool")
                args = act.get("args", {}) if isinstance(act.get("args"), dict) else {}
                if tool == "finish":
                    finished = args.get("summary", "Done.")
                    break
                if tool not in ALL_TOOLS:
                    observations.append(f"[{tool}] Unknown tool. Use only the listed tools.")
                    continue
                observations.append(self._execute(tool, args))

            if finished is not None:
                self.cb.on_finish(self._with_session_line(finished, step + 1))
                return finished

            self.messages.append({"role": "user", "content": "OBSERVATIONS:\n" + "\n\n".join(observations)})

        self.cb.on_finish("Reached the step limit before finishing. Re-run to continue.")
        return "step-limit"

    def _run_task_native(self, task: str) -> str:
        """Native tool-calling loop: the model returns real `tool_calls` instead of
        fenced JSON. Same policy as the fence loop -- explore/escalate/loop-guard/
        approval bounds are identical, only the wire format differs."""
        self.messages.append({"role": "user", "content": task})
        tl = task.lower()
        if len(task) > 280 or any(s in tl for s in self._HARD_SIGNALS):
            self._escalate_next = True

        for step in range(self.max_steps):
            self.cb.on_status("thinking")
            try:
                response = self._complete_native()
            except WeioError as e:
                self.cb.on_text(f"[error] {e}")
                return f"error: {e}"

            content = response.get("content") or ""
            if content:
                self.cb.on_text(content)

            tool_calls = response.get("tool_calls") or []
            assistant_msg: dict = {"role": "assistant", "content": response.get("content")}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            self.messages.append(assistant_msg)

            if not tool_calls:
                # The model replied without calling a tool. Same nudge policy as the
                # fence loop: push back (harder if it sounds like it claimed to act),
                # up to 2 nudges before accepting the reply as final.
                claims_edit = any(w in content.lower() for w in self._CLAIM_WORDS)
                if self._native_nudges < 2:
                    self._native_nudges += 1
                    msg = ("Take the next step now by calling a tool, "
                           "or call finish if the task is already complete.")
                    if claims_edit:
                        msg = ("You did NOT call a tool — nothing changed on disk. "
                               "If you intended to edit a file, call edit_file (or write_file) "
                               "now with the exact content. Otherwise call finish.")
                    self.messages.append({"role": "user", "content": msg})
                    continue
                self.cb.on_finish(content or "(done)")
                return content
            self._native_nudges = 0

            finished = None
            for tc in tool_calls:
                call_id = tc.get("id") or ""
                fn = tc.get("function") or {}
                tool = fn.get("name", "")
                raw_args = fn.get("arguments")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                except (json.JSONDecodeError, ValueError, TypeError):
                    self.messages.append({"role": "tool", "tool_call_id": call_id, "name": tool,
                                          "content": "Invalid tool arguments; expected a JSON object."})
                    continue
                if finished is not None:
                    self.messages.append({"role": "tool", "tool_call_id": call_id, "name": tool,
                                          "content": "Task already finished; no tool executed."})
                    continue
                if tool == "finish":
                    finished = args.get("summary", "Done.")
                    self.messages.append({"role": "tool", "tool_call_id": call_id,
                                          "name": tool, "content": "Task finished."})
                    continue
                if tool not in ALL_TOOLS:
                    self.messages.append({"role": "tool", "tool_call_id": call_id, "name": tool,
                                          "content": f"Unknown tool: {tool}. Use only the provided tools."})
                    continue
                observation = self._execute(tool, args)
                self.messages.append({"role": "tool", "tool_call_id": call_id,
                                      "name": tool, "content": observation})

            if finished is not None:
                self.cb.on_finish(self._with_session_line(finished, step + 1))
                return finished

        self.cb.on_finish("Reached the step limit before finishing. Re-run to continue.")
        return "step-limit"
