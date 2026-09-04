# Testing and coverage guidance

Coverage answers which instrumented code executed during particular tests. It
does not prove assertion quality, requirement correctness, absence of defects,
or production reachability.

## Sources

- Google's [Code Coverage Best Practices](https://testing.googleblog.com/2020/08/code-coverage-best-practices.html)
  recommends treating coverage as a useful, lossy signal rather than imposing a
  single universal target.
- Google's [Test Sizes](https://testing.googleblog.com/2010/12/test-sizes.html)
  describes small, medium, and large tests by execution properties and helps
  reason about feedback speed and isolation.
- Martin Fowler's advisory [Test Pyramid](https://martinfowler.com/bliki/TestPyramid.html)
  encourages many fast focused tests and fewer broad end-to-end tests. It is a
  practitioner model, not a requirement for every architecture.

## Measurement rules

- Run the repository's documented suite first. A generic adapter supplements
  rather than silently replaces it.
- Record exact argv, working directory, tool/runtime versions, duration,
  exit/signal, test counts when machine-readable output exists, exclusions,
  skipped projects, and coverage denominator.
- Prefer branch coverage for conditional behavior when supported. Preserve line,
  branch, and function counts separately; do not average incompatible metrics.
- Go's native profile measures covered statements/blocks. Label that basis and
  use `null` for unavailable branch/function metrics.
- A test failure with readable coverage is `tests_failed_partial`. A failed or
  unavailable measurement has a status and `null` percentage, never 0%.
- Include source files with executable statements but zero hits. Explain how
  generated, vendored, migration, declaration, and unreachable files were
  included or excluded.
- Do not compare percentages across different commands or denominators as if
  they were the same experiment.

## Risk-focused gap analysis

Map uncovered behavior back to documented promises. Inspect, in priority order:

1. authentication and authorization denials;
2. public protocol validation and compatibility;
3. transactions, persistence, migrations, and data-loss prevention;
4. concurrency, idempotency, leases, and duplicate delivery;
5. retry, timeout, cancellation, recovery, and rollback;
6. destructive operations and filesystem/network boundaries;
7. malformed, empty, maximum-size, and adversarial inputs;
8. core domain decisions and state transitions;
9. integration seams and operator-critical diagnostics;
10. low-risk utility or presentation paths.

Recommend a test by naming the risk, setup, stimulus, expected invariant, and
appropriate level. Avoid requests to cover trivial getters or defensive lines
without a reachable scenario merely to raise a percentage.
