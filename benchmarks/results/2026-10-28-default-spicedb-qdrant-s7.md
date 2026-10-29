| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.883 | 0.117 | 0.00 | 45.7 | 47.2 |
| post_filter | 0.000 | 0.000 | 0.342 | 0.83 | 19.5 | 27.7 |
| iterative | 0.000 | 0.000 | 0.962 | 0.11 | 137.8 | 321.0 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 68.7 | 143.6 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 57.9 | 62.8 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.105 (n=438) | 0.202 (n=59) | 0.200 (n=3) | - |
| post_filter | - | 0.327 (n=438) | 0.437 (n=59) | 0.667 (n=3) | - |
| iterative | - | 0.957 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| cached_pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
