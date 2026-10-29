### Enforcement after a revoke

| engine | consistency | strategy | sidecar lag p50 / p95 ms | enforcement lag p50 / p95 / max ms | trials that leaked |
|---|---|---|---:|---:|---:|
| openfga | full | pre_filter | 7.73 / 11.83 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | full | post_filter | 8.7 / 10.36 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | full | cached_pre_filter | 8.15 / 12.72 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | at_least_as_fresh | pre_filter | 7.9 / 10.44 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | at_least_as_fresh | post_filter | 8.47 / 9.53 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | at_least_as_fresh | cached_pre_filter | 7.45 / 11.2 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | minimize_latency | pre_filter | 7.04 / 8.83 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | minimize_latency | post_filter | 8.09 / 9.86 | 0.0 / 0.0 / 0.0 | 0/20 |
| openfga | minimize_latency | cached_pre_filter | 7.34 / 8.48 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | full | pre_filter | 7.38 / 8.85 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | full | post_filter | 7.33 / 9.7 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | full | cached_pre_filter | 7.46 / 14.06 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | at_least_as_fresh | pre_filter | 8.54 / 12.97 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | at_least_as_fresh | post_filter | 8.39 / 10.61 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | at_least_as_fresh | cached_pre_filter | 9.03 / 14.26 | 0.0 / 0.0 / 0.0 | 0/20 |
| spicedb | minimize_latency | pre_filter | 8.05 / 11.91 | 58.59 / 4903.69 / 4930.9 | 12/20 |
| spicedb | minimize_latency | post_filter | 7.72 / 9.46 | 62.97 / 4815.97 / 4898.56 | 11/20 |
| spicedb | minimize_latency | cached_pre_filter | 7.9 / 12.03 | 155.96 / 4826.34 / 4874.47 | 15/20 |

### Revoking one folder grant

| engine | docs below the folder | model | tuples written | write ms |
|---|---:|---|---:|---:|
| openfga | 10 | structured | 1 | 4.6 |
| openfga | 10 | flattened | 10 | 5.7 |
| openfga | 1,000 | structured | 1 | 4.1 |
| openfga | 1,000 | flattened | 1,000 | 183.3 |
| openfga | 10,000 | structured | 1 | 4.6 |
| openfga | 10,000 | flattened | 10,000 | 4,779.7 |
| spicedb | 10 | structured | 1 | 4.0 |
| spicedb | 10 | flattened | 10 | 4.5 |
| spicedb | 1,000 | structured | 1 | 4.5 |
| spicedb | 1,000 | flattened | 1,000 | 139.8 |
| spicedb | 10,000 | structured | 1 | 6.8 |
| spicedb | 10,000 | flattened | 10,000 | 3,502.7 |
