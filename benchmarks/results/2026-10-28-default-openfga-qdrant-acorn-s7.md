| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 63.6 | 76.6 |
| post_filter | 0.000 | 0.000 | 0.342 | 0.83 | 71.6 | 125.1 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| post_filter | - | 0.327 (n=438) | 0.437 (n=59) | 0.667 (n=3) | - |
