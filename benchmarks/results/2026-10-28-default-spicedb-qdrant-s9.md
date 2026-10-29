| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.850 | 0.150 | 0.00 | 45.8 | 47.4 |
| post_filter | 0.000 | 0.000 | 0.427 | 0.72 | 18.7 | 25.9 |
| iterative | 0.000 | 0.000 | 0.975 | 0.08 | 93.1 | 239.7 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 80.6 | 178.9 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 58.2 | 63.7 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.136 (n=387) | 0.194 (n=111) | 0.350 (n=2) | - |
| post_filter | - | 0.411 (n=387) | 0.475 (n=111) | 1.000 (n=2) | - |
| iterative | - | 0.967 (n=387) | 1.000 (n=111) | 1.000 (n=2) | - |
| pre_filter | - | 1.000 (n=387) | 1.000 (n=111) | 1.000 (n=2) | - |
| cached_pre_filter | - | 1.000 (n=387) | 1.000 (n=111) | 1.000 (n=2) | - |
