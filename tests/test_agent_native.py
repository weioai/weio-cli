"""weio-cli 1.0: native tool-calling agent loop (ops #129).

- When the client's gateway probe reports tool_calling support, the agent runs
  the native loop: tool_calls come back structured, get executed via the same
  tools.py functions and the same approval/loop-guard bounds as the fence path,
  and results are fed back as role:"tool" messages keyed by tool_call_id.
- When the probe reports no support (or the client doesn't even have the
  method -- an older/fake client), the agent falls back to the ORIGINAL
  fence-based loop unchanged.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import agent as agent_mod  # noqa: E402


def _cb():
    events = {"text": [], "actions": [], "results": [], "finish": None}

    def on_text(t):
        events["text"].append(t)

    def on_action(tool, args):
        events["actions"].append((tool, args))

    def on_result(tool, r):
        events["results"].append((tool, r))

    def on_finish(s):
        events["finish"] = s

    cb = agent_mod.Callbacks(on_text=on_text, on_action=on_action, on_result=on_result,
                             approve=lambda t, a, r: True, on_status=lambda s: None, on_finish=on_finish)
    return cb, events


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class _NativeClient:
    """Scripted client: probe_tool_calling() -> True, chat_with_tools() replays a
    queued list of responses (one per agent step)."""

    session_tokens = 0

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def probe_tool_calling(self) -> bool:
        return True

    def chat_with_tools(self, messages, *, tools=None, tool_choice=None,
                        max_tokens=2048, temperature=0.7, model=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        return self._responses.pop(0)


class _FenceClient:
    """No probe_tool_calling method at all -- mirrors a client talking to an older
    gateway build, or a test double that predates ops #129."""

    session_tokens = 0

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = []

    def chat(self, messages, *, max_tokens=2048, temperature=0.7, model=None):
        self.calls.append([dict(m) for m in messages])
        return self._replies.pop(0)


def _mk_agent(client, root: Path, settings=None):
    cb, events = _cb()
    a = agent_mod.Agent(client, root, settings or {}, cb)
    return a, events


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def test_native_mode_detected_when_client_probe_is_true(tmp_path):
    a, _ = _mk_agent(_NativeClient([]), tmp_path)
    assert a.native is True
    assert a.messages[0]["content"] == agent_mod.system_prompt_native(tmp_path)


def test_fence_mode_when_client_has_no_probe_method(tmp_path):
    a, _ = _mk_agent(_FenceClient([]), tmp_path)
    assert a.native is False
    assert a.messages[0]["content"] == agent_mod.system_prompt(tmp_path)


def test_native_prompt_has_no_fence_instructions(tmp_path):
    sp = agent_mod.system_prompt_native(tmp_path)
    assert "```action" not in sp
    assert "TOOLS (args shown)" not in sp


def test_fence_prompt_unchanged_still_has_fence_instructions(tmp_path):
    sp = agent_mod.system_prompt(tmp_path)
    assert "```action" in sp
    assert "TOOLS (args shown)" in sp


def test_probe_exception_falls_back_to_fence_mode(tmp_path):
    class _Boom:
        session_tokens = 0

        def probe_tool_calling(self):
            raise RuntimeError("network down")

    a, _ = _mk_agent(_Boom(), tmp_path)
    assert a.native is False


# ---------------------------------------------------------------------------
# Native loop: single tool call, then finish
# ---------------------------------------------------------------------------

