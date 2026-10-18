# facet-trace

A scoped execution tracer for Python, built to give coding agents runtime
evidence instead of guesses about what a program did.

It runs a script in-process under a call/return/exception tracer, filtered by
three things: a temporal trigger (start recording when a named function is
entered), a spatial whitelist (only record frames whose path matches one of the
given prefixes), and a depth limit measured from the trigger. It prints a
header, a call tree, and the script's own output, and writes a canonical event
list to disk.

No model is involved anywhere in the tool. The tree is built by a formatter, so
it cannot describe a call that did not happen.

## Why

Two 2026 papers established that agents resolve more issues when they can see
runtime behaviour rather than inferring it from source.

[DAIRA](https://arxiv.org/abs/2603.22048) gives the agent a tracing CLI and a
workflow, and reports 74.20% against a 65.40% baseline. Its own ablation is the
more interesting result: feeding raw traces straight to the agent scores 65.80%,
barely above baseline, so the value is in the processing rather than the capture.

[SWE-Doctor](https://arxiv.org/abs/2607.00990) decomposes an issue into its
separate stated behaviours and generates a test for each, after finding that
using a single generated reproduction as a target makes agents worse — 74
resolved dropping to 71, 71 and 73 with three published test generators. The
reason is partial patches: one test covers one manifestation of the issue, the
agent satisfies it and stops.

Both leave the same two gaps, which is what this tool is for.

A wrong scope is silent. DAIRA's whitelist is a single path substring chosen by
the agent from reading the issue. If it misses the file the fault lives in, the
trace comes back clean, complete, well-formatted and useless, and nothing
distinguishes that from the bug not being there.

One reproduction sees one behaviour. DAIRA traces a single reproduction, so a
patch that fixes one of three stated behaviours produces a re-trace that looks
correct.

## Install

```sh
python -m venv .venv
.venv/bin/pip install hunter
```

Python 3.10+. Tested on 3.14.

## Use

```sh
python facet_trace.py <script.py> \
    --trigger <function_name> \
    --prefixes path/one,path/two \
    --depth 10 \
    --out events.jsonl
```

Everything except the script is optional. Without `--trigger` it records from
script start; without `--prefixes` it records every non-stdlib frame.

## Reading the header

The header is the part that does work. Each line is a signal with a response.

```
events recorded 0
WARNING trigger never fired — nothing was recorded.
```

The function you named was never entered. Pick a different one, or drop
`--trigger` to see what actually runs.

```
events recorded 2

touched but not recorded (17 events):
       16  app/core/serial.py
        1  facet1.py
  widen --prefixes if the fault may live in one of these.
```

Execution went somewhere you were not watching. This is the signal DAIRA cannot
produce, because by the time its whitelist has filtered an event, the fact that
it existed is gone. Here the tracer sees everything and reports what it
discarded, so a wrong scope corrects itself instead of returning an empty trace
that looks like an answer.

```
trigger fired   4 time(s), recorded 3
```

The function ran more often than was captured. DAIRA calls `hunter.stop()` on
the trigger's return, so it records the first invocation only and never says so.
This re-arms, bounds the count with `--max-invocations`, and reports both
numbers so a missed invocation is visible rather than silent.

```
OVER CAP 9120 events exceeds --cap-events 4000.
         --depth 4 would fit.
```

The tracer has the full event log in hand, so it can compute which depth fits
rather than just refusing. One re-trace with a number instead of several with
guesses.

## What it does differently

| | DAIRA's `run_hunter_trace` | this |
|---|---|---|
| scope | one path substring | a list of prefixes |
| discarded events | invisible | counted and reported by module |
| repeated invocations | first only, silently | bounded, with both counts reported |
| over budget | "too big" | the depth that would fit |
| call tree | written by an LLM | rendered by a formatter |
| script output | — | captured and shown separately |

The last two matter together. Recording stops when the trigger's frame returns,
so the assertion that proves the failure usually happens outside the trace.
Capturing stdout and stderr puts the symptom back in front of the reader without
dragging teardown into the tree.

## Noise handling

Comprehension and generator frames fire once per iteration and would bury a
trace in frames that carry no diagnostic weight, so they are excluded by default
and `--include-comprehensions` brings them back.

Consecutive sibling calls to the same function are collapsed to one
representative plus a count, so a loop over twenty thousand items renders as two
lines rather than sixty thousand.

Memory addresses are normalised to `0xADDR`. They differ every run, mean
nothing, and leaving them in would make any future comparison of two traces
report differences that are not differences.

Module-level frames carry the entire module namespace as locals, which is
thousands of tokens of imports and dunders, so their arguments are suppressed.

## Status

Built and tested: the tracer, the header signals, the deterministic tree, the
canonical event file.

Designed, not built: the agent skill that drives it, per-behaviour test
generation, the patch-set-against-trace-set intersection check, and the scrubbed
pre/post trace diff. Those live in the design notes, not in this repo.

## Limits

Single process. `sys.settrace`, which Hunter sits on, installs per thread, so
code running in a spawned thread is invisible unless threading support is
enabled, and nothing crosses a process boundary. Async job queues and anything
distributed are out of scope.

Call boundaries only. Arguments and return values are recorded; whatever happens
inside a function body is not. A bug that flips a condition where both branches
only assign locals produces an identical trace.

Overhead is roughly 40x on a tight pure-Python call loop, which is the worst
case. On a test that does real work the ratio is far lower, and next to model
latency it is noise.

## Tests

```sh
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests/ -q
```
