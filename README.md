# weio-cli

An agentic coding assistant that runs on your machine and routes inference
through your [Weio](https://weio.ai) account.

## Install

```bash
pip install weio-ai        # or: pipx install weio-ai
```

If you installed the previous `weio-cli` distribution, uninstall it with the same tool before installing `weio-ai`. Both distributions use the `weio` command.

Requires Python 3.9+.

## Authenticate

Weio is in closed beta. New accounts need an approved invitation, and plan
purchases are paused. Sign in to an eligible account and create a revocable API
key in [Settings → API keys](https://app.weio.ai/workspace/keys).

```bash
weio login                 # get a short code for https://app.weio.ai/cli/device
weio login --loopback      # or sign in in a browser on this same computer
weio login --no-browser    # or paste a CLI and coding agent key from Settings → API keys
weio whoami
```

Open the displayed link, enter the short code, and approve the request in any
signed-in browser. The code expires after 10 minutes and works once. Device
sign-in uses the default Weio gateway and creates a revocable key with the
chat, documents, and hosted-agent scopes. Hosted-agent routes remain closed
during beta. For a custom gateway, paste that gateway's key with
`weio login --no-browser`.
For CI, supply `WEIO_API_KEY` from your secret store. The key is never needed
in a command argument.

```bash
weio usage             # view usage reported for your account
weio logout            # clears the credential saved by login
```

Revoke an issued key from Settings → API keys if you no longer need it. Logout
does not unset an environment variable set by your shell.

## Use

### Interactive agent (default)

Run `weio` with no arguments to start an autonomous coding agent in your terminal —
like Claude Code / Codex. It reads files, searches, edits, and runs commands to
complete your task, showing each step:

```bash
cd my-project
weio
```

```
weio › add a /health route to app.py and run the tests

● Let me look at the app first.
  📖 read_file app.py
● Adding the route.
  ✎ edit_file app.py
  │ + @app.get("/health")
  │ + def health(): return {"ok": True}
  apply edit_file to app.py? [y/N] y
● Verifying.
  ❯ run_command pytest -q
  │ exit=0  …  5 passed
✔ Added /health and confirmed tests pass.
```

The agent uses tools: **read_file, list_dir, search, edit_file, write_file,
run_command**. File paths are confined to the project directory.

Slash commands: `/model <id>`, `/approval <mode>`, `/steps <n>`, `/settings`,
`/undo`, `/clear`, `/cwd`, `/help`, `/exit`.

### Approval modes

Control how much the agent does without asking (`/approval` or `weio config approval`):

| Mode | Edits | Shell commands |
|---|---|---|
| `suggest` (default) | ask each time | ask each time |
| `auto-edit` | auto-apply | ask each time |
| `full-auto` | auto-apply | auto-run |

### Settings

```bash
weio config                      # show all settings
weio config approval auto-edit   # set a value
weio config model pro            # auto | low | mid | pro
weio config max_steps 40
```

Settings live in `~/.weio/settings.json`. The agent uses the most capable model
your plan allows.

### One-shot & other commands

```bash
# Run a coding task non-interactively (auto-applies edits; add --auto to also run commands):
weio "add error handling to the fetch() in api.py"
weio code "refactor db.py to async" --auto

# Quick question (no file edits):
weio ask "what does a 502 from nginx usually mean?"

weio chat        # plain interactive chat
weio usage       # usage reported for your account
weio whoami      # account, plan, key type, gateway, validity
weio ping        # connectivity + key check
weio logout      # clear the saved credential
```

Edits are shown as a diff and require confirmation before anything is written
(use `--yolo` to apply automatically). New files are created as needed.

## Updating

weio-cli checks PyPI once a day (fail-silent) and prints a one-line notice when a
newer version is available. To upgrade:

```bash
weio update           # upgrades in place via pip
# or
pip install -U weio-ai
```

Disable the check with `WEIO_NO_UPDATE_CHECK=1`.

## Configuration

| Setting | Flag | Env | Config file |
|---|---|---|---|
| API key | `--key` (any command) / `--api-key` (`weio login` only) | `WEIO_API_KEY` | `~/.weio/config.json` |
| API base | `--base` | `WEIO_BASE` | `~/.weio/config.json` |
| Model | `--model` | — | — |

Resolution order for the key (first hit wins): `--key`/`--api-key` flag →
`WEIO_API_KEY` env → the key saved by `weio login` → none (commands that need
one will ask you to log in). `weio whoami` reports which of these is active.

Using a different OpenAI-compatible endpoint (e.g. a gateway on your LAN)? Point at it with `--base http://HOST:8901/v1`.

Output is billed against your Weio account usage.


## Release 1.0.4

The verified install and API-key workflow covers Linux with Python. The wheel
and source distribution are Python packages. See [Downloads](https://weio.ai/download)
for current availability of other platforms and standalone installers.

Weio is run by AI agents, with a human founder.
