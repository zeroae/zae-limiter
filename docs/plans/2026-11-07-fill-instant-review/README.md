# Fill-instant proofs: an independent review (2026-11-07)

A review of [ADR-153](../../adr/153-admission-contract-for-dripping-limits.md),
ADR-154, ADR-155 and `docs/contributing/fill-instant-proofs.md` as proposed on branch
`design/808-fill-instant-fast-path` @ 6c7eb0b8 (PR #811). Refs #808.

Open `report.html` in a browser. It is self-contained and works offline. `artifact.html` is the same page
prepared for the Claude `Artifact` tool, which supplies its own `<html>`/`<head>`/`<body>`. It explains how the
fill-instant fast path works, measures #808 on the real library, runs a fuzz campaign against
the proof's bound, and lists the findings.

## Findings

1. A positive adjustment written as `z ← z + d` over-admits: on a full bucket the debt can
   vanish. It needs the admission's two writes, without the upper bound.
2. The full write `F` admits `c > C`; the client must refuse it before sending.
3. ADR-153's "ceiling in force at the window's start" fails under clock skew at a boundary
   that lowers capacity; "the largest ceiling within `s` before the start" holds.
4. Integer headroom for the scaled fill instant is about 1.3 orders of magnitude (4.1e36
   against 1e38), not about 8.

## Reproduce

```bash
cd docs/plans/2026-11-07-fill-instant-review
python3 campaign.py        # every scenario, ~30 s, writes results.json (not committed)
python3 build_report.py    # rebuilds report.html from results.json + template.html
```

`sim.py` is the model and window checker. It uses the standard library only, and its floats
stand in for the proof's reals. `real_808.json` is the real-library trace from `probe_808.py`,
recorded on `main` @ 1bcc38b1; its docstring says how to re-record it.

The design is not implemented. "Before" numbers come from the real library, and "after"
numbers come from the model.
