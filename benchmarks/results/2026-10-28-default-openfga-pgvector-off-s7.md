| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| pre_filter | 0.000 | 0.000 | 0.342 | 0.83 | 21.5 | 44.3 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| pre_filter | - | 0.326 (n=438) | 0.437 (n=59) | 0.667 (n=3) | - |
