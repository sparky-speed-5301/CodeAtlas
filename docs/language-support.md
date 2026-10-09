# Work Package 4: broader language support

CodeAtlas now uses one `LanguageAdapter` contract for Python, JavaScript,
TypeScript, Go, Java, and Rust. The existing Python/JS/TS behavior remains on
the same base class; Go, Java, and Rust add grammar-backed static parsing,
capability reporting, protected/generated-file classification, bounded test
discovery, and allowlisted offline command templates.

## Common adapter surface

Every adapter exposes the existing compatibility methods:

- `detect_language(path)`
- `language_version(root)` for a bounded known-tool version probe
- `parse_modified_file(path, source)` / `syntax_check(path, source)`
- `report_tool_availability(root)` for legacy boolean tool probes
- `capability_report(root)` for typed status reporting
- `classify_fixability(finding)`
- `is_protected(path, source)` / `identify_protected_generated_files(...)`
- `discover_targeted_tests(...)`
- `produce_allowlisted_commands(...)`
- `allowlisted_check_commands(...)`
- `validate_patch_constraints(...)`

`LanguageCapabilityReport` uses these statuses:

```text
available | unavailable | unsupported | not_run | passed | failed | blocked
```

Parser availability and compiler availability are separate. A grammar parser
being available does not claim that a compiler, type checker, formatter, linter,
or test runner has executed.

## Language matrix

| Language | Files | Static parser | Formatter/linter/type tools | Test command policy |
| --- | --- | --- | --- | --- |
| Go | `.go` | Tree-sitter Go | `gofmt`, `staticcheck`, `golangci-lint`, `go` | `go test -mod=readonly ...` |
| Java | `.java` | Tree-sitter Java | `javac`, `google-java-format`, `checkstyle` | Maven `-o test` or Gradle `--offline test` |
| Rust | `.rs` | Tree-sitter Rust | `rustfmt`, Cargo check, Clippy | `cargo test --offline` |

Toolchains are capability-dependent. Missing tools produce `unavailable` or
`blocked` statuses; dependencies are never installed automatically. Parser
diagnostics are bounded and never include provider or credential material.

This verification environment has none of the native Go, Java, or Rust
toolchains installed (`go`, `javac`/Maven/Gradle, and `rustc`/Cargo are absent).
The grammar parsers are installed and exercised; native compiler, formatter,
linter, and test execution remain `unavailable` here rather than being claimed
as passed. Use the capability response before requesting those checks.

## API capability reporting

The local API reports all six language adapters:

```bash
codeatlas --json capabilities
curl http://127.0.0.1:8765/api/v1/capabilities
```

The response includes `language_capabilities` entries with parser, syntax,
formatter, linter, type-checker, test-runner, language-version, and limitation
fields. OpenAPI includes the typed response model.

## Repair behavior

Go, Java, and Rust repairs use the same bounded repair context, provider schema,
patch parser, target scope, redaction, policy, syntax, approval, isolated
validation, explicit apply, and safe revert gates as Python and JS/TS. A
grammar parse is static evidence only. Compiler/type-checker/test evidence is
reported only when the corresponding local capability exists and the operation
is explicitly approved.

Protected targets include tests, generated files, dependency manifests,
workflow/configuration files, vendor directories, Go `vendor`, Rust `target`,
and Gradle output. Go `_test.go`, Java `*Test.java`/`*Tests.java`, and Rust test
targets are discovered only within bounded repository-relative paths.

## Benchmark

The deterministic repair-quality benchmark now includes six languages:

```bash
python eval/eval_repair_quality.py
```

The benchmark covers Python, JavaScript, TypeScript, Go, Java, and Rust with
the same parseability, scope, redaction, syntax, and expected-behavior gates.
It does not invoke a compiler, provider, package manager, repository test, or
network. A successful benchmark result demonstrates the adapter and patch
contracts, not semantic correctness of an arbitrary repair.

## Verification

```bash
python -m pytest tests/unit/test_language_adapters.py -q
python -m pytest tests/unit/test_repair.py tests/unit/test_repository_intel.py -q
python eval/eval_repair_quality.py
```
