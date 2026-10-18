"""Behavioural tests for the tracer. Run with: .venv/bin/python -m pytest -q"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "facet_trace.py")


def trace(tmp_path, source: str, *args: str, script_name: str = "s.py"):
    script = tmp_path / script_name
    script.write_text(textwrap.dedent(source))
    result = subprocess.run(
        [sys.executable, TOOL, str(script), *args],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


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
        import sys; sys.path.insert(0, '.')
        from pkg.inner import hidden

        def target(x):
            return hidden(x)

        target(1)
    """
    out = trace(tmp_path, source, "--trigger", "target", "--prefixes", "nothing_matches")
    assert "touched but not recorded" in out
    assert "pkg/inner.py" in out
    assert "widen --prefixes" in out


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


def test_event_file_is_written_and_parseable(tmp_path):
    events = tmp_path / "events.jsonl"
    trace(tmp_path, NESTED, "--trigger", "target", "--out", str(events))
    rows = [json.loads(line) for line in events.read_text().splitlines()]
    assert rows, "no events written"
    assert rows[0]["k"] == "call" and rows[0]["f"] == "target"
    assert {"inv", "d", "k", "m", "f"} <= set(rows[0])


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
