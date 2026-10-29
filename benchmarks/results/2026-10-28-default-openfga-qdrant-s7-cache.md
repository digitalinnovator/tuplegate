| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| cached_pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 61.0 | 72.0 |
| auto | 0.000 | 0.000 | 1.000 | 0.00 | 61.0 | 72.5 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| cached_pre_filter | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
| auto | - | 1.000 (n=438) | 1.000 (n=59) | 1.000 (n=3) | - |
