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

## 7. Milestone 3

### 7.1 Explore (`archaeology/explore.py`)

`archaeology explore` records a workload with fewer requests than `record`. An
explicit request, or a sweep or scenario whose `vary` holds only value lists, is
sent exhaustively. A sweep or scenario that varies an integer `range` or a
`date_range` is explored along that axis, once in each context of its other
keys. The axis is sampled at nine evenly spaced points. A coarse interval is
clean when its far endpoint continues the line of the sample before it and its
midpoint lies on the chord. Every other interval is halved: a probe that fits
the left line lies before the change, one that fits the right line lies after
it, and otherwise the interval is bisected. Each change is then a pair of
adjacent grid values, the same pair the full sweep compares, so
`detect_boundaries` gives the same findings on either recording. Fields that
differ between identical requests (ids, timestamps) are found by repeating the
first probe and are then ignored. The tests in `tests/test_explore.py` check
the findings of both bundled systems against their full recordings. On billing,
`explore` sends 484 requests against 3,226 for the full recording (6.7x fewer).
On the Node.js library it sent 412 against 1,115 (2.7x fewer) before §7.3
widened that workload. The new questions are value lists, which both recorders
send in full, so the library is now measured on its explored axes: 309 requests
against 1,012 for their full sweeps (3.3x fewer), out of 861 against 1,564
overall. The acceptance criteria are at most a fifth of the full recording for
billing and at most two fifths of the explored axes' sweeps for the library. The method can miss two changes that cancel at every
probe point, which a grid of nine points cannot rule out in general.

### 7.2 Confidence and thin evidence

A finding's sample count says how much the recording supports it. Every
boundary finding carries `evidence`: the number of samples in its sweep, and for
each change point the last sampled input and value before it and the first after
it. A change is `exact` when those two inputs are adjacent. Otherwise the reports
tag it `±N`, where N is the number of inputs never sent that could hide the exact
edge. Findings merged across contexts keep the evidence of their smallest sweep.

Each claim verdict carries `support`: how many traces with the claim's request
agree with it (`matching`) and how many contradict it (`contradicting`). The
verdict still rests on the first matching trace.

The spec's `evidence_gaps` lists what the recording never covered. Each gap has
a `kind`, a `where`, a `detail` and a `suggestion`: one line of workload to add or
fix.

* `inexact_change`: a change point is bracketed by samples that are not adjacent.
  A bracket shared by several contexts is reported once.
* `few_values`: a request parameter or scenario variable has exactly two distinct
  values. A variable with one value is a fixed base input that no sweep meant to
  vary, so it is not reported. Three or more values count as varied.
* `single_status`: an endpoint with at least two requests only ever returned one
  HTTP status. A single request cannot show a change, so it is not reported.
* `failed_capture`: a scenario step sent a request with an unbound
  `{{placeholder}}`. Either a varied value was null and the literal placeholder
  went out, or an earlier step never captured the value. These requests are left
  out of `single_status`.
* `unobserved_claim`: a documented claim that no recorded exchange tests.

The report, the rebuild brief and the HTML page all show these. The brief lists
them under a "be careful here" heading, so a coding agent knows which edges are
guesses. Specs written before this change lack the new keys, and the renderers
say so instead of reporting none.

On the bundled systems the gaps include a workload defect rather than a gap in
behaviour: the Pinecrest `join` scenario's null `tier` variant sends the literal
`{{tier}}` instead of omitting the field.

### 7.3 The open questions, answered by evidence

The first Pinecrest rebuild had to guess four things its recording could not
decide (§6.8). Milestone 3 put each of them to the legacy service as a black
box, with 11 new scenarios and one sweep (449 more exchanges, 1,564 in all),
written without reading the JavaScript or the answer key:

* Borrowing is blocked by total outstanding fees reaching 5.00. Two late
  returns with fees of 3.00 and 2.00 block it, though neither loan was 20 days
  late, and a loan still out past its due date never blocks.
