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

## 5. Milestone 1 (done)

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

## 6. Milestone 2 (done): stateful systems in any language

Milestone 1 proved the loop on one stateless Python service whose ids were
the only thing that changed between runs. Real legacy systems are neither:
they are written in whatever was fashionable in 2009, a client creates a
record and then uses the identifier the server handed back, and the business
rules are piecewise formulas, not lookup tables. Milestone 2 makes every stage
handle that, and proves it on a second, harder legacy system.

### 6.1 Pinecrest Library: the second legacy system

`legacy_library/` is a lending-library service written in Node.js with no
dependencies. Clients create members, browse a paged catalogue, borrow books
and return them late. Its identifiers are random tokens, its rules depend on
state (how many books a member already has), dates and money, and its
documentation (`legacy_library/LEGACY_API.md`) is wrong in realistic ways.
Its planted behaviours are listed in `legacy_library/ANSWER_KEY.md`; the
engine has to find them from traffic alone, through HTTP, without reading the
JavaScript.

### 6.2 Scenarios: recording multi-step behaviour

A workload gains a third section, `scenarios`. A scenario is an ordered list
of steps; a step can `capture` a value from its response by a dotted JSON
path and later steps use it as `{{name}}` in their path, query or form. A
scenario may `vary` parameters (`values`, `range`, or `date_range` for
ISO dates) and is then run once per combination, each run with its own
captures. Every trace a scenario produces is tagged
`{"name", "run", "step", "vars"}`, so later stages know which exchanges belong
together, in what order, and which varied values the run used: a step's
response can then be analysed against a value set two steps earlier.
Stateless traces carry `scenario: null`.

### 6.3 Excavating any system

* **Process targets**: `cmd:<command>` starts a system as a child process on
  a free port (passed in the `PORT` environment variable), waits until it
  accepts connections, and talks to it over HTTP. Each target is a fresh
  process, so every replay or probe starts from empty state.
* **Recording over HTTP**: the exerciser drives any target (WSGI, ASGI,
  process or URL), not only in-process WSGI apps.
* **Recording reverse proxy**: `archaeology proxy` sits in front of a live
  system and records the real traffic that passes through it.

### 6.4 Inference for stateful systems

* **Opaque identifiers**: path segments that look like generated ids
  (`m_3fa9c2d1`, `L-04b7e2`, UUIDs) are templated like numeric ones, so
  `GET /members/m_3fa9c2d1` and `GET /members/m_77c0e1aa` are one endpoint.
* **Identifier flows**: which response field feeds which later request
  (`POST /members -> member_id -> POST /loans form member_id`), learned from
  the scenario traces. The brief tells the rebuild how clients chain calls.
* **Dates as numbers**: ISO date parameters are swept and analysed as day
  numbers, so a boundary can be "three days after the due date".
* **Piecewise-linear rules**: a numeric output that follows a formula in
  segments (free during a grace period, then a jump, then a fixed amount per
  day, then a cap) is described segment by segment with exact decimal slopes.
* **More normalisation**: inputs whose hyphens are ignored.
* **Claims about steps**: a documented claim can name a scenario step
  ("the sixth loan is refused") instead of a fixed request.

### 6.5 Proving a stateful rebuild

Recorded identifiers cannot be replayed literally: the rebuild hands out its
own. Replay therefore keeps an **identity map**. Whenever a response field
known to be volatile differs between the recording and the rebuild, the pair
is remembered; every later request is rewritten through the map before it is
sent, and every later expected response is rewritten before it is compared.
A rebuild passes when the rewritten exchanges match exactly. The differential
probe gains a scenario mode: it samples fresh scenario runs (any day in a date
range, any integer in a range) and runs each one against fresh copies of
both systems, mapping identifiers the same way.

### 6.6 Evidence report

`archaeology report` renders one self-contained HTML page per system:
documentation versus reality, every hidden behaviour with a chart of the
evidence, identifier flows, and the replay and probe verdicts. CI publishes
the reports as a build artifact.

### 6.7 Issues

1. Milestone 2 contract: scenario traces, scenario workloads, process targets.
2. Recording reverse proxy and `record --target`.
3. Pinecrest Library: the second legacy system, in Node.js.
4. Inference for stateful systems (6.4).
5. Stateful replay and scenario probing with an identity map (6.5).
6. Brief and HTML evidence report for multi-step systems.
7. Two-system demo and CI.
8. Rebuild Pinecrest Library in FastAPI from the brief alone.
9. README and results for milestone 2.

### 6.8 Result

Pinecrest Library was rebuilt in FastAPI by a coding agent from the brief,
spec and traces only. It replays 1,115 / 1,115 recorded exchanges, with 821
requests remapped to the rebuild's own ids. The probe found no divergence in
3,000 fresh requests and 300 fresh scenario runs against the Node.js service.
Inference contradicts 10 of the 13 documented claims and recovers the late-fee
rule exactly. A mutation test shows that replay fails when any of six
business rules is changed by one step. CI runs both systems end to end and
publishes the evidence reports.

## 7. Later milestones

* Targeted probing: generate requests to test UNOBSERVED claims and to refine
  boundaries with binary search instead of sweeps.
* Agent loop: an orchestrator that hands the brief to coding agents, runs
  replay, and feeds diffs back until equivalence.
* Concurrency: record interleaved clients and check the rebuild under the
  same interleavings.
