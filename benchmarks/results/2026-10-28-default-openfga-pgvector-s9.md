| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 1.000 | 0.851 | 0.149 | 0.00 | 1.2 | 1.7 |
| post_filter | 0.000 | 0.000 | 0.424 | 0.72 | 71.5 | 112.3 |
| iterative | 0.000 | 0.000 | 0.927 | 0.09 | 514.3 | 1925.5 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 33.5 | 53.8 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 23.9 | 38.8 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.135 (n=387) | 0.193 (n=111) | 0.350 (n=2) | - |
| post_filter | - | 0.407 (n=387) | 0.470 (n=111) | 0.950 (n=2) | - |
| iterative | - | 0.918 (n=387) | 0.959 (n=111) | 0.950 (n=2) | - |
| pre_filter | - | 1.000 (n=387) | 1.000 (n=111) | 1.000 (n=2) | - |
| cached_pre_filter | - | 1.000 (n=387) | 1.000 (n=111) | 1.000 (n=2) | - |
