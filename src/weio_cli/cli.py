"""weio — command-line entry point.

Commands:
  weio login                 Sign in (browser by default; --api-key for a dashboard key)
  weio logout                Clear the saved credential
  weio whoami                Show the signed-in account, tier, key type, and gateway
  weio "do X"                Run a coding task in the current directory (default)
  weio code "do X" [-f ...]  Same, explicit; -f adds files to the context
  weio ask "question"        One-shot chat answer (no file edits)
  weio chat                  Interactive chat session
  weio ping                  Check connectivity + key
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__, config, themes
from .client import WeioClient, WeioError, WeioAuthError


def _make_client(args) -> WeioClient:
    key = config.resolve_key(getattr(args, "key", None))
    base = config.resolve_base(getattr(args, "base", None))
    model = getattr(args, "model", None) or "auto"
    return WeioClient(key, base, model=model)


def _max_tokens(args) -> int:
    return int(getattr(args, "max_tokens", 4096) or 4096)


def _c(text: str, role: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{themes.code(role)}m{text}\033[0m"


# Labels for `weio whoami`'s "key type" line, keyed by config.resolve_key_source().
_KEY_SOURCE_LABELS = {
    "browser": "session key (browser sign-in)",
    "api_key": "dashboard API key",
    "flag": "dashboard API key (--key flag)",
    "env": "dashboard API key (WEIO_API_KEY)",
    "unknown": "unknown origin — run `weio login` to refresh",
}


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

def cmd_models(args) -> int:
    """Show descriptions supplied by the authenticated server registry."""
    try:
        models = _make_client(args).models()
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 1
    for item in models:
        print(item["id"])
        if item["description"]:
            print(item["description"])
        if item["availability"]:
            print("Availability: " + item["availability"])
    return 0


def cmd_login(args) -> int:
    cli_key = getattr(args, "key", None) or getattr(args, "api_key", None)
    cli_base = getattr(args, "base", None)
    base = config.resolve_base(cli_base)
    no_browser = getattr(args, "no_browser", False)

    # Snapshot the previous identity (no network) before we clobber it, so a
    # re-auth to a different account can be called out below.
    prev = config.current_identity()

    key = cli_key
    source = "api_key" if key else None
    # Default: open the browser, sign in (incl. Google), auto-mint a key.
    if not key and not no_browser:
        try:
            from .browser_login import browser_login
            key = browser_login(base)
            if key:
                source = "browser"
        except Exception as e:
            print(_c(f"Browser sign-in unavailable ({e}).", "warning"), file=sys.stderr)
        if not key:
            print(_c("Browser sign-in didn't complete. Falling back to manual key entry.", "warning"))
    # Fallback / explicit: paste a key from Settings → API & CLI (the dashboard path).
    if not key:
        print(_c(
            "Paste an API key from Settings -> API keys at https://app.weio.ai/workspace/keys (closed beta waitlist at https://app.weio.ai/request-access).",
            "dim",
        ))
        try:
            key = input("Weio API key (weio_sk_…): ").strip()
            source = "api_key"
        except (EOFError, KeyboardInterrupt):
            print()
            return 2
    if not key:
        print("No key provided.", file=sys.stderr)
        return 2

    # Validate + fetch identity before persisting, so a bad key can't clobber a
    # working credential.
    try:
        probe = WeioClient(key, base)
        ok = probe.ping()
    except WeioError:
        ok = False
    if not ok:
        print(_c("Sign-in could not be verified. Existing saved credentials were not changed. "
                 "Check the key and gateway, then try again.", "error"), file=sys.stderr)
        return 1
    try:
        identity = probe.whoami()
    except WeioAuthError:
        print(_c("Account authentication was refused. Existing saved credentials were not changed.", "error"), file=sys.stderr)
        return 1
    except WeioError:
        # Login requires both account endpoints to agree. Usage metadata may be
        # optional later, but unavailable identity cannot replace a saved login.
        print(_c("Account details are unavailable. Existing saved credentials were not changed. Try again later.", "error"), file=sys.stderr)
        return 1

    path = config.save(key=key, base=cli_base, source=source,
                       account_email=identity.get("email"), account_tier=identity.get("tier"))
    print(f"Saved credentials to {path}")
    print("Connection check:", _c("OK", "success"))

    new_email = identity.get("email")
    if prev.get("email") and new_email and prev["email"] != new_email:
        print(_c(f"Was signed in as {prev['email']}, now {new_email}.", "prompt"))

    # Teams/Enterprise: nudge toward dashboard keys for CI/headless/rotation —
    # only when they just used the browser flow, since --api-key/paste already IS
    # that path.
    tier = identity.get("tier")
    if tier == "team" and source == "browser":
        print(_c("Note: on the Teams plan, a dashboard API key (Settings → API & CLI) is "
                 "recommended for CI, headless use, and easy rotation.", "dim"))
    return 0


def cmd_logout(args) -> int:
    removed = config.clear_auth()
    if not removed["had_key"]:
        print("Not logged in (nothing to do).")
        return 0
    who = removed.get("email") or "this device"
    print(f"Logged out {who}.")
    if os.environ.get("WEIO_API_KEY"):
        print(_c("Note: WEIO_API_KEY is still set in your environment and will keep being used "
                 "until you unset it.", "warning"))
    return 0


def cmd_whoami(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    try:
        info = client.whoami()
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 1

    source = config.resolve_key_source(getattr(args, "key", None))
    key_label = _KEY_SOURCE_LABELS.get(source, _KEY_SOURCE_LABELS["unknown"])
    tier = info.get("tier", "free")

    print(_c("  account  ", "dim") + f"{info.get('email', '?')}")
    org_name = info.get("org_name")
    if org_name:
        print(_c("  org      ", "dim") + org_name)
    print(_c("  plan     ", "dim") + str(tier))
    print(_c("  key type ", "dim") + key_label)
    print(_c("  gateway  ", "dim") + client.base)
    print(_c("  status   ", "dim") + _c("valid", "success"))

    # Refresh the cached identity (best-effort) so the next `weio logout` /
    # re-auth diff has an up-to-date "who was this" even if the key was pasted
    # in manually rather than saved via `weio login`.
    if source not in ("flag", "env"):
        config.save(account_email=info.get("email"), account_tier=tier)
    return 0


def cmd_ping(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    ok = client.ping()
    print(f"base: {client.base}")
    print("status:", _c("reachable, key valid", "success") if ok else _c("unreachable or invalid key", "error"))
    return 0 if ok else 1


def cmd_ask(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    messages = [{"role": "user", "content": args.message}]
    try:
        if args.no_stream:
            print(client.chat(messages, max_tokens=_max_tokens(args)))
        else:
            for delta in client.chat_stream(messages, max_tokens=_max_tokens(args)):
                sys.stdout.write(delta)
                sys.stdout.flush()
            print()
    except WeioError as e:
        print("\n" + _c(str(e), "error"), file=sys.stderr)
        return 1
    return 0


def cmd_chat(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    print(_c("Weio interactive chat.", "prompt"), "Ctrl-C or 'exit' to quit.\n")
    history: list[dict] = []
    while True:
        try:
            q = input(_c("you> ", "prompt")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q in ("exit", "quit"):
            break
        if q == "/ledger":
            try:
                print(_ledger_text(client, all_entries=False))
            except WeioError as e:
                print(_c(str(e), "error"), file=sys.stderr)
            continue
        history.append({"role": "user", "content": q})
        sys.stdout.write(_c("weio> ", "success"))
        sys.stdout.flush()
        acc = ""
        try:
            for delta in client.chat_stream(history, max_tokens=_max_tokens(args)):
                acc += delta
                sys.stdout.write(delta)
                sys.stdout.flush()
            print("\n")
        except WeioError as e:
            print("\n" + _c(str(e), "error"), file=sys.stderr)
            history.pop()
            continue
        history.append({"role": "assistant", "content": acc})
    return 0


def _ledger_text(client: WeioClient, *, person: str | None = None, state: str | None = None,
                 kind: str | None = None, all_entries: bool = False) -> str:
    from .ledger import format_ledger_view
    limit = 100 if all_entries else 15
    entry_state = state if state is not None else (None if all_entries else "open")
    rollup = client.ledger_rollup()
    entries_data = client.ledger_entries(
        state=entry_state,
        kind=kind,
        person=person,
        limit=limit,
    )
    entries = entries_data.get("entries") or []
    title = "Entries" if all_entries or state or kind or person else "Open entries"
    return format_ledger_view(rollup, entries, entries_title=title)


def cmd_ledger(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    try:
        print(_ledger_text(
            client,
            person=getattr(args, "person", None),
            state=getattr(args, "state", None),
            kind=getattr(args, "kind", None),
            all_entries=getattr(args, "all", False),
        ))
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 1
    return 0


def _usage_next_reset(value, now=None):
    """Display the next daily boundary, never an elapsed API window start."""
    from datetime import datetime, timedelta, timezone
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        anchor = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)
    anchor = anchor.astimezone(timezone.utc)
    current = datetime.now(timezone.utc) if now is None else now
    if current.tzinfo is None:
        raise ValueError("now must include a timezone")
    current = current.astimezone(timezone.utc)
    if anchor <= current:
        anchor += timedelta(days=(current - anchor).days + 1)
    return anchor.isoformat().replace("+00:00", "Z")


def cmd_usage(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    try:
        u = client.usage()
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 1
    used = u.get("daily_tokens_used", 0)
    limit = u.get("daily_token_limit", 0)
    print(_c(f"  account  ", "dim") + f"{u.get('email','?')}  ({u.get('tier','?')})")
    # The server uses the maximum signed 32-bit value for legacy counters on monthly plans.
    if limit == (1 << 31) - 1:
        print("  Monthly allowance: see Usage in Weio (https://app.weio.ai/workspace/usage).")
        return 0
    remaining = u.get("daily_tokens_remaining", 0)
    pct = (used / limit * 100) if limit else 0
    bar_n = int(pct / 5)
    bar = "█" * bar_n + "░" * (20 - bar_n)
    print(_c(f"  today    ", "dim") + f"{used:,} / {limit:,} tokens  ({pct:.0f}%)")
    print(_c(f"           ", "dim") + _c(bar, "usage_ok" if pct < 80 else "usage_warn"))
    print(_c(f"  remaining", "dim") + f" {remaining:,} tokens")
    if u.get("reset_at"):
        reset_at = _usage_next_reset(u["reset_at"])
        print(_c(f"  resets   ", "dim") + (reset_at or "unavailable (invalid reset timestamp)"))
    return 0


def cmd_update(args) -> int:
    from . import updater
    latest = updater.check_for_update(force=True)
    if not latest:
        print(_c(f"weio-cli {__version__} is up to date.", "success"))
        return 0
    print(_c(f"Updating weio-cli {__version__} → {latest}…", "prompt"))
    return updater.self_update()


def cmd_tui(args) -> int:
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    root = Path(getattr(args, "dir", ".") or ".").resolve()
    if not root.is_dir():
        print(_c(f"Not a directory: {root}", "error"), file=sys.stderr)
        return 2
    from .tui import run_tui
    return run_tui(client, root, max_tokens=_max_tokens(args),
                   yolo=getattr(args, "yolo", False),
                   model=getattr(args, "model", None),
                   theme=getattr(args, "theme", None))


def cmd_code(args) -> int:
    """One-shot agent run: `weio "do X"` (non-interactive)."""
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2
    root = Path(getattr(args, "dir", ".") or ".").resolve()
    if not root.is_dir():
        print(_c(f"Not a directory: {root}", "error"), file=sys.stderr)
        return 2
    # Non-interactive: auto-apply edits; run commands only in full-auto.
    full_auto = getattr(args, "auto", False) or getattr(args, "yolo", False)
    approval = "full-auto" if full_auto else "auto-edit"
    from .tui import run_once
    return run_once(client, root, args.message, approval=approval,
                    max_tokens=_max_tokens(args), model=getattr(args, "model", None),
                    theme=getattr(args, "theme", None))


def cmd_agent(args) -> int:
    """Run or list hosted Weio agents. `weio agent list` / `weio agent run <name>`."""
    action = getattr(args, "action", None)
    try:
        client = _make_client(args)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 2

    if action == "list":
        try:
            ags = client.list_agents()
        except WeioError as e:
            print(_c(str(e), "error"), file=sys.stderr)
            return 1
        if not ags:
            print("No agents yet.")
            return 0
        print(_c("Your agents:", "bold"))
        for a in ags:
            flags = []
            if a.get("enabled"):
                flags.append(_c("deployed", "success"))
            tools = ",".join(k for k, v in (a.get("tools") or {}).items() if v) or "no tools"
            tail = ("  " + " ".join(flags)) if flags else ""
            print(f"  {_c(a.get('name','?'), 'prompt')}  [{tools}]{tail}")
            if a.get("description"):
                print(_c(f"      {a['description']}", "dim"))
        return 0

    # run
    name = getattr(args, "name", None)
    if not name:
        print(_c("Usage: weio agent run <name> [--input '…'] [--local]", "error"), file=sys.stderr)
        return 2
    input_text = getattr(args, "input", "") or ""

    if getattr(args, "local", False):
        # Local runner: adopt the agent's persona and drive the on-machine coding agent
        # (file/shell tools) against the task. Persona + task are handed to the coding loop.
        agent = client.get_agent(name)
        if not agent:
            print(_c(f"No agent named or with id '{name}'.", "error"), file=sys.stderr)
            return 1
        persona = agent.get("persona") or agent.get("description") or ""
        task = input_text or "Carry out your standing instructions in this project directory."
        message = (f"You are operating as the Weio agent \"{agent.get('name')}\".\n"
                   f"{persona}\n\nTask: {task}")
        root = Path(getattr(args, "dir", ".") or ".").resolve()
        if not root.is_dir():
            print(_c(f"Not a directory: {root}", "error"), file=sys.stderr)
            return 2
        full_auto = getattr(args, "auto", False) or getattr(args, "yolo", False)
        approval = "full-auto" if full_auto else "auto-edit"
        from .tui import run_once
        print(_c(f"Running agent '{agent.get('name')}' locally in {root}…", "prompt"))
        return run_once(client, root, message, approval=approval,
                        max_tokens=_max_tokens(args), model=getattr(args, "model", None),
                        theme=getattr(args, "theme", None))

    # Hosted run: executes server-side with the agent's connectors/documents/knowledge.
    print(_c(f"Running agent '{name}' on Weio…", "prompt"))
    try:
        run = client.run_agent(name, input_text)
    except WeioError as e:
        print(_c(str(e), "error"), file=sys.stderr)
        return 1
    status = run.get("status")
    if status != "complete":
        print(_c(f"Run {status}: {run.get('error') or ''}", "error"), file=sys.stderr)
        return 1
    print(run.get("output") or "(no output)")
    srcs = run.get("sources") or []
    if srcs:
        print(_c("\nPulled from: " + ", ".join(s.get("label", s.get("source", "")) for s in srcs), "dim"))
    acts = run.get("proposed_actions") or []
    if acts:
        print(_c(f"\n{len(acts)} action(s) proposed (approve in the web app to send):", "warning"))
        for a in acts:
            print(_c(f"  • {a.get('title') or a.get('action')}"
                     + (f" [{a.get('provider')}]" if a.get("provider") else ""), "warning"))
    print(_c(f"\n{run.get('tokens_used', 0)} tokens · run {run.get('id','')}", "dim"))
    return 0


def cmd_config(args) -> int:
    from . import settings as settings_mod
    items = list(getattr(args, "items", []) or [])
    if not items:
        st = settings_mod.load()
        print(_c("weio settings:", "bold"))
        for k in ("model", "approval", "theme", "max_steps", "max_tokens", "stream"):
            print(f"  {k:11} {st.get(k)}")
        print(_c(f"\nfile: {settings_mod._file()}", "dim"))
        print(_c("set with: weio config set <key> <value>  (e.g. weio config set theme terminal)", "dim"))
        return 0
    if len(items) == 1 and items[0] != "set":
        st = settings_mod.load()
        print(f"{items[0]} = {st.get(items[0])}")
        return 0
    if items and items[0] == "set":
        if len(items) != 3:
            print(_c("Usage: weio config set <key> <value>", "error"), file=sys.stderr)
            return 2
        key, value = items[1], items[2]
    elif len(items) == 2:
        key, value = items
    else:
        print(_c("Usage: weio config [set] <key> <value>", "error"), file=sys.stderr)
        return 2
    ok, msg = settings_mod.set_value(key, value)
    print(_c(msg, "success" if ok else "error"))
    return 0 if ok else 1


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    # Shared auth/model flags — accepted both before AND after the subcommand,
    # so `weio --key X ask "q"` and `weio ask "q" --key X` both work.
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS defaults so a flag given before the subcommand isn't clobbered by
    # the subparser's default (argparse parents + subparsers share dests).
    common.add_argument("--key", "-k", default=argparse.SUPPRESS,
                        help="API key (or set WEIO_API_KEY / run `weio login`)")
    common.add_argument("--base", default=argparse.SUPPRESS,
                        help="API base URL (default https://api.weio.ai/v1)")
    common.add_argument("--model", "-m", default=argparse.SUPPRESS, help="Model id (default: auto)")
    common.add_argument("--theme", default=argparse.SUPPRESS,
                        help="CLI theme: weio or terminal (unknown names use weio)")
    common.add_argument("--max-tokens", type=int, default=argparse.SUPPRESS, help="Max output tokens")
    # Documented here for `--help`; actually consumed by a pre-scan in main() so it
    # works in any position, including bare `weio --dangerously-skip-permissions`.
    common.add_argument("--yolo", "--dangerously-skip-permissions", dest="yolo",
                        action="store_true", default=argparse.SUPPRESS,
                        help="Skip ALL approval prompts (writes + commands run unattended). Trusted dirs only.")

    p = argparse.ArgumentParser(
        prog="weio",
        parents=[common],
        description="Weio — an agentic coding assistant that routes inference through your Weio account.",
    )
    p.add_argument("--version", action="version", version=f"weio-cli {__version__}")

    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("login", parents=[common],
                        help="Sign in via browser, or --api-key for a dashboard key (re-auth replaces the saved key)")
    sp.add_argument("--no-browser", action="store_true",
                    help="Skip the browser flow and paste an API key manually")
    sp.add_argument("--api-key", metavar="KEY", default=argparse.SUPPRESS,
                    help="Use a dashboard-issued key (Settings -> API keys at https://app.weio.ai/workspace/keys) instead of "
                         "browser sign-in — the recommended path for CI/headless/Teams use")
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser("logout", parents=[common],
                        help="Clear the saved credential (idempotent)")
    sp.set_defaults(func=cmd_logout)

    sp = sub.add_parser("whoami", parents=[common],
                        help="Show the signed-in account, tier, key type, and gateway")
    sp.set_defaults(func=cmd_whoami)

    sp = sub.add_parser("models", parents=[common], help="List server model descriptions")
    sp.set_defaults(func=cmd_models)

    sp = sub.add_parser("ping", parents=[common], help="Check connectivity and key")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("ask", parents=[common], help="One-shot question (no file edits)")
    sp.add_argument("message")
    sp.add_argument("--no-stream", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("chat", parents=[common], help="Interactive chat")
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("tui", parents=[common], help="Interactive coding TUI (default when run with no args)")
    sp.add_argument("--dir", "-C", default=".", help="Project directory (default: cwd)")
    sp.set_defaults(func=cmd_tui)

    sp = sub.add_parser("usage", parents=[common], help="Show your token usage and limits")
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("ledger", parents=[common], help="Show the unified org work ledger")
    sp.add_argument("--person", metavar="NAME", help="Filter entries by assignee/person name")
    sp.add_argument("--state", metavar="X", help="Filter entries by state")
    sp.add_argument("--kind", metavar="X", help="Filter entries by kind")
    sp.add_argument("--all", action="store_true", help="Show up to 100 entries instead of 15 open entries")
    sp.set_defaults(func=cmd_ledger)

    sp = sub.add_parser("update", parents=[common], help="Update weio-cli to the latest version")
    sp.set_defaults(func=cmd_update)

    sp = sub.add_parser("code", parents=[common], help="Run an agent task non-interactively")
    sp.add_argument("message")
    sp.add_argument("--dir", "-C", default=".", help="Project directory (default: cwd)")
    sp.add_argument("--auto", action="store_true",
                    help="Full-auto: also run shell commands without asking")
    sp.set_defaults(func=cmd_code)

    sp = sub.add_parser("config", parents=[common], help="View or change CLI settings")
    sp.add_argument("items", nargs="*",
                    help="Setting command: [set] <key> <value> or <key>")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("agent", parents=[common], help="List or run your Weio agents")
    asub = sp.add_subparsers(dest="action")
    asub.add_parser("list", parents=[common], help="List your agents")
    rp = asub.add_parser("run", parents=[common], help="Run an agent (hosted, or --local)")
    rp.add_argument("name", help="Agent name or id")
    rp.add_argument("--input", "-i", default="", help="Task/instruction for this run")
    rp.add_argument("--local", action="store_true",
                    help="Run on THIS machine with local file/shell tools, using the agent's persona")
    rp.add_argument("--dir", "-C", default=".", help="Project directory for --local (default: cwd)")
    rp.add_argument("--auto", action="store_true", help="Full-auto for --local (also runs shell commands)")
    sp.set_defaults(func=cmd_agent)

    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Pre-scan the skip-permissions flag from anywhere in argv so it works even on
    # the bare `weio --dangerously-skip-permissions` (which routes to the TUI).
    yolo = False
    _yolo_flags = ("--yolo", "--dangerously-skip-permissions")
    if any(f in argv for f in _yolo_flags):
        yolo = True
        argv = [a for a in argv if a not in _yolo_flags]

    known = {"login", "logout", "whoami", "models", "ping", "ask", "chat", "code", "tui", "update",
             "usage", "ledger", "config", "agent"}
    # Bare `weio` (no args) → interactive coding TUI, like Claude Code.
    if not argv:
        argv = ["tui"]
    # Bare `weio "do something"` (a message, not a flag/command) → one-shot code.
    elif argv[0] not in known and not argv[0].startswith("-"):
        argv = ["code", *argv]

    args = parser.parse_args(argv)
    from . import settings as settings_mod
    themes.set_active_theme(getattr(args, "theme", None) or settings_mod.load().get("theme"))
    if yolo:
        args.yolo = True
    if not getattr(args, "command", None):
        args.command = "tui"
        args.func = cmd_tui
    try:
        rc = args.func(args)
    except KeyboardInterrupt:
        print()
        return 130
    # Throttled, fail-silent update notice (skipped for tui — it shows its own
    # in the header — and for the update command itself).
    if args.command not in ("update", "tui"):
        try:
            from . import updater
            notice = updater.update_notice()
            if notice:
                print(_c("\n" + notice, "warning"), file=sys.stderr)
        except Exception:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