def test_native_single_tool_call_then_finish(tmp_path):
    (tmp_path / "a.py").write_text("print('hi')\n")
    client = _NativeClient([
        {"content": "Reading the file.", "tool_calls": [_tool_call("call_1", "read_file", {"path": "a.py"})],
         "finish_reason": "tool_calls"},
        {"content": None, "tool_calls": [_tool_call("call_2", "finish", {"summary": "Done reading."})],
         "finish_reason": "tool_calls"},
    ])
    a, events = _mk_agent(client, tmp_path)
    result = a.run_task("read a.py")

    assert result == "Done reading."
    assert events["finish"].startswith("Done reading.")
    assert events["actions"][0] == ("read_file", {"path": "a.py"})

    # tool_call_id round-trips onto the fed-back tool message
    tool_msgs = [m for m in a.messages if m.get("role") == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "print" in tool_msgs[0]["content"]
    assert tool_msgs[1]["tool_call_id"] == "call_2"


def test_native_multiple_readonly_tool_calls_in_one_turn(tmp_path):
    (tmp_path / "a.py").write_text("A\n")
    (tmp_path / "b.py").write_text("B\n")
    client = _NativeClient([
        {"content": "Reading both.", "tool_calls": [
            _tool_call("call_1", "read_file", {"path": "a.py"}),
            _tool_call("call_2", "read_file", {"path": "b.py"}),
        ], "finish_reason": "tool_calls"},
        {"content": None, "tool_calls": [_tool_call("call_3", "finish", {"summary": "Read both files."})],
         "finish_reason": "tool_calls"},
    ])
    a, events = _mk_agent(client, tmp_path)
    result = a.run_task("read both files")

    assert result == "Read both files."
    assert [act[0] for act in events["actions"]] == ["read_file", "read_file"]
    tool_msgs = [m for m in a.messages if m.get("role") == "tool" and m.get("name") == "read_file"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["tool_call_id"] == "call_1" and "A" in tool_msgs[0]["content"]
    assert tool_msgs[1]["tool_call_id"] == "call_2" and "B" in tool_msgs[1]["content"]


def test_native_write_tool_respects_approval_gate(tmp_path):
    approvals = []

    def approve(tool, args, r):
        approvals.append((tool, args))
        return False   # decline

    cb = agent_mod.Callbacks(on_text=lambda t: None, on_action=lambda t, a: None,
                             on_result=lambda t, r: None, approve=approve,
                             on_status=lambda s: None, on_finish=lambda s: None)
    client = _NativeClient([
        {"content": "Writing.", "tool_calls": [_tool_call("call_1", "write_file",
                                                          {"path": "new.py", "content": "x = 1\n"})],
         "finish_reason": "tool_calls"},
        {"content": None, "tool_calls": [_tool_call("call_2", "finish", {"summary": "Declined write."})],
         "finish_reason": "tool_calls"},
    ])
    a = agent_mod.Agent(client, tmp_path, {"approval": "suggest"}, cb)
    result = a.run_task("write new.py")

    assert result == "Declined write."
    assert not (tmp_path / "new.py").exists()
    assert approvals and approvals[0][0] == "write_file"
    tool_msgs = [m for m in a.messages if m.get("role") == "tool" and m.get("name") == "write_file"]
    assert "declined" in tool_msgs[0]["content"].lower()


def test_native_unknown_tool_reported_back_without_crashing(tmp_path):
    client = _NativeClient([
        {"content": "Trying something odd.", "tool_calls": [_tool_call("call_1", "teleport", {})],
         "finish_reason": "tool_calls"},
        {"content": None, "tool_calls": [_tool_call("call_2", "finish", {"summary": "Gave up."})],
         "finish_reason": "tool_calls"},
    ])
    a, _ = _mk_agent(client, tmp_path)
    result = a.run_task("do something impossible")
    assert result == "Gave up."
    tool_msgs = [m for m in a.messages if m.get("role") == "tool" and m.get("tool_call_id") == "call_1"]
    assert "Unknown tool" in tool_msgs[0]["content"]


def test_native_no_tool_call_nudges_then_accepts(tmp_path):
    client = _NativeClient([
        {"content": "Thinking out loud.", "tool_calls": [], "finish_reason": "stop"},
        {"content": "Still thinking.", "tool_calls": [], "finish_reason": "stop"},
        {"content": "Okay, final answer.", "tool_calls": [], "finish_reason": "stop"},
    ])
    a, events = _mk_agent(client, tmp_path)
    result = a.run_task("just answer in words")
    assert result == "Okay, final answer."
    assert events["finish"] == "Okay, final answer."
    # two nudge messages were injected before the reply was accepted
    nudge_msgs = [m for m in a.messages if m.get("role") == "user" and "next step now" in m.get("content", "").lower()]
    assert len(nudge_msgs) == 2


def test_native_step_limit_reached(tmp_path):
    (tmp_path / "a.py").write_text("x\n")
    # Every step re-reads the same file and never calls finish.
    responses = [
        {"content": "Reading again.", "tool_calls": [_tool_call(f"call_{i}", "read_file", {"path": "a.py"})],
         "finish_reason": "tool_calls"}
        for i in range(3)
    ]
    client = _NativeClient(responses)
    a, events = _mk_agent(client, tmp_path, {"max_steps": 3})
    result = a.run_task("keep reading")
    assert result == "step-limit"
    assert "step limit" in events["finish"].lower()


def test_native_tool_schemas_cover_the_fence_tool_vocabulary():
    schemas = agent_mod.tool_schemas()
    names = {s["function"]["name"] for s in schemas}
    assert names == agent_mod.ALL_TOOLS
    for s in schemas:
        assert s["type"] == "function"
        assert "parameters" in s["function"]


# ---------------------------------------------------------------------------
# Fence fallback stays intact end-to-end via run_task()
# ---------------------------------------------------------------------------

def test_fence_run_task_end_to_end_unchanged(tmp_path):
    (tmp_path / "a.py").write_text("hi\n")
    client = _FenceClient([
        'Reading it.\n```action\n{"tool": "read_file", "args": {"path": "a.py"}}\n```',
        'Done.\n```action\n{"tool": "finish", "args": {"summary": "Read the file."}}\n```',
    ])
    a, events = _mk_agent(client, tmp_path)
    assert a.native is False
    result = a.run_task("read a.py")
    assert result == "Read the file."
    assert events["actions"][0] == ("read_file", {"path": "a.py"})
    # fence mode still uses the OBSERVATIONS: user-turn shape, not role:"tool"
    assert any(m.get("role") == "user" and str(m.get("content", "")).startswith("OBSERVATIONS")
              for m in a.messages)
    assert not any(m.get("role") == "tool" for m in a.messages)


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
