# Contributing to Porter Workflow

Thanks for taking the time to contribute. This document covers what you need to
build, test and land a change. The engine's rationale lives in the code itself:
every non-obvious decision is explained in a module docstring next to it.

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).

---

## Ways to contribute

* **Report a bug** — use the [bug report form](https://github.com/RolinShmily/porter-workflow/issues/new?template=bug_report.yml).
* **Request a feature** — use the [feature request form](https://github.com/RolinShmily/porter-workflow/issues/new?template=feature_request.yml).
* **Report a security issue** — do **not** open a public issue; follow
  [`SECURITY.md`](SECURITY.md).
* **Send a pull request** — see below.

For anything larger than a bug fix, open an issue first so the design can be
agreed before you spend time on it.

---

## Prerequisites

| Requirement | Why |
| --- | --- |
| Python ≥ 3.11 | The floor declared in `pyproject.toml`. CI tests 3.11–3.13. |
| [uv](https://docs.astral.sh/uv/) | Environment and dependency management used throughout. |
| FFmpeg + ffprobe, built with `libass` | The whole media layer. The test suite runs real encodes. |
| Deno or Node ≥ 20 | Only needed for live YouTube downloads, not for the test suite. |

---

## Setting up

```bash
git clone https://github.com/RolinShmily/porter-workflow
cd porter-workflow
uv sync --extra all --extra dev     # installs exactly what uv.lock pins
```

`uv.lock` is committed, and CI installs from it with
`uv sync --locked`, which fails if the lock no longer matches `pyproject.toml`.
So if you add, remove or bump a dependency, run `uv lock` in the same commit.

## The gate

Every change must pass the same four checks CI runs. Run them from the
repository root:

```bash
ruff check src tests packaging     # style + lint (includes T20, see "Architecture rules")
mypy src packaging                 # strict type checking
lint-imports             # enforces the engine/frontend boundary
pytest -m "not slow"     # the fast suite
```

`pytest` without `-m "not slow"` also runs the `slow` tier: end-to-end tests that
shell out to FFmpeg and touch the network. Run those before opening a PR that
touches the media layer, but they are excluded from CI because they are flaky
and slow.

Test tiers are documented in each directory:

* [`tests/unit/`](tests/unit/) — fast, stubbed, no system dependencies.
* [`tests/regression/`](tests/regression/README.md) — behaviour-equivalence
  ports of the v0.1 implementation; some use a real FFmpeg.
* [`tests/integration/`](tests/integration/README.md) — end-to-end, marked
  `slow`.

If you add a test that shells out to FFmpeg or hits the network, mark it
`@pytest.mark.slow`.

---

## Architecture rules

These are enforced by tooling, not convention. A PR that breaks one will fail
the gate.

1. **The engine never imports a frontend.** `porter` must not import
   `porter_cli` or `porter_mcp`. Enforced by `lint-imports`.
2. **The engine never writes to stdout.** In an MCP stdio server, stdout *is*
   the JSON-RPC channel. A single stray `print()` corrupts the protocol. Use
   `porter.logging.get_logger`. Enforced by ruff's `T20` rule.
3. **Respect the layer order.** `lint-imports` encodes the real dependency
   graph; see the comment above `[tool.importlinter]` in `pyproject.toml` for
   why each module sits where it does.
4. **Keep the zero-configuration path light.** Pure Python plus FFmpeg must
   keep working without `openai`, `pillow` or `SpeechRecognition` installed.
   New capabilities go behind an optional extra.

### Licence rules

Porter is MIT. Contributions are accepted under the same terms.

* **Do not add a hard dependency on a copyleft package.** Anything under
  GPL/AGPL stays behind a subprocess boundary, as `videocaptioner` does. If you
  are unsure, ask in the issue before writing code.
* **If you add or remove a dependency, update
  [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)**, including the licence.
* Do not paste code from a copyleft project into this repository.

---

## Making a change

1. Fork, then branch from `main`. Use a descriptive name
   (`fix/burn-truncation`, `feat/rumble-platform`).
2. Make the change. Keep commits focused; squash noise before requesting review.
3. Add or update tests. A bug fix should come with a test that fails before it.
4. Run the gate above.
5. Update documentation in the same PR. Behaviour changes that are not
   reflected in `README.md`, `README_zh.md` or `CHANGELOG.md` are incomplete.
6. Open the pull request and fill in the template.

### Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `feat:`,
`fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `build:`, `ci:`.
A scope is welcome — `fix(mcp): ...`. Both English and Chinese messages are
fine; match the change, not a language policy.

### Documentation language

* `README.md` is English, `README_zh.md` is Chinese. A user-visible change
  should update both.
* Governance files (`CONTRIBUTING`, `CODE_OF_CONDUCT`, `SECURITY`,
  `CHANGELOG`, `THIRD_PARTY_NOTICES`) and GitHub templates are in English.
* `THIRD_PARTY_NOTICES.md` is in English: it is a legal notice.

---

## Adding a platform, ASR backend or translation backend

These three extension points are the most common contributions. Each has a
registry and a transport-shaped base class:

* Platforms — `src/porter/platforms/`. Implement the base in
  `platforms/base.py`, register it, and add URL handling in `platforms/urls.py`.
* ASR backends — `src/porter/asr/`. Implement the chain's backend protocol and
  add it to the chain in `asr/chain.py`.
* Translation backends — `src/porter/translate/`, same shape.

For all three: a backend must fail in a way the chain can fall through, never
by raising out of the chain. Add a unit test with the network stubbed, and a
`doctor` probe if the backend has an environment prerequisite.

---

## Reporting a problem well

A useful bug report includes:

* `porter --version`, your OS, and `python --version`.
* The exact command, with **secrets and URLs redacted**.
* The output of `porter doctor` — it captures the environment in one shot.
* What you expected, what happened, and the smallest input that reproduces it.

Never paste API keys, cookies or session tokens into an issue. `porter config
list` masks secrets; use its output rather than the raw config file.

---

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE), and you confirm you have the right to submit them.
