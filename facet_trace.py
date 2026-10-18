#!/usr/bin/env python3
"""
facet-trace — capture one scoped execution trace of a Python script.

Runs a script in-process under a call/return/exception tracer, filtered by a
temporal trigger (start recording when a named function is entered), a spatial
whitelist (only record frames whose file matches one of the given prefixes),
and a depth limit relative to the trigger.

Emits a header, a deterministically rendered call tree, and a canonical event
list on disk. No model is involved anywhere in this file.

Differences from DAIRA's run_hunter_trace, which this is modelled on:
  - prefixes is a list, not a single substring
  - recording is not stopped on the trigger's return, so repeated invocations
    are captured (bounded by --max-invocations) instead of silently dropped
  - events discarded by the whitelist are counted and reported, so a wrong
    scope is visible rather than indistinguishable from an empty trace
  - the tree is built by this file rather than written by a model
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import runpy
import sys
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field

import hunter
from hunter import And, Or, Q, When

KINDS = ("call", "return", "exception")
REPR_LIMIT = 160
MAX_ARGS = 8

# Comprehension and generator frames fire once per iteration, so a loop over a
# large collection buries the trace in frames that carry no diagnostic weight.
# Excluded by default; --include-comprehensions brings them back when a bug is
# suspected inside one.
SYNTHETIC_FRAMES = frozenset({"<genexpr>", "<listcomp>", "<dictcomp>", "<setcomp>"})

# The one normalisation that is always safe: object reprs embed the address the
# object happened to land at, which differs every run and means nothing.
ADDRESS = re.compile(r"0x[0-9a-fA-F]{4,}")

_TOOL_FILE = os.path.basename(__file__)


def is_tool_frame(module: str) -> bool:
    """
    Frames belonging to the interpreter's own plumbing or to this tool. They are
    never the answer, and letting them into the out-of-scope report would point
    an agent at <frozen runpy> as a place the bug might be hiding.
    """
    return (
        module.startswith("<frozen ")
        or module.endswith(_TOOL_FILE)
        or f"{os.sep}hunter{os.sep}" in module
    )


# ---------------------------------------------------------------- utilities


def safe_repr(value, limit: int = REPR_LIMIT) -> str:
    """repr() that never raises and never runs away."""
    try:
        text = repr(value)
    except BaseException:
        return f"<unreprable {type(value).__name__}>"
    text = ADDRESS.sub("0xADDR", text)
    if len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def norm_path(path: str, root: str) -> str:
    """Repo-relative where possible, so traces are comparable across checkouts."""
    try:
        absolute = os.path.abspath(path)
        if absolute.startswith(root + os.sep):
            return os.path.relpath(absolute, root)
    except (ValueError, OSError):
        pass
    return path


# ------------------------------------------------------------------ capture


@dataclass
class Record:
    invocation: int
    depth: int
    kind: str
    module: str
    function: str
    args: dict | None = None
    value: str | None = None

    def to_json(self) -> dict:
        out = {
            "inv": self.invocation,
            "d": self.depth,
            "k": self.kind,
            "m": self.module,
            "f": self.function,
        }
        if self.args is not None:
            out["a"] = self.args
        if self.value is not None:
            out["v"] = self.value
        return out


class Recorder:
    """
    Single Hunter action. Every event inside the trigger's extent reaches it,
    and it decides whether to record or merely count. Keeping both jobs in one
    action avoids relying on how Hunter's Or() short-circuits across When()s.
    """

    def __init__(self, prefixes: list[str], max_invocations: int, root: str,
                 include_comprehensions: bool = False, has_trigger: bool = True,
                 max_events: int = 100_000):
        self.has_trigger = has_trigger
        self.include_comprehensions = include_comprehensions
        self.prefixes = prefixes
        self.max_invocations = max_invocations
        self.root = root

        self.records: list[Record] = []
        self.census: Counter[str] = Counter()      # every module seen in extent
        self.recorded_modules: Counter[str] = Counter()
        self.depth_histogram: Counter[int] = Counter()

        self.invocations = 0                        # times the trigger fired
        self.recorded_invocations = 0
        self.max_events = max_events
        self.truncated = False

    def in_scope(self, module: str) -> bool:
        if not self.prefixes:
            return True
        return any(prefix in module for prefix in self.prefixes)

    def __call__(self, event) -> None:
        module = norm_path(event.filename, self.root)
        kind = event.kind
        depth = event.depth

        if is_tool_frame(module):
            return
        if not self.include_comprehensions and event.function in SYNTHETIC_FRAMES:
            return

        if self.has_trigger and kind == "call" and depth == 0:
            self.invocations += 1
            if self.invocations <= self.max_invocations:
                self.recorded_invocations += 1

        self.census[module] += 1

        if self.has_trigger and self.invocations > self.max_invocations:
            return
        if not self.in_scope(module):
            return

        self.recorded_modules[module] += 1
        self.depth_histogram[depth] += 1

        # Safety valve, not a normal path: the advisory cap is meant to send the
        # agent back with a shallower depth long before this trips.
        if len(self.records) >= self.max_events:
            self.truncated = True
            return

        record = Record(
            invocation=max(self.invocations - 1, 0),
            depth=depth,
            kind=kind,
            module=module,
            function=event.function,
        )
        if kind == "call":
            # A <module> frame's locals are the module's entire namespace, which
            # is thousands of tokens of imports and dunders and never the answer.
            if event.function == "<module>":
                record.args = {}
            else:
                try:
                    items = list(event.locals.items())
                except BaseException:
                    items = []
                record.args = {k: safe_repr(v) for k, v in items[:MAX_ARGS]}
                if len(items) > MAX_ARGS:
                    record.args["..."] = f"+{len(items) - MAX_ARGS} more"
        else:
            record.value = safe_repr(event.arg)

        self.records.append(record)


def build_predicate(trigger: str, depth: int, recorder: Recorder):
    """
    From(start, inner) begins recording when `start` matches and renumbers depth
    so the triggering call sits at 0. It re-arms once depth returns to the origin,
    which is why repeated invocations arrive without any extra bookkeeping.
    """
    start = Q(kind="call", function=trigger)
    inner = And(
        Q(stdlib=False),
        Q(depth_lte=depth),
        Or(*[Q(kind=k) for k in KINDS]),
    )
    return hunter.From(start, When(inner, recorder))


@dataclass
class RunResult:
    error: BaseException | None = None
    stdout: str = ""
    stderr: str = ""


def run(script: str, argv: list[str], predicate) -> RunResult:
    """
    Execute the script in this interpreter with the tracer already installed.

    The script's own output is captured rather than interleaved with the report.
    Recording stops when the trigger's frame returns, so the assertion that
    proves the failure usually lands outside the trace — keeping stdout and
    stderr puts the symptom back in front of the reader without dragging the
    whole teardown into the tree.
    """
    saved_argv = sys.argv[:]
    sys.argv = [script, *argv]
    result = RunResult()
    out, err = io.StringIO(), io.StringIO()
    try:
        with hunter.trace(predicate, actions=[]):
            try:
                with redirect_stdout(out), redirect_stderr(err):
                    runpy.run_path(script, run_name="__main__")
            except BaseException as exc:          # the script failing is the point
                result.error = exc
    finally:
        sys.argv = saved_argv
        result.stdout = out.getvalue()
        result.stderr = err.getvalue()
    return result


# ------------------------------------------------------------------- render


@dataclass
class Node:
    function: str
    module: str
    args: dict
    value: str | None = None
    raised: str | None = None
    children: list["Node"] = field(default_factory=list)


def build_tree(records: list[Record]) -> list[tuple[int, Node]]:
    """
    Reconstruct the call tree per invocation. Depth gaps are expected, because a
    frame can be filtered out by the whitelist while its callee is recorded, so
    parents are resolved by depth rather than by assuming a contiguous stack.
    """
    roots: list[tuple[int, Node]] = []
    stack: list[tuple[int, Node]] = []
    current_invocation = None

    for record in records:
        if record.invocation != current_invocation:
            stack.clear()
            current_invocation = record.invocation

        if record.kind == "call":
            while stack and stack[-1][0] >= record.depth:
                stack.pop()
            node = Node(record.function, record.module, record.args or {})
            if stack:
                stack[-1][1].children.append(node)
            else:
                roots.append((record.invocation, node))
            stack.append((record.depth, node))
            continue

        # return or exception: attach to the open frame at this depth, if any
        target = None
        for index in range(len(stack) - 1, -1, -1):
            if stack[index][0] == record.depth:
                target = stack[index][1]
                del stack[index:]
                break
        if target is None:
            continue
        if record.kind == "return":
            target.value = record.value
        else:
            target.raised = record.value

    return roots


def render_node(node: Node, indent: int, lines: list[str]) -> None:
    pad = "  " * indent
    args = ", ".join(f"{k}={v}" for k, v in node.args.items())
    head = f"{pad}{node.function}({args})"

    # Leaf frames are the bulk of a real trace, so fold their outcome onto the
    # call line rather than spending a line on it.
    if not node.children:
        if node.raised is not None:
            lines.append(f"{head}  ! {node.raised}")
        elif node.value is not None:
            lines.append(f"{head} -> {node.value}")
        else:
            lines.append(head)
        return

    lines.append(head)
    render_children(node.children, indent + 1, lines)
    if node.raised is not None:
        lines.append(f"{pad}  ! {node.raised}")
    elif node.value is not None:
        lines.append(f"{pad}  -> {node.value}")


def render_children(children: list[Node], indent: int, lines: list[str]) -> None:
    """
    A loop calling the same helper a thousand times is a thousand identical
    frames. Render the first as a representative and count the rest, so the
    shape of the execution survives without the volume.
    """
    index = 0
    while index < len(children):
        end = index + 1
        while (
            end < len(children)
            and children[end].function == children[index].function
            and children[end].module == children[index].module
        ):
            end += 1

        render_node(children[index], indent, lines)
        repeats = end - index - 1
        if repeats:
            lines.append("  " * indent + f"... {children[index].function} x{repeats} more")
        index = end


def render_tree(roots: list[tuple[int, Node]]) -> str:
    lines: list[str] = []
    multiple = len({inv for inv, _ in roots}) > 1
    for invocation, node in roots:
        if multiple:
            lines.append(f"[invocation {invocation}]")
        render_node(node, 0, lines)
        lines.append("")
    return "\n".join(lines).rstrip()


def suggest_depth(histogram: Counter[int], cap: int) -> int | None:
    """Largest depth whose cumulative event count still fits under the cap."""
    running = 0
    best = None
    for depth in sorted(histogram):
        running += histogram[depth]
        if running > cap:
            break
        best = depth
    return best


def render_header(args, recorder: Recorder, result: RunResult, cap: int) -> str:
    out_of_scope = Counter(
        {m: n for m, n in recorder.census.items() if not recorder.in_scope(m)}
    )
    total_recorded = len(recorder.records)

    lines = [
        "=== header ===",
        f"script          {args.script}",
        f"prefixes        {', '.join(args.prefixes) if args.prefixes else '(none — all non-stdlib)'}",
        f"depth           {args.depth}",
        (
            f"trigger fired   {recorder.invocations} time(s), recorded "
            f"{recorder.recorded_invocations}"
            if recorder.has_trigger
            else "trigger         (none — recording from script start)"
        ),
        f"events recorded {total_recorded}",
    ]

    if recorder.has_trigger and recorder.invocations == 0:
        lines.append("")
        lines.append("WARNING trigger never fired — nothing was recorded.")
        lines.append("        the named function was not entered during this run.")
        lines.append("        try another function, or run without --trigger.")
    elif total_recorded == 0:
        lines.append("")
        lines.append("WARNING trigger fired but no event matched the prefixes.")

    if out_of_scope:
        total_dropped = sum(out_of_scope.values())
        lines.append("")
        lines.append(f"touched but not recorded ({total_dropped} events):")
        for module, count in out_of_scope.most_common(8):
            lines.append(f"  {count:>7}  {module}")
        remaining = len(out_of_scope) - 8
        if remaining > 0:
            lines.append(f"  {'':>7}  (+{remaining} more modules)")
        lines.append("  widen --prefixes if the fault may live in one of these.")

    if recorder.truncated:
        lines.append("")
        lines.append(f"TRUNCATED recording stopped at {recorder.max_events} events.")
        lines.append("          the tree below is incomplete; re-trace shallower.")

    if cap and total_recorded > cap:
        fit = suggest_depth(recorder.depth_histogram, cap)
        lines.append("")
        lines.append(f"OVER CAP {total_recorded} events exceeds --cap-events {cap}.")
        if fit is None:
            lines.append("         even depth 0 does not fit; narrow --prefixes instead.")
        else:
            lines.append(f"         --depth {fit} would fit.")

    if result.error is not None:
        lines.append("")
        lines.append(f"script exited with {type(result.error).__name__}: {result.error}")

    return "\n".join(lines)


# ---------------------------------------------------------------------- cli


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        prog="facet-trace",
        description="Capture one scoped call/return/exception trace of a Python script.",
    )
    parser.add_argument("script", help="path to the script to run and trace")
    parser.add_argument(
        "--prefixes",
        default="",
        help="comma-separated path substrings to record; empty records all non-stdlib",
    )
    parser.add_argument(
        "--trigger",
        default=None,
        help="function name whose entry starts recording; omit to trace from script start",
    )
    parser.add_argument("--depth", type=int, default=10, help="depth below the trigger (default 10)")
    parser.add_argument(
        "--max-invocations",
        type=int,
        default=3,
        help="how many trigger firings to record (default 3); the header reports how many fired",
    )
    parser.add_argument(
        "--cap-events",
        type=int,
        default=4000,
        help="advisory event budget; over it the header suggests a depth that fits",
    )
    parser.add_argument(
        "--include-comprehensions",
        action="store_true",
        help="record <genexpr>/<listcomp>/<dictcomp>/<setcomp> frames (off by default)",
    )
    parser.add_argument(
        "--max-events",
        type=int,
        default=100_000,
        help="hard ceiling on recorded events (default 100000); a safety valve, not a budget",
    )
    parser.add_argument("--out", default=None, help="write the canonical event list here (.jsonl)")
    parser.add_argument("--root", default=None, help="path root for relative module names (default cwd)")
    parser.add_argument("--json", action="store_true", help="emit a JSON report instead of text")
    parser.add_argument("script_args", nargs="*", help="arguments passed through to the script")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.prefixes = [p.strip() for p in args.prefixes.split(",") if p.strip()]
    root = os.path.abspath(args.root or os.getcwd())

    recorder = Recorder(
        args.prefixes, args.max_invocations, root,
        args.include_comprehensions, has_trigger=bool(args.trigger),
        max_events=args.max_events,
    )

    if args.trigger:
        predicate = build_predicate(args.trigger, args.depth, recorder)
    else:
        # no temporal gate: record from script start, depth measured from there
        predicate = When(
            And(Q(stdlib=False), Q(depth_lte=args.depth), Or(*[Q(kind=k) for k in KINDS])),
            recorder,
        )

    result = run(args.script, args.script_args, predicate)

    if args.out:
        with open(args.out, "w") as handle:
            for record in recorder.records:
                handle.write(json.dumps(record.to_json(), sort_keys=True) + "\n")

    roots = build_tree(recorder.records)

    if args.json:
        print(
            json.dumps(
                {
                    "script": args.script,
                    "trigger": args.trigger,
                    "prefixes": args.prefixes,
                    "depth": args.depth,
                    "invocations": recorder.invocations,
                    "recorded_invocations": recorder.recorded_invocations,
                    "events_recorded": len(recorder.records),
                    "recorded_modules": dict(recorder.recorded_modules),
                    "out_of_scope": {
                        m: n for m, n in recorder.census.items() if not recorder.in_scope(m)
                    },
                    "error": None if result.error is None else f"{type(result.error).__name__}: {result.error}",
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "tree": render_tree(roots),
                },
                indent=2,
            )
        )
        return 0

    print(render_header(args, recorder, result, args.cap_events))
    print()
    print("=== tree ===")
    print(render_tree(roots) or "(empty)")

    if result.stdout.strip() or result.stderr.strip():
        print()
        print("=== script output ===")
        if result.stdout.strip():
            print(result.stdout.rstrip())
        if result.stderr.strip():
            print(result.stderr.rstrip())
    if args.out:
        print()
        print(f"events written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
