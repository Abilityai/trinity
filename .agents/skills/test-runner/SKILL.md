---
name: test-runner
description: Run the full Trinity API test suite, analyze results, and generate a comprehensive testing report. Use before merging a PR, after implementing a new feature, or when debugging test failures.
allowed-tools: [Agent, Bash, Read, Write, Grep, Glob]
user-invocable: true
argument-hint: "[<filter>] [--verbose] [--failing]"
automation: manual
---

# Test Runner

Run the full Trinity API test suite, analyze results, and generate a comprehensive testing report.

## When to Use

- Before merging a PR to validate API changes
- After implementing a new feature to verify nothing broke
- When debugging test failures
- For periodic health checks of the test suite

## Usage

```
/test-runner              # Run full test suite
/test-runner auth         # Run tests matching "auth"
/test-runner --verbose    # Include detailed output
```

## Arguments

| Arg | Description |
|-----|-------------|
| `<filter>` | Optional: only run tests matching this pattern |
| `--verbose` | Show detailed test output, not just summary |
| `--failing` | Only re-run previously failing tests |

## Process

The skill spawns a specialized test-runner agent that:

1. **Discovers tests** - Finds all pytest test files in `tests/`
2. **Runs the suite** - Executes tests with appropriate flags
3. **Analyzes results** - Identifies failures, patterns, flaky tests
4. **Generates report** - Summary with pass/fail counts, duration, recommendations

## Output

Returns a structured report:

```
## Test Results

**Status**: PASSED / FAILED
**Duration**: Xs
**Tests**: X passed, Y failed, Z skipped

### Failures (if any)
- test_name: reason

### Recommendations
- ...
```

## Implementation

```instruction
Spawn the test-runner agent to execute the Trinity API test suite.

Agent prompt should include:
- Run pytest on the tests/ directory
- Apply any filter pattern from args: {{args}}
- Capture and analyze all output
- Generate a clear summary report
- If failures occur, include the relevant error messages and file:line references
- Keep the report concise but actionable
```
