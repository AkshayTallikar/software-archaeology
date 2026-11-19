# Mutation report: modern_app.main

**Mutation score: 76.3%** (killed 71, survived 22, errors 0)

Mutants generated: 93; evaluated: 93. Errors are excluded from the score.

Baseline: the unmutated rebuild replays 3226 / 3226 exchanges equivalent.

Wall time: 590.1 s with 4 jobs, timeout 120 s per mutant. Rebuild: `modern_app/main.py`. Traces: `out/mutate/billing-traces.jsonl`. Spec: `out/mutate/billing-spec.json`.

## By operator

| Operator | Killed | Survived | Errors | Score |
| --- | --- | --- | --- | --- |
| int-literal | 52 | 16 | 0 | 76.5% |
| money-literal | 8 | 2 | 0 | 80.0% |
| comparison | 8 | 2 | 0 | 80.0% |
| boolean | 2 | 0 | 0 | 100.0% |
| guard-order | 1 | 2 | 0 | 33.3% |

## Surviving mutants

Grouped by the function they change.

### `<module>`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 39 | `L39:C22:money-literal+1` | money-literal | `0.01` | `0.02` |
| 39 | `L39:C22:money-literal-1` | money-literal | `0.01` | `0.00` |

### `first_value`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 189 | `L189:C18:int-literal-1` | int-literal | `0` | `-1` |

### `__init__`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 244 | `L244:C21:int-literal+1` | int-literal | `0` | `1` |
| 244 | `L244:C21:int-literal-1` | int-literal | `0` | `-1` |

### `next_id`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 247 | `L247:C22:int-literal+1` | int-literal | `1` | `2` |
| 247 | `L247:C22:int-literal-1` | int-literal | `1` | `0` |

### `handle_quote`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 272 | `L272:C4:guard-order` | guard-order | `if sku not in CATALOGUE: / if qty <= 0:` | `if qty <= 0: / if sku not in CATALOGUE:` |

### `handle_invoice`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 307 | `L307:C44:int-literal+1` | int-literal | `400` | `401` |
| 307 | `L307:C44:int-literal-1` | int-literal | `400` | `399` |
| 312 | `L312:C4:guard-order` | guard-order | `if sku not in CATALOGUE: / if qty <= 0:` | `if qty <= 0: / if sku not in CATALOGUE:` |
| 314 | `L314:C14:int-literal-1` | int-literal | `0` | `-1` |
| 314 | `L314:C7:comparison#0:LtE->Lt` | comparison | `qty <= 0` | `qty < 0` |

### `handle_customer_tier`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 351 | `L351:C42:int-literal+1` | int-literal | `404` | `405` |
| 351 | `L351:C42:int-literal-1` | int-literal | `404` | `403` |

### `route`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 375 | `L375:C53:int-literal+1` | int-literal | `405` | `406` |
| 375 | `L375:C53:int-literal-1` | int-literal | `405` | `404` |
| 394 | `L394:C53:int-literal+1` | int-literal | `405` | `406` |
| 394 | `L394:C53:int-literal-1` | int-literal | `405` | `404` |

### `plain_text_errors`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 417 | `L417:C31:comparison#0:Eq->NotEq` | comparison | `exc.status_code == 404` | `exc.status_code != 404` |
| 417 | `L417:C50:int-literal+1` | int-literal | `404` | `405` |
| 417 | `L417:C50:int-literal-1` | int-literal | `404` | `403` |
