# Software Archaeology Engine: Design

## 1. Problem

Legacy systems are rewritten all the time, and the rewrite is almost always
specified from the wrong source: the documentation, the original authors'
memory, or a reading of the old code. All three describe what the system was
*meant* to do. Users depend on what it *actually* does: the off-by-one
discount boundary, the error that comes back as HTTP 200, the input that is
silently clamped. Those behaviours are where rewrites break.

The Software Archaeology Engine treats the running legacy system as the only
trustworthy specification:

1. **Excavate**: record the system's real inputs and outputs.
2. **Interpret**: infer its true behaviour from those recordings, including
   behaviour the documentation gets wrong.
3. **Rebuild**: hand coding agents a behavioural brief derived from evidence,
   not from docs or source.
4. **Prove**: replay the recorded traffic against the rebuild and show the
   two are equivalent, response for response.

## 2. First principles

* **Observed behaviour is the spec.** Documentation is a set of *claims* to be
  tested against evidence, never an input to the rebuild.
* **Equivalence is demonstrated, not argued.** A rebuild is done when it
  replays the recorded traffic with zero unexplained differences.
* **Some differences are legitimate.** Generated ids and timestamps differ on
  every call. The engine *detects* such volatile fields from the traffic
  rather than relying on a hand-written ignore list.
* **Black box first.** Every stage works from traces alone, so the engine
  applies to systems whose source is lost, unreadable or untrusted.

## 3. Architecture

```
                     +-------------------+
   workload.json --> |  Exerciser /      |   traces.jsonl
   (or live traffic) |  Recorder         | ----------------+
                     |  (WSGI middleware)|                 |
                     +---------+---------+                 |
                               |                           v
                     +---------v---------+      +----------------------+
                     |   Legacy system   |      |   Inference engine   |
                     +-------------------+      |  catalog, volatility,|
                                                |  boundaries, input   |
   claims.json  --------------------------------> normalisation, claims|
   (what the docs say)                          +----------+-----------+
                                                           | spec.json + report.md
                                                           v
                                                +----------------------+
                                                |  Brief generator     | --> brief.md
                                                +----------------------+      |
                                                                              v
                                                                  coding agents build
                                                                  the modern system
                                                                              |
                     +-------------------+      +----------------------+      |
   traces.jsonl ---> |  Replay engine    | <--- |   Modern system      | <----+
                     |  + comparator     |      |   (FastAPI)          |
                     +---------+---------+      +----------------------+
                               |
                               v
                      equivalence report (pass/fail, diffs)
```

### 3.1 Trace format (`archaeology/trace.py`)

The single contract shared by every stage. One JSON object per line:
`id`, `ts`, `request {method, path, query_string, params, headers, body}`,
`response {status, headers, body}`. `params` merges query string and
form-encoded body into a flat `str -> str` map for analysis; raw
`query_string` and `body` are kept for byte-exact replay.

### 3.2 Recorder (`archaeology/recorder/`)

* `RecordingMiddleware(app, sink)`: WSGI middleware that wraps any WSGI app
  and emits a `Trace` per exchange to a sink (list, JSONL file).
* `Exerciser`: drives an app with a declarative workload (explicit requests
  plus parameter sweeps) so a small system can be explored systematically.
  In production the same middleware sits in front of real traffic.

### 3.3 Inference engine (`archaeology/infer/`)

| Module | Question it answers |
| --- | --- |
| `catalog` | Which endpoints exist (with path templates such as `/customer/{id}/tier`), what parameters they take, their types and optionality, which status codes and response shapes they produce. |
| `volatility` | Which response fields change between identical requests or look like generated ids / timestamps, so they must be ignored when comparing. |
| `boundaries` | Where behaviour changes as a numeric input varies: step changes in an output (a discount tier switching on at 101, not 100) and clamping (an echoed input that stops following the input at 1000). |
| `normalization` | Which string inputs the system treats as equivalent: case-insensitive, whitespace-trimmed. |
| `claims` | For each documented claim, is it CONFIRMED, CONTRADICTED or UNOBSERVED by the recorded evidence? |
| `spec` | Assembles all of the above into `spec.json` and a human-readable `report.md`. |

### 3.4 Brief generator (`archaeology/brief.py`)

Turns `spec.json` plus representative traces into `brief.md`: the endpoint
contracts, every inferred hidden behaviour, the documentation errors, and
example exchanges. This brief is the *only* thing the rebuild agents see.

### 3.5 Replay engine (`archaeology/replay/`)

* Adapters turn a target into `Request -> Response`: an in-process WSGI app,
  an in-process ASGI app (FastAPI), or a live base URL.
* The comparator checks status, content type and body; JSON bodies are
  compared structurally with the spec's volatile fields ignored.
* The runner produces an equivalence report: total, matched, and a readable
  diff for every mismatch, with a non-zero exit when below a threshold.

### 3.6 Differential probe (`archaeology/probe.py`)

Replay is only as strong as the recording: a rebuild can match every
recorded exchange and still differ on inputs nobody recorded (see #15, where
the first rebuild truncated money after every discount step and no recorded
request combined a coupon with a bulk quantity). When the legacy system can
still be called, the probe generates fresh, seeded requests from the spec
(observed parameter values, integers drawn across their observed range) and
sends each to both systems. Any divergence fails CI, just like a replay
mismatch.

## 4. The sample legacy system

`legacy_app/` contains "ACME Billing", a deliberately crufty stdlib-only WSGI
service (quotes, invoices, loyalty tiers) with documentation
(`legacy_app/LEGACY_API.md`) that is wrong in several realistic ways. The
planted behaviours are listed in `legacy_app/ANSWER_KEY.md` so the engine's
findings can be scored. The rebuild agents never see that file or the legacy
source.

## 5. Milestone 1 (this milestone)

Goal: the full excavate, interpret, rebuild, prove loop works end to end on
ACME Billing, in CI.

1. Project scaffold, trace format, design doc, CI.
2. Sample legacy app with misleading docs, claims and workload.
3. Recorder middleware and exerciser.
4. Inference engine.
5. Replay engine and equivalence report.
6. CLI, brief generator and end-to-end demo.
7. Modern FastAPI rebuild built only from the brief, passing replay at 100%.
8. README with architecture, how to run and demo output.

## 6. Later milestones

* Recording reverse proxy for live HTTP systems in any language.
* Targeted probing: generate requests to test UNOBSERVED claims and to refine
  boundaries with binary search instead of sweeps.
* Stateful behaviour: infer sequences (create then read) and replay them in
  order with state reset.
* Agent loop: an orchestrator that hands the brief to coding agents, runs
  replay, and feeds diffs back until equivalence.
