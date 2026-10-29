| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.884 | 0.116 | 0.00 | 1.3 | 1.6 |
| post_filter | 0.000 | 0.000 | 0.338 | 0.83 | 18.7 | 29.6 |
| iterative | 0.000 | 0.000 | 0.918 | 0.12 | 131.7 | 293.4 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 71.3 | 166.1 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 22.1 | 35.4 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.104 (n=438) | 0.202 (n=59) | 0.200 (n=3) | - |
| post_filter | - | 0.322 (n=438) | 0.441 (n=59) | 0.667 (n=3) | - |
| iterative | - | 0.914 (n=438) | 0.949 (n=59) | 0.967 (n=3) | - |
| pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| cached_pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
