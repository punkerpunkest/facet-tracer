---
name: facet-trace
description: Resolve a bug report by watching what the code actually does instead of inferring it from source. Use when fixing an issue, reproducing a reported bug, or when you have read the same files twice and still are not sure why a value is wrong. Captures one execution per behaviour the issue states, then checks before submitting that the patch reached every one of them.
---

# facet-trace

Reading source tells you what code is supposed to do. Running it under a tracer
tells you what it did. This skill is the second thing, plus the bookkeeping that
stops you fixing one behaviour and believing you fixed the issue.

## The loop

1. Split the issue into the separate behaviours it states.
2. Write one reproduction per behaviour.
3. Trace each one and read the header before reading the tree.
4. Diagnose from the traces.
5. Patch.
6. Run `cover`, then re-run the tests. Go back to 5 until done.
7. Run the repo's own test suite. Submit.

## 1. Split the issue

A requirement is one observable behaviour the report says is wrong. An expected
output, an exception that should or should not be raised, a compatibility
guarantee. Write them down as a list.

Only extract what the report actually says. Do not invent requirements that seem
reasonable — a probe for a behaviour nobody reported wastes a trace and, worse,
produces a confident diagnosis of the wrong thing.

One behaviour is a normal answer. Several is common. If the report says the text
renders wrong on two different pages, that is two.

## 2. Write one reproduction per behaviour

Each one needs setup, a call into the code, and an assertion of what should have
been true.

Where the values come from, in order of how much you can trust them:

- The issue report. Most include a snippet or a concrete example.
- The repo's existing tests. Find one exercising nearby code and copy how it
  constructs things. This is usually the fastest route and the most reliable.
- Signatures and type hints, which give shape but not values.
- Guessing. Last resort, and the main source of reproductions that fail for the
  wrong reason.

In any framework-heavy repo, write a test file in the repo's own test layout and
run it through the repo's runner rather than writing a standalone script. You
inherit the settings, fixtures and bootstrap for free, and that bootstrap is what
kills most hand-written reproductions.

Do not try to make the test pass. Its job is to drive execution through the
suspect code so there is something to watch. A reproduction that asserts the
wrong thing but reaches the right code is still useful.

## 3. Trace each one

```sh
python facet_trace.py trace --script repro_1.py \
    --trigger <function the test calls into> \
    --prefixes <dirs the behaviour probably lives in> \
    --label "<the behaviour this covers>" \
    --out facet1.jsonl
```

Through the repo's test runner instead:

```sh
python facet_trace.py trace --module pytest \
    --trigger <function> --prefixes <dirs> \
    --label "<behaviour>" --out facet1.jsonl \
    -- tests/test_thing.py -x
```

Set `--prefixes` to the union of the directories any of the behaviours might
live in, not just this one's. A wider net costs nothing when the extra code does
not run, and it means a wrong guess about one behaviour does not blind you.

Pick `--trigger` as the function the test calls directly. It is the root of the
subtree you record, so triggering deep inside the implementation gives you a
narrow view of everything below it and nothing above.

## 4. Read the header before the tree

The header tells you whether the trace is worth reading. Each signal has one
response.

| signal | what happened | do this |
|---|---|---|
| `trigger never fired` | the function was never entered | pick another function, or drop `--trigger` to see what runs |
| `events recorded 0` with modules listed under `touched but not recorded` | execution went somewhere you were not watching | widen `--prefixes` to include them, re-trace |
| `events recorded 0` and nothing listed | the test died before reaching product code | fix the test's setup, usually an import or a missing fixture |
| `trigger fired 4 time(s), recorded 3` | the function ran more often than was captured | raise `--max-invocations` if the later calls matter |
| `OVER CAP ... --depth 4 would fit` | too much to read | re-trace at the suggested depth, or narrow `--prefixes` |
| `TRUNCATED` | the hard ceiling tripped, the tree is incomplete | re-trace much shallower |

A trace with few events and a long `touched but not recorded` list is the common
failure, and it is not an empty result — it is a pointer. The modules listed are
where execution actually went. Widen into them.

## 5. Diagnose

Read the tree with the arguments and return values, not just the call shape.
Work backwards from the wrong value to the call that produced it. The script's
own output is shown under `=== script output ===`, because recording stops when
the trigger's frame returns and the assertion usually fires after that.

If you cannot construct an object the reproduction needs, trace an existing
passing test that uses it and read the arguments off the tree.

## 6. Check coverage before you believe you are finished

```sh
python facet_trace.py cover facet1.jsonl facet2.jsonl
```

This intersects the files your patch touched with the files each behaviour's
execution ran through.

`NOT ADDRESSED` is a proof, not a suggestion. The patch did not alter one line
that behaviour executes, so it cannot have changed it. Go and fix that behaviour
before doing anything else.

`INCONCLUSIVE` means the patch landed in a file that behaviour uses, possibly in
a function it never calls. Re-run that behaviour's test to find out.

Then re-run every reproduction, not only the one you just worked on, because a
second edit can break what the first one fixed.

A reproduction that still fails is not automatically unfinished work. If its
trace shows the behaviour is now correct and the test is asserting the wrong
thing, say so explicitly and move on. Do not contort the code to satisfy a test
that was wrong to begin with.

## 7. Before submitting

Run the repo's existing test suite. The coverage check tells you whether you
fixed everything that was broken; only the existing suite tells you whether you
broke something that worked.

## Notes

Single process only. Code running in a spawned thread is invisible, and nothing
crosses a process boundary.

Call boundaries only. Arguments and return values are recorded; what happens
inside a function body is not. A fix that flips a condition where both branches
only assign locals produces an identical trace.

If you have subagents available, running step 3 in one per behaviour keeps the
widen-and-retrace iteration out of your own context. Only the finished report
comes back.
