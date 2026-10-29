> In-memory reference run: leak rate and recall are meaningful; latency is not.

| strategy | query leak rate | result leak rate | recall@k | short results | p50 ms | p95 ms |
|---|---:|---:|---:|---:|---:|---:|
| unfiltered | 0.990 | 0.722 | 0.278 | 0.00 | 0.3 | 0.4 |
| post_filter | 0.000 | 0.000 | 0.495 | 0.58 | 1.9 | 3.1 |
| iterative | 0.000 | 0.000 | 0.995 | 0.01 | 5.7 | 32.2 |
| pre_filter | 0.000 | 0.000 | 1.000 | 0.00 | 28.0 | 34.9 |

Recall@k by user selectivity (share of docs the user can see):

| strategy | <1% | 1-5% | 5-20% | 20-50% | >=50% |
|---|---:|---:|---:|---:|---:|
| unfiltered | - | 0.244 (n=91) | 0.278 (n=95) | 0.469 (n=13) | 0.900 (n=1) |
| post_filter | - | 0.427 (n=91) | 0.520 (n=95) | 0.738 (n=13) | 1.000 (n=1) |
| iterative | - | 0.990 (n=91) | 1.000 (n=95) | 1.000 (n=13) | 1.000 (n=1) |
| pre_filter | - | 1.000 (n=91) | 1.000 (n=95) | 1.000 (n=13) | 1.000 (n=1) |
