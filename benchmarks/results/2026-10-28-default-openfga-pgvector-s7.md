| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.883 | 0.117 | 0.00 | 2.3 | 3.9 |
| post_filter | 0.000 | 0.000 | 0.341 | 0.83 | 110.6 | 180.1 |
| iterative | 0.000 | 0.000 | 0.919 | 0.12 | 1225.4 | 3856.5 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 40.0 | 90.3 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 30.0 | 49.4 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.105 (n=438) | 0.202 (n=59) | 0.200 (n=3) | - |
| post_filter | - | 0.325 (n=438) | 0.441 (n=59) | 0.667 (n=3) | - |
| iterative | - | 0.915 (n=438) | 0.949 (n=59) | 0.967 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| cached_pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
