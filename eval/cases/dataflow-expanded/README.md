# Historical Dataflow Suite (`dataflow-expanded`)

**Status:** Historical / Non-Aligned Benchmark Suite (Preserved)

This directory contains the original Phase 3C exploratory fixtures.
These fixtures use synthetic rules (e.g. `sql-injection`) and generic placeholder signatures (e.g. `sink(value)`) that are not targeted at the `SensitiveDataExposureAnalyzer` (`sensitive-to-sink` rule).

When executed against the real `SensitiveDataExposureAnalyzer`, this suite reports a 0.0 recall score because the analyzer targets sensitive credential flow patterns rather than generic SQL injection stubs.

This suite is preserved intact for historical continuity and backward compatibility testing with static baselines. For official Phase 3C real-analyzer evaluation, use the aligned benchmark suite in `eval/cases/sensitive-flow-aligned`.
