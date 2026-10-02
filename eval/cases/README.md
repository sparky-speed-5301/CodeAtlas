# Evaluation cases

Add deterministic vulnerable, fixable, clean, and ambiguous repositories here.
Each case contains `metadata.json`, a `before/` snapshot, and `visible_tests/`.
Metadata must include `case_id`, `language`, `category`, `severity`,
`expected_status`, `expected_behavior`, and boolean `abstention_allowed`.

The runner treats `before/` and visible tests as untrusted data and does not
execute them. Hidden tests and reference patches are future benchmark inputs.