* Fees on several late loans add up, and the 10.00 cap applies per loan.
* `POST /loans` checks the member, the date, the book, the fees, the loan limit
  and availability, in that order. Fees and the loan limit cannot be ordered
  from outside: fees only come from returns, and a return drops the member below
  the limit.
* Invalid dates give `400 INVALID_DATE`, unknown or malformed ISBNs give
  `404 BOOK_NOT_FOUND`, and a return dated before its borrow gives
  `400 RETURN_BEFORE_BORROW`, a rule nobody had asked about.

Replay of the widened recording found three differences in the rebuild: the
loan limit came after availability, an active loan showed empty return fields,
and an early return was accepted. A new agent fixed them in an isolated
workspace holding only the new recording, spec, brief and replay report, and
cited the trace that justifies each change. Replay is 1,564 / 1,564 and the
probe agrees on 4,997 / 4,997 fresh requests and scenario steps.

Asking the questions also fixed a recorder bug: a scenario's `null` variant
sent the literal `{{tier}}` instead of omitting the field. A form or query value
that is exactly the placeholder of a variable the run omitted is now left out.

### 7.4 Result

| | ACME Billing | Pinecrest Library |
| --- | --- | --- |
| Full recording | 3,226 | 1,564 (was 1,115) |
| `explore`, same findings | 484 (6.7x fewer) | 309 against 1,012 on explored axes (3.3x fewer) |
| Thin-evidence items | 9 | 8 |
| Replay | 3,226 / 3,226 | 1,564 / 1,564 |
| Probe | 3,000 / 3,000 | 4,997 / 4,997 |

## 8. Milestone 4

### 8.1 Mutation score

The replay proves that a rebuild matches the recording. It does not show that
the recording pins every rule the rebuild relies on. `archaeology mutate`
measures that gap. It generates mutants, each a rebuild with one change, and
replays the whole recording against each one.

Operators (`archaeology/mutate.py`): integer literals move by +1 and -1;
decimal money literals (`0.25`, `"10.00"`) move by one unit in their last
decimal place; `<` and `<=`, `>` and `>=`, `==` and `!=` swap; `and` and `or`
swap; two consecutive `if <cond>: return ...` guards in one function swap
places. Docstrings, bare expression statements, annotations and booleans
generate no mutants. Status codes are integer literals and are mutated.

Each mutant is written to a fresh package and replayed in a new process, with
the same replay engine and spec that prove the rebuild. Process isolation
keeps module-level state from leaking between mutants. A mutant is **killed**
when replay is not 100% equivalent, **survived** when every recorded exchange
still matches, and an **error** when it fails to import or start, or times
out. Errors are reported apart. The mutation score is
killed / (killed + survived). The unmutated rebuild must replay 100% before a
score is computed.

```
python -m archaeology mutate --rebuild modern_app/main.py \
    --traces out/traces.jsonl --spec out/spec.json \
    --out docs/mutation/billing.md --json docs/mutation/billing.json --jobs 4
```

Measured on the two rebuilds, with the recordings and specs made by
`archaeology record` and `archaeology infer` (four jobs, four cores):

| | ACME Billing | Pinecrest Library |
| --- | --- | --- |
| Mutants generated | 93 | 105 |
| Killed | 71 | 82 |
| Survived | 22 | 23 |
| Errors | 0 | 0 |
| **Mutation score** | **76.3%** | **78.1%** |
| Wall time | 590 s | 257 s |
| Reports | `docs/mutation/billing.md`, `.json` | `docs/mutation/library.md`, `.json` |

The survivors were classified by reading the code and the recording. Of the
45, 12 are equivalent by construction: the mutated value never changes
behaviour (the billing `CENT` quantum uses only the exponent of its `Decimal`;
the library's fee threshold of 500 cents and its id-length divisors at
8 to 9 and 6 to 7 are equivalent on every reachable value; a fee-state default is overwritten before
it is read; a dead branch in `handle_customer_tier`). Another 12 survive
because the replay masks the field: the invoice and loan identifiers, the
member identifier, and the timestamps are volatile, so their length, format and
sequence are never compared. The remaining 21 point to rules the recording does
not pin, for example repeated query or form keys resolving to the first value,
an invalid quote order where sku and quantity both fail, a quantity of zero on
an invoice, a missing-sku invoice, a wrong method on `/quote`, a framework 405
for `TRACE`, a `GET` on an unknown loan, and the check order of fees against the
loan limit.

