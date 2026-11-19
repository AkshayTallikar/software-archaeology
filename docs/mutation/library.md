# Mutation report: modern_library.main

**Mutation score: 83.8%** (killed 88, survived 17, errors 0)

Mutants generated: 105; evaluated: 105. Errors are excluded from the score.

Baseline: the unmutated rebuild replays 1821 / 1821 exchanges equivalent.

Wall time: 344.9 s with 4 jobs, timeout 120 s per mutant. Rebuild: `modern_library/main.py`. Traces: `out/mutate/library-traces.jsonl`. Spec: `out/mutate/library-spec.json`.

## By operator

| Operator | Killed | Survived | Errors | Score |
| --- | --- | --- | --- | --- |
| int-literal | 74 | 16 | 0 | 82.2% |
| comparison | 8 | 0 | 0 | 100.0% |
| boolean | 4 | 0 | 0 | 100.0% |
| guard-order | 2 | 1 | 0 | 66.7% |

## Surviving mutants

Grouped by the function they change.

### `<module>`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 54 | `L54:C25:int-literal-1` | int-literal | `500` | `499` |
| 62 | `L62:C15:int-literal-1` | int-literal | `1` | `0` |
| 67 | `L67:C23:int-literal+1` | int-literal | `8` | `9` |
| 67 | `L67:C23:int-literal-1` | int-literal | `8` | `7` |
| 69 | `L69:C21:int-literal+1` | int-literal | `6` | `7` |
| 69 | `L69:C21:int-literal-1` | int-literal | `6` | `5` |
| 229 | `L229:C21:int-literal+1` | int-literal | `0` | `1` |
| 229 | `L229:C21:int-literal-1` | int-literal | `0` | `-1` |
| 230 | `L230:C21:int-literal+1` | int-literal | `0` | `1` |
| 230 | `L230:C21:int-literal-1` | int-literal | `0` | `-1` |

### `utc_timestamp`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 206 | `L206:C70:int-literal+1` | int-literal | `1000` | `1001` |
| 206 | `L206:C70:int-literal-1` | int-literal | `1000` | `999` |

### `new_member_id`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 248 | `L248:C85:int-literal+1` | int-literal | `2` | `3` |
| 248 | `L248:C85:int-literal-1` | int-literal | `2` | `1` |

### `new_loan_id`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 254 | `L254:C81:int-literal+1` | int-literal | `2` | `3` |
| 254 | `L254:C81:int-literal-1` | int-literal | `2` | `1` |

### `borrow`

| Line | Mutant | Operator | Original | Mutated |
| --- | --- | --- | --- | --- |
| 431 | `L431:C8:guard-order` | guard-order | `if lib.fees_due_cents(member.member_id) >= FEE_BORROW_BLOCK_CENTS: / if len(lib.active_loans_for(member.member_id)) >= MAX_ACTIVE_LOANS:` | `if len(lib.active_loans_for(member.member_id)) >= MAX_ACTIVE_LOANS: / if lib.fees_due_cents(member.member_id) >= FEE_BORROW_BLOCK_CENTS:` |
