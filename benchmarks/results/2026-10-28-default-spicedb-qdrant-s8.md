| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 0.998 | 0.855 | 0.145 | 0.00 | 46.1 | 47.3 |
| post_filter | 0.000 | 0.000 | 0.420 | 0.77 | 18.2 | 24.6 |
| iterative | 0.000 | 0.000 | 0.970 | 0.07 | 65.3 | 226.3 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 72.7 | 136.2 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 57.9 | 63.3 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.136 (n=428) | 0.203 (n=71) | 0.000 (n=1) | - |
| post_filter | - | 0.406 (n=428) | 0.506 (n=71) | 0.200 (n=1) | - |
| iterative | - | 0.965 (n=428) | 1.000 (n=71) | 1.000 (n=1) | - |
| pre_filter | - | 1.000 (n=428) | 1.000 (n=71) | 1.000 (n=1) | - |
| cached_pre_filter | - | 1.000 (n=428) | 1.000 (n=71) | 1.000 (n=1) | - |
