| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| post_filter | 0.000 | 0.000 | 0.682 | 0.41 | 17.4 | 26.0 |
| iterative | 0.000 | 0.000 | 0.970 | 0.01 | 31.6 | 233.6 |
| pre_filter | 0.000 | 0.000 | 0.999 | 0.00 | 497.8 | 2299.6 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 58.1 | 151.6 |
| auto | 0.000 | 0.000 | 0.993 | 0.00 | 29.8 | 95.4 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| post_filter | - | - | 0.419 (n=167) | 0.709 (n=167) | 0.920 (n=166) |
| iterative | - | - | 0.932 (n=167) | 0.988 (n=167) | 0.992 (n=166) |
| pre_filter | - | - | 1.000 (n=167) | 1.000 (n=167) | 0.998 (n=166) |
| cached_pre_filter | - | - | 1.000 (n=167) | 1.000 (n=167) | 1.000 (n=166) |
| auto | - | - | 1.000 (n=167) | 0.988 (n=167) | 0.992 (n=166) |
