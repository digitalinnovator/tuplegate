| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| post_filter | 0.000 | 0.000 | 0.341 | 0.83 | 16.9 | 23.0 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 86.9 | 230.1 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| post_filter | - | 0.325 (n=438) | 0.442 (n=59) | 0.667 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
