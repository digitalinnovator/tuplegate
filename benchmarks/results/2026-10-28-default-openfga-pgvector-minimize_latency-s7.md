| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| post_filter | 0.000 | 0.000 | 0.339 | 0.83 | 75.1 | 130.6 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 24.3 | 53.2 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| post_filter | - | 0.323 (n=438) | 0.442 (n=59) | 0.667 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