The score measures how well the recording pins the rebuild, not whether the
rebuild is correct. Equivalent survivors lower it without a fault, and masked
survivors show where comparison rules are missing. Closing the rest needs new
recorded requests, for example `POST /invoice` without a sku, `GET` on an
unknown loan, or a fee total of 24.75 that exposes the money formatting, not
more mutants.

### 8.2 Refine (`archaeology/refine.py`)

`archaeology refine` closes the evidence gaps of a recording instead of only
listing them. Each gap carries a machine-readable `data` field (endpoint, method,
path, parameter or scenario variable, bracketing inputs, observed values and
statuses, and for an inexact change every context that shares the bracket), so the
generator reads facts rather than prose. Each actionable gap becomes a workload
addition:

* `inexact_change`: a sweep of every unsampled input strictly between the two
  bracketing samples, once per context that shares the bracket. For a scenario
  variable, one copy of the template per context, named with a suffix
  (`late-return~refine1`), whose `vary` holds the unsampled values.
* `single_status`: requests that try an unknown path id in the format the catalog
  inferred (same prefix, length and charset as the observed ids), each invalid value
  already seen for that parameter name elsewhere in the traces, and the request
  with each required parameter left out.
* `few_values`: one request or run with a third value: a value a claim names for
  that parameter, else an obviously invalid value.

`failed_capture` (a placeholder left in the workload) and `unobserved_claim` are
logged with the reason and not acted on. Refine never modifies the workload.

Each round records the additions against the target, appends the traces, infers
the spec again and recomputes the gaps. A gap that no longer appears is *closed*.
A gap that remains after its additions ran is *explained*, and the log quotes the
statuses its additions drew. Scenario variants count with their template
(`scenario_family`), so the values a variant adds reach the template's sweeps.

Refine assumes a fresh target and a full recording (`record`, not `explore`). It
replays the workload silently to reach the state the recording was made in, then
records only the additions, so the appended traces continue the recording in order.

Results. Both refinements took one round. The refined workloads are
`legacy_app/workload.refined.json` and `legacy_library/workload.refined.json`, and
the logs are `docs/refine/billing.md` and `docs/refine/library.md`.

| | ACME Billing | Pinecrest Library |
| --- | --- | --- |
| Gaps before | 9 (7 inexact, 1 few values, 1 single status) | 8 (2 few values, 2 single status, 4 failed capture) |
| Closed | 8 (all 7 inexact changes, the coupon values) | 3 (both tier few values, `GET /loans/{id}`) |
| Explained | 1 (`GET /customer/{id}/tier`) | 1 (`GET /books`) |
| Not acted on | 1 (`GET /customer/abc/tier`, which appeared when the invalid-id request added a second 404; it has no id to vary) | 4 (failed captures) |
| Requests added | 78 | 16 |
| Replay of the rebuild | 3,304 / 3,304 | 1,580 / 1,580 |
| Probe of the rebuild | 3,000 / 3,000 | 5,064 / 5,064 |

The explained billing gap is a real single status. Every numeric customer id
returns 200, including unseen ones such as 8942 and 1315. The only 404 comes from
`abc`, which the catalog treats as a literal path, not an id. The explained library
gap is `GET /books`: removing `per_page` still returns 200, and no invalid page
value was ever sent.

The refined library recording also shows the legacy accepting an unknown
membership tier: `POST /members` with `tier=gold` and with `tier=refine-invalid`
both return 201 and store `standard`. That contradicts claim L2, and the rebuild
reproduces it. Neither system diverged anywhere the refinement looked.

### 8.3 Interleavings (`archaeology/interleave.py`)

