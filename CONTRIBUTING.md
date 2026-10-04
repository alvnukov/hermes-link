# Contributing

Use Python 3.13 or 3.14 and [uv](https://docs.astral.sh/uv/). The bridge package
supports Python 3.11–3.14; native integration tests require a compatible Hermes
checkout. CI uses the tested native revision below instead of a moving branch.

```sh
git clone https://github.com/NousResearch/hermes-agent.git /tmp/hermes-test-source
git -C /tmp/hermes-test-source checkout 7817bf522af3caf54b30ae59f16157469d7638fc
export TEST_HERMES_REPO=/tmp/hermes-test-source
uv sync --locked --group integration --python 3.13
ln -s "$PWD/.venv" "$TEST_HERMES_REPO/venv"
```

The example uses a fresh disposable Hermes checkout. Its interpreter symlink
matches CI and lets native worker subprocesses use the integration environment.
Keep an existing personal Hermes installation separate from this test checkout.

Run only the relevant checks while editing:

```sh
uv run --no-sync pytest tests/test_core.py
uv run --no-sync ruff check hermes_bridge/core.py
```

Run all gates before proposing a change:

```sh
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync bandit -c pyproject.toml -r hermes_bridge scripts
uv run --no-sync coverage run -m pytest
uv run --no-sync coverage report
uv run --no-sync coverage html
uv export --locked --no-dev --no-emit-project -o /tmp/hermes-runtime-requirements.txt >/dev/null
uv run --no-sync pip-audit --disable-pip --require-hashes -r /tmp/hermes-runtime-requirements.txt
uv build
```

The 95% coverage floor includes lines and branches from every production module
and script. Do not remove modules or introduce coverage exclusions to pass it.
Use temporary native homes and actual native registries, journals and stores.
Replace only unavoidable external provider/process boundaries. Tests must never
submit model turns, use real provider keys, or modify the developer's Hermes home.

Keep new behavior behind the existing native interfaces. Add a failing regression
test before fixing a bug. Preserve MCP input schemas and error envelopes. Native
private interfaces require a comment explaining why the public interface is
insufficient and a test of the contract. Avoid `Any`, unchecked casts and blanket
lint suppressions. See [quality policy](docs/quality.md) for justified exceptions.

Runtime upgrades must be tested against Hermes itself. `mcp==2.0.0` matches the
native Hermes SDK contract; bump it only with compatible native discovery,
tool calls, HTTP authentication and session tests. `uv.lock` pins transitive
runtime and development packages with hashes. Refresh it deliberately and rerun
the dependency audit. Changes to version metadata must update the package,
plugin manifest and changelog together; tests detect drift.

Contributions are licensed under the [MIT license](LICENSE). No contribution
requires a model subscription or real provider credential to run the tests.

Inside a Git checkout, optional commit hooks can be installed with
`uv tool run pre-commit install`. Their configuration runs pinned Ruff and the
same local mypy/Bandit gates; prepare the development environment first.
