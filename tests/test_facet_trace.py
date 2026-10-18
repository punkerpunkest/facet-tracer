"""Behavioural tests for the tracer. Run with: .venv/bin/python -m pytest -q"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "facet_trace.py")


def run_tool(cwd, *args):
    result = subprocess.run(
        [sys.executable, TOOL, *args], capture_output=True, text=True, cwd=str(cwd)
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def trace(tmp_path, source: str, *args: str, name: str = "s.py"):
    (tmp_path / name).write_text(textwrap.dedent(source))
    return run_tool(tmp_path, "trace", "--script", name, *args)


NESTED = """
    def leaf(x):
        return x * 2

    def middle(x):
        return leaf(x) + 1

    def target(x):
        return middle(x)

    target(3)
    target(4)
"""


# ----------------------------------------------------------------- capture


def test_tree_shows_nesting_and_return_values(tmp_path):
    out = trace(tmp_path, NESTED, "--trigger", "target")
    assert "target(x=3)" in out
    assert "middle(x=3)" in out
    assert "leaf(x=3) -> 6" in out
    assert "-> 7" in out


def test_trigger_rearms_across_invocations(tmp_path):
    out = trace(tmp_path, NESTED, "--trigger", "target")
    assert "trigger fired   2 time(s), recorded 2" in out
    assert "target(x=3)" in out and "target(x=4)" in out


def test_max_invocations_caps_recording_but_not_counting(tmp_path):
    out = trace(tmp_path, NESTED, "--trigger", "target", "--max-invocations", "1")
    assert "trigger fired   2 time(s), recorded 1" in out
    assert "target(x=3)" in out
    assert "target(x=4)" not in out


def test_out_of_scope_modules_are_reported(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "inner.py").write_text("def hidden(x):\n    return x + 1\n")
    source = """
        from pkg.inner import hidden

        def target(x):
            return hidden(x)

        target(1)
    """
    out = trace(tmp_path, source, "--trigger", "target", "--prefixes", "nothing_matches")
    assert "touched but not recorded" in out
    assert "pkg/inner.py" in out
    assert "widen --prefixes" in out


def test_script_directory_is_importable(tmp_path):
    """`python script.py` puts the script's dir on sys.path; runpy does not."""
    (tmp_path / "sibling.py").write_text("def helper():\n    return 42\n")
    source = """
        from sibling import helper

        def target():
            return helper()

        target()
    """
    out = trace(tmp_path, source, "--trigger", "target")
    assert "helper() -> 42" in out
    assert "ModuleNotFoundError" not in out


def test_module_mode_runs_a_module(tmp_path):
    pkg = tmp_path / "demo"
    pkg.mkdir()
    pkg.joinpath("__init__.py").write_text("")
    pkg.joinpath("__main__.py").write_text(
        "def target():\n    return 7\n\ntarget()\n"
    )
    out = run_tool(tmp_path, "trace", "--module", "demo", "--trigger", "target")
    assert "target() -> 7" in out


def test_trigger_that_never_fires_warns_instead_of_looking_empty(tmp_path):
    out = trace(tmp_path, NESTED, "--trigger", "no_such_function")
    assert "trigger never fired" in out
    assert "(empty)" in out


def test_memory_addresses_are_normalised(tmp_path):
    source = """
        class Thing:
            pass

        def target(obj):
            return obj

        target(Thing())
    """
    out = trace(tmp_path, source, "--trigger", "target")
    assert "0xADDR" in out
    assert "object at 0x" not in out.replace("0xADDR", "")


def test_comprehension_frames_excluded_by_default(tmp_path):
    source = """
        def target(n):
            return sum(i for i in range(n))

        target(5)
    """
    assert "<genexpr>" not in trace(tmp_path, source, "--trigger", "target")
    assert "<genexpr>" in trace(
        tmp_path, source, "--trigger", "target", "--include-comprehensions"
    )


def test_repeated_sibling_calls_are_collapsed(tmp_path):
    source = """
        def helper(i):
            return i

        def target(n):
            return [helper(i) for i in range(n)]

        target(30)
    """
    out = trace(tmp_path, source, "--trigger", "target")
    assert "helper(i=0)" in out
    assert "x29 more" in out


def test_module_frame_namespace_is_suppressed(tmp_path):
    out = trace(tmp_path, NESTED)
    assert "<module>()" in out
    assert "__builtins__" not in out


def test_over_cap_suggests_a_depth_that_fits(tmp_path):
    out = trace(tmp_path, NESTED, "--trigger", "target", "--cap-events", "4")
    assert "OVER CAP" in out
    assert "--depth" in out


