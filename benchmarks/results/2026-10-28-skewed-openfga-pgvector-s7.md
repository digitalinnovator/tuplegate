| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| post_filter | 0.000 | 0.000 | 0.677 | 0.41 | 79.9 | 140.9 |
| iterative | 0.000 | 0.000 | 0.968 | 0.01 | 120.5 | 1629.7 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 245.0 | 915.4 |
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 46.5 | 116.5 |
| auto | 0.000 | 0.000 | 0.991 | 0.00 | 91.8 | 792.4 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| post_filter | - | - | 0.419 (n=167) | 0.694 (n=167) | 0.920 (n=166) |
| iterative | - | - | 0.932 (n=167) | 0.980 (n=167) | 0.993 (n=166) |
| pre_filter | - | - | 1.000 (n=167) | 1.000 (n=167) | 1.000 (n=166) |
| cached_pre_filter | - | - | 1.000 (n=167) | 1.000 (n=167) | 1.000 (n=166) |
| auto | - | - | 1.000 (n=167) | 0.980 (n=167) | 0.993 (n=166) |
