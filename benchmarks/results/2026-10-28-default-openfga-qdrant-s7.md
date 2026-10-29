| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.883 | 0.117 | 0.00 | 45.0 | 47.1 |
| post_filter | 0.000 | 0.000 | 0.342 | 0.83 | 96.7 | 184.7 |
| iterative | 0.000 | 0.000 | 0.962 | 0.11 | 1028.4 | 2500.2 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 66.0 | 79.9 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 63.1 | 77.1 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.105 (n=438) | 0.202 (n=59) | 0.200 (n=3) | - |
| post_filter | - | 0.327 (n=438) | 0.437 (n=59) | 0.667 (n=3) | - |
| iterative | - | 0.957 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| cached_pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