def test_script_failure_is_reported_not_swallowed(tmp_path):
    source = """
        def target():
            return 1

        target()
        assert False, "boom"
    """
    out = trace(tmp_path, source, "--trigger", "target")
    assert "AssertionError" in out and "boom" in out
    assert "target()" in out


def test_json_mode_emits_valid_json(tmp_path):
    payload = json.loads(trace(tmp_path, NESTED, "--trigger", "target", "--json"))
    assert payload["invocations"] == 2
    assert payload["events_recorded"] > 0
    assert "target" in payload["tree"]


# ------------------------------------------------------------- persistence


def test_event_file_is_written_and_parseable(tmp_path):
    trace(tmp_path, NESTED, "--trigger", "target", "--out", "events.jsonl")
    rows = [
        json.loads(line)
        for line in (tmp_path / "events.jsonl").read_text().splitlines()
    ]
    assert rows, "no events written"
    assert rows[0]["k"] == "call" and rows[0]["f"] == "target"
    assert {"inv", "d", "k", "m", "f"} <= set(rows[0])


def test_metadata_sidecar_records_the_window(tmp_path):
    trace(
        tmp_path, NESTED, "--trigger", "target", "--depth", "4",
        "--label", "the thing", "--out", "events.jsonl",
    )
    meta = json.loads((tmp_path / "events.jsonl.meta.json").read_text())
    assert meta["label"] == "the thing"
    assert meta["trigger"] == "target"
    assert meta["depth"] == 4


# ---------------------------------------------------------------- coverage


DJANGO_SHAPED = {
    "app/__init__.py": "",
    "app/core.py": (
        "def encode(text, escape=True):\n"
        "    return text.upper() if escape else text\n"
    ),
    "app/forms.py": (
        "from app.core import encode\n\n"
        "def prepare_value(v):\n    return encode(v)\n"
    ),
    "app/admin.py": (
        "from app.core import encode\n\n"
        "def display_for_field(v):\n    return encode(v)\n"
    ),
    "f1.py": "from app.forms import prepare_value\nprepare_value('x')\n",
    "f2.py": "from app.admin import display_for_field\ndisplay_for_field('x')\n",
}


@pytest.fixture
def two_facets(tmp_path):
    """Two behaviours sharing one encoder, each reached by its own path."""
    for name, body in DJANGO_SHAPED.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    run_tool(tmp_path, "trace", "--script", "f1.py", "--trigger", "prepare_value",
             "--prefixes", "app", "--label", "form path", "--out", "f1.jsonl")
    run_tool(tmp_path, "trace", "--script", "f2.py", "--trigger", "display_for_field",
             "--prefixes", "app", "--label", "admin path", "--out", "f2.jsonl")
    return tmp_path


def test_cover_flags_the_facet_the_patch_cannot_have_reached(two_facets):
    out = run_tool(
        two_facets, "cover", "f1.jsonl", "f2.jsonl", "--changed", "app/forms.py"
    )
    form, admin = out.split("facet  admin path")
    assert "INCONCLUSIVE" in form
    assert "NOT ADDRESSED" in admin
    assert "app/admin.py" in admin          # tells the agent where this facet runs
    assert "1 of 2 facet(s) provably not addressed." in out


def test_cover_clears_both_facets_when_the_shared_file_is_patched(two_facets):
    out = run_tool(
        two_facets, "cover", "f1.jsonl", "f2.jsonl", "--changed", "app/core.py"
    )
    assert "NOT ADDRESSED" not in out
    assert out.count("INCONCLUSIVE") == 2
    assert "none provably untouched" in out


def test_cover_uses_facet_labels(two_facets):
    out = run_tool(two_facets, "cover", "f1.jsonl", "--changed", "app/core.py")
    assert "facet  form path" in out


def test_cover_reads_the_working_tree_when_no_changed_list_given(two_facets):
    subprocess.run(["git", "init", "-q"], cwd=two_facets, check=True)
    subprocess.run(["git", "add", "-A"], cwd=two_facets, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
        cwd=two_facets, check=True,
    )
    (two_facets / "app" / "forms.py").write_text(
        "from app.core import encode\n\ndef prepare_value(v):\n    return encode(v, escape=False)\n"
    )
    out = run_tool(two_facets, "cover", "f1.jsonl", "f2.jsonl")
    assert "app/forms.py" in out
    assert "NOT ADDRESSED" in out


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
