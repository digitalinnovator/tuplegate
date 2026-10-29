| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 0.998 | 0.855 | 0.145 | 0.00 | 1.7 | 3.2 |
| post_filter | 0.000 | 0.000 | 0.421 | 0.77 | 81.0 | 138.9 |
| iterative | 0.000 | 0.000 | 0.929 | 0.08 | 462.6 | 1956.2 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 29.3 | 56.4 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 24.6 | 38.6 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.136 (n=428) | 0.201 (n=71) | 0.000 (n=1) | - |
| post_filter | - | 0.408 (n=428) | 0.501 (n=71) | 0.200 (n=1) | - |
| iterative | - | 0.925 (n=428) | 0.955 (n=71) | 1.000 (n=1) | - |
| pre_filter | - | 1.000 (n=428) | 1.000 (n=71) | 1.000 (n=1) | - |
| cached_pre_filter | - | 1.000 (n=428) | 1.000 (n=71) | 1.000 (n=1) | - |