A scenario is one client at a time, so behaviour that only appears between
clients (two members after one copy, a return racing a borrow) was never recorded
or proven. A workload's `interleave` section names two or more lanes, each a
scenario template or an inline list of steps, and a set of global orders (explicit,
or sampled from a seed). An order names every step of every lane once and keeps
each lane's own step order, and the code checks that. Each lane keeps its own
variables and captures; `vars` gives a lane's domains, as `vary` does for a template.
The schema is documented in the module docstring.

* **Recording**: each order runs against the target in turn. Every exchange carries
  its lane's usual `scenario` tag plus `interleave: {name, id, lane, position}`, so
  the global order is recovered from the trace file. Lane run numbers continue after
  the scenarios' and stay unique. Replay sends traces in file order, so an
  interleaving replays as recorded, and the identity map is shared by all lanes.
* **Probing**: `probe_interleavings` runs fresh interleavings (a random entry, a
  uniformly random valid order, values drawn from each lane's domains) in lockstep
  against both systems. Each divergence names the order it happened in.
* **Brief**: the section "Interactions between clients" lists each recorded
  interleaving, and for every step whose response differs between orders of the
  same lanes, the two orders and the two responses (status and error code).
* **CI**: `archaeology demo --system library` records, replays and probes the
  interleavings (`--interleave-count`, 60 by default). No CI change was needed.

The Pinecrest workload has four interleavings (25 recorded orders, 240 of its 1,804
exchanges). Each lane ends by returning what it borrowed.

| Interleaving | Lanes | Orders | What it contends for |
| --- | --- | --- | --- |
| `same-copy` | two members borrow one copy | 5 | the copy, in both orders and while the other holds it |
| `borrow-return-borrow` | a borrow and return by one member, a borrow, return and borrow by another | 6 | the second member's second borrow after the first returns |
| `fee-blocked-and-free` | a member who returns late (fee, or not) and a free member, after one copy | 10 | a fee-blocked borrow of a free copy, against a free member's borrow |
| `limit-and-another-member` | a member who borrows five copies and tries a sixth, and another member | 4 (sampled, seed 11) | the loan limit against a copy another member holds |

Results, for the rebuild in `modern_library.main:make_app`:

| | Pinecrest Library |
| --- | --- |
| Replay of the widened recording | 1,804 / 1,804 |
| Probe of 60 fresh interleavings (650 steps) | 650 / 650 agree (5,647 / 5,647 with the other probes) |
| Availability checked per member (mutant) | killed: replay fails on the full recording and on the interleavings alone |

What the legacy system does between clients, from the recorded orders:

* A copy on loan refuses another member with 409 `BOOK_UNAVAILABLE` until the
  holder returns it. Once it is returned, the next member's borrow succeeds, in
  either order.
* A late return fee blocks only the member who owes it. A return two days late is
  free; one 32 days late leaves a fee, so that member's next borrow is refused with
  403 `FEES_OUTSTANDING`, even for a copy nobody holds. A free member can borrow the
  same copy in the same run.
* The loan limit is checked before availability. At five loans, a borrow of a copy
  another member holds answers 409 `LOAN_LIMIT`, not `BOOK_UNAVAILABLE`. The limit is
  five in every recording, though `LEGACY_API.md` and the claims file say three.

Limits. The engine reports the refused borrow's later return as a `failed_capture`
gap, because the return names a loan the refused borrow never issued. That is the
intended outcome of a refused borrow, and the refine step does not act on it. The
probe also now passes each run's omitted (null) variables to the step renderer, as the
replay does. Before this, a null in `vary` was sent as a literal placeholder on both
sides, so it compared equal without testing the omission.

`explore` sends every interleaving as recorded, in the exhaustive part, since an order
is not an axis it can bisect, and `full_request_count` counts their steps. `refine`
leaves gaps found on an interleaving's lanes alone: it varies scenario templates, and
a lane is not one, so its log says so instead.

## 9. Later milestones

* Targeted probing: generate requests to test UNOBSERVED claims. Boundary
  refinement by binary search now lives in `explore`.
* Agent loop: an orchestrator that hands the brief to coding agents, runs
  replay, and feeds diffs back until equivalence.
* Concurrency: requests in flight at the same time. Section 8.3 covers clients
  whose steps run one after another in a chosen global order.
