# Mutation report: modern_app.main

**Mutation score: 88.2%** (killed 82, survived 11, errors 0)

Mutants generated: 93; evaluated: 93. Errors are excluded from the score.

Baseline: the unmutated rebuild replays 3234 / 3234 exchanges equivalent.

Wall time: 467.3 s with 4 jobs, timeout 120 s per mutant. Rebuild: `modern_app/main.py`. Traces: `out/mutate/billing-traces.jsonl`. Spec: `out/mutate/billing-spec.json`.

## By operator

| Operator | Killed | Survived | Errors | Score |
| --- | --- | --- | --- | --- |
| int-literal | 60 | 8 | 0 | 88.2% |
| money-literal | 8 | 2 | 0 | 80.0% |
| comparison | 9 | 1 | 0 | 90.0% |
| boolean | 2 | 0 | 0 | 100.0% |
| guard-order | 3 | 0 | 0 | 100.0% |

## Surviving mutants

Grouped by the function they change.

### `<module>`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 39 | `L39:C22:money-literal+1` | money-literal | `0.01` | `0.02` |
| 39 | `L39:C22:money-literal-1` | money-literal | `0.01` | `0.00` |

### `__init__`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 247 | `L247:C21:int-literal+1` | int-literal | `0` | `1` |
| 247 | `L247:C21:int-literal-1` | int-literal | `0` | `-1` |

### `next_id`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 250 | `L250:C22:int-literal+1` | int-literal | `1` | `2` |
| 250 | `L250:C22:int-literal-1` | int-literal | `1` | `0` |

### `handle_customer_tier`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 354 | `L354:C42:int-literal+1` | int-literal | `404` | `405` |
| 354 | `L354:C42:int-literal-1` | int-literal | `404` | `403` |

### `plain_text_errors`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 423 | `L423:C31:comparison#0:Eq->NotEq` | comparison | `exc.status_code == 404` | `exc.status_code != 404` |
| 423 | `L423:C50:int-literal+1` | int-literal | `404` | `405` |
| 423 | `L423:C50:int-literal-1` | int-literal | `404` | `403` |
