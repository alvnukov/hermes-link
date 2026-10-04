# Quality policy

The gates are intentionally strict and reproducible: pinned Ruff with `ALL`,
Ruff formatting, strict mypy with unreachable-code checking, all-level Bandit,
hashed dependency auditing, and line/branch coverage with a 95% combined floor.
Only stable Ruff rules are enabled. An upgrade is a reviewed lockfile change;
preview rules do not silently change acceptance criteria.

PEPs specify many alternative protocols and facilities, not one universal
conformance checklist. This project follows standard package metadata, MIT SPDX
licensing, typed-package metadata, annotations and documented Python formatting.
The native directory-plugin layout is deliberately retained.

Exceptions are explicit and narrowly scoped:

- The Ruff formatter's officially incompatible whitespace/quote/comma rules
  are disabled. Google docstrings resolve mutually exclusive docstring styles.
- `unittest` assertions and context managers remain valid tests; pytest is the
  runner. Tests omit repetitive docstrings, allow fixture secrets/fixed SQL,
  assert statements and fixture constants, and may inspect internal state to
  inject storage failures. Other test lint rules remain enabled.
- CLI entry points may print. The native plugin's hyphenated directory name is
  fixed by Hermes. The tiny directory loader is tested by actual registration;
  strict mypy covers the typed package, scripts and tests with explicit roots.
- Inline suppressions document required native private interfaces, fixed SQL
  fragments with bound values, lazy imports needed for safe startup diagnostics,
  and public secret-preservation markers. Bandit false positives are suppressed
  only at these specific lines, never by ignoring a whole security category.

No production files or executable paths are omitted. Reporting includes the native
directory loader, every bridge module and every script; test code is outside the
coverage denominator. Coverage's standard handling of
type-only code and abstract protocol bodies remains in effect. Branch coverage
reports cancellation, malformed input, corrupt storage, interrupted publishing,
resource cleanup and native response validation, rather than only success paths.
Mutation checks confirm that lifecycle tests fail when cancellation guards,
usage filtering, safe error codes, agent cleanup or idempotency protection break.

Reference contracts: [Ruff linter](https://docs.astral.sh/ruff/linter/),
[formatter compatibility](https://docs.astral.sh/ruff/formatter/#conflicting-lint-rules),
[Python package metadata](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/).

The baseline suite had 72 tests: 77.65% statement coverage, 66.52% branch coverage,
and 75.23% combined coverage. Machine-readable baseline and final reports are
kept in local `docs/coverage-baseline.json` and `docs/coverage-final.json`. They
are evidence for this refactor, not substitutes for rerunning the gates.

Final refactor: 155 tests passed; statement coverage 96.21%, branch coverage
96.02%, combined coverage 96.17%. Core validation, authentication and executions
each have 100% line/branch coverage. The complete suite also passed on Python
3.14 and against a freshly fetched native Hermes revision. Native integration
was tested on macOS; the Linux/macOS CI matrix is configured but has not been
executed on a hosted repository. The Python 3.11 wheel import and explicit
missing-Hermes failure were separately checked, not the full native suite.

Localization update 0.4.2: 166 tests and 217 subtests pass, with 96.11% combined
line/branch coverage. Ruff, formatting, strict mypy, Bandit and the hashed runtime
dependency audit pass. All 17 bundled Hermes language catalogs are checked against
native language lists; the real native settings reader consumes translated forms
without changing defaults or exposing secret values. Native alias parity and
interrupted presentation publication are covered. Wheel/sdist catalog bytes and
isolated wheel resource loading were verified. The installed candidate was updated
with a recoverable backup, preserving configuration and credentials. The user's
active Desktop view was left intact; the new live form requires a Plugins rescan.

Profile connection update 0.5.0: 188 tests pass, with 95.96% combined coverage.
Ruff checks all 55 Python files; strict mypy checks 41 typed source/test files.
Bandit and the hashed runtime dependency audit pass. The native directory loader
is now measured through an explicit source directory, avoiding accidental
instrumentation of standard-library modules named `__init__`.

Real native chat and Kanban startup selection and MCP dispatch pass six A/B/A
cases against the generated connector configurations. They cover 36 tool schemas,
profile-local reads and writes, revisions, sibling isolation, administrator policy,
worker board pins and outside-board denial. A separate installed stdio client
discovers 36 tools and reads real native profiles, including `devops`, without
provider calls. Only the default profile's plugin settings and MCP connection
were changed during installation; existing credentials and hermes_v2 files were
verified unchanged. New sessions consume the connection.

Smoke remediation 0.7.0: 231 tests and 286 subtests passed, with 95.79% combined
coverage. Ruff `ALL`, formatting, strict mypy and Bandit passed; wheel/sdist builds
passed. Real installed HTTP checks verified credential redaction and two model
turns sharing a persisted session. A separate native `devops` Kanban attempt
completed after an obsolete API gateway was retired. Targeted launcher tests
verified that service correction. Independent review added fault-path and
credential-header regressions; all final gates were then rerun successfully.
See `smoke-remediation.md` for causes,
deployment evidence and native limitations.

Regression follow-up 0.8.0: 258 tests and 315 subtests passed, with 95.37%
combined line/branch coverage. Ruff `ALL`, formatting, strict mypy, all-level
Bandit and the locked, hash-checked runtime dependency audit passed. Wheel and
sdist builds passed. New regression checks cover attempt/session attribution,
credential redaction, malformed worker records, concurrent conditional updates,
native audit retention, mutation receipts and post-commit failures. The test
which removes a temporary profile needs macOS process-list access; the final
suite ran with that access instead of weakening the native activity check.
These results are from Python 3.13 on macOS against the pinned Hermes 0.21.5
checkout, not a new execution of the hosted CI matrix.
