"""CLI-V2: session token footer + never-bootstrap-venv verification rule."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import agent as agent_mod


class _FakeClient:
    session_tokens = 4321


def _mk_agent():
    cbs = agent_mod.Callbacks(on_text=lambda t: None, on_action=lambda t, a: None,
                              on_result=lambda t, r: None, approve=lambda t, a, r: True,
                              on_status=lambda s: None, on_finish=lambda s: None)
    return agent_mod.Agent(_FakeClient(), Path("."), {}, cbs)


def test_session_line_appended():
    a = _mk_agent()
    out = a._with_session_line("Done.", 7)
    assert "7 steps" in out and "4,321 tokens" in out


def test_session_line_absent_without_usage():
    a = _mk_agent()
    a.client.session_tokens = 0
    assert a._with_session_line("Done.", 3) == "Done."


def test_prompt_forbids_venv_bootstrap():
    sp = agent_mod.system_prompt(Path("/tmp/x"))
    assert "NEVER create a new virtualenv" in sp
    assert "VERIFY IN THE ENVIRONMENT THAT EXISTS" in sp


def test_tool_named_fence_parsed_as_action():
    from weio_cli.agent import parse_actions, strip_actions
    t = 'Fixed.\n```finish\n{"summary":"done"}\n```'
    assert parse_actions(t) == [{"tool": "finish", "args": {"summary": "done"}}]
    assert strip_actions(t) == "Fixed."


def test_code_fences_survive_stripping():
    from weio_cli.agent import parse_actions, strip_actions
    t = 'Run:\n```bash\npytest -q\n```'
    assert parse_actions(t) == []
    assert "pytest -q" in strip_actions(t)
