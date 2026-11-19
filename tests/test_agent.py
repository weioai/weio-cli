"""Tests for the agent tool layer and action parsing."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weio_cli import tools, agent  # noqa: E402


# ---- path safety ----
def test_read_file_confined(tmp_path):
    (tmp_path / "a.txt").write_text("hello")
    r = tools.read_file(tmp_path, "a.txt")
    assert r.ok and "hello" in r.output


def test_path_escape_refused(tmp_path):
    r = tools.read_file(tmp_path, "../../etc/passwd")
    assert not r.ok and "outside" in r.output.lower()


def test_list_dir(tmp_path):
    (tmp_path / "x.py").write_text("x")
    (tmp_path / "sub").mkdir()
    r = tools.list_dir(tmp_path, ".")
    assert r.ok and "x.py" in r.output and "sub/" in r.output


def test_search(tmp_path):
    (tmp_path / "a.py").write_text("def foo():\n    return 1\n")
    r = tools.search_files(tmp_path, "foo")
    assert r.ok and "a.py" in r.output


# ---- edits ----
def test_edit_file_match_and_commit(tmp_path):
    fp = tmp_path / "m.py"
    fp.write_text("a = 1\nb = 2\n")
    r = tools.edit_file(tmp_path, "m.py", "a = 1", "a = 99")
    assert r.ok and "a = 99" in r.new_text
    assert tools.commit_write(tmp_path, r.path, r.new_text)
    assert "a = 99" in fp.read_text()


def test_edit_file_no_match(tmp_path):
    (tmp_path / "m.py").write_text("a = 1\n")
    r = tools.edit_file(tmp_path, "m.py", "zzz", "qqq")
    assert not r.ok and "not found" in r.output.lower()


def test_write_new_file(tmp_path):
    r = tools.write_file(tmp_path, "new.py", "print('hi')\n")
    assert r.ok and r.created
    tools.commit_write(tmp_path, r.path, r.new_text)
    assert (tmp_path / "new.py").read_text() == "print('hi')\n"


def test_run_command(tmp_path):
    r = tools.run_command(tmp_path, "echo weio-ok")
    assert r.ok and "weio-ok" in r.output


def test_make_dir_with_spaces(tmp_path):
    r = tools.make_dir(tmp_path, "My Project Folder")
    assert r.ok and (tmp_path / "My Project Folder").is_dir()


def test_make_dir_nested(tmp_path):
    r = tools.make_dir(tmp_path, "a/b/c")
    assert r.ok and (tmp_path / "a" / "b" / "c").is_dir()


def test_make_dir_confined(tmp_path):
    r = tools.make_dir(tmp_path, "../escape")
    assert not r.ok and "outside" in r.output.lower()


# ---- action parsing ----
def test_parse_single_action():
    text = 'I will read it.\n```action\n{"tool": "read_file", "args": {"path": "a.py"}}\n```'
    acts = agent.parse_actions(text)
    assert len(acts) == 1 and acts[0]["tool"] == "read_file"
    assert acts[0]["args"]["path"] == "a.py"


def test_parse_multiple_actions():
    text = ('```action\n{"tool": "list_dir", "args": {"path": "."}}\n```\n'
            '```action\n{"tool": "finish", "args": {"summary": "done"}}\n```')
    acts = agent.parse_actions(text)
    assert [a["tool"] for a in acts] == ["list_dir", "finish"]


def test_strip_actions_leaves_narration():
    text = 'Reading the file.\n```action\n{"tool":"read_file","args":{"path":"a"}}\n```'
    assert agent.strip_actions(text) == "Reading the file."


def test_parse_ignores_bad_json():
    text = '```action\n{not valid json}\n```'
    assert agent.parse_actions(text) == []


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
