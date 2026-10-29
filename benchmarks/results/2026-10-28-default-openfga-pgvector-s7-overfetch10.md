| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| post_filter | 0.000 | 0.000 | 0.472 | 0.60 | 203.8 | 368.7 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| post_filter | - | 0.459 (n=438) | 0.561 (n=59) | 0.700 (n=3) | - |
