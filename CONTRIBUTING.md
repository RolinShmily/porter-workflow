# Contributing to Porter Workflow

Thanks for taking the time to contribute. This page is the entry point; the
details live in [`docs/development/`](docs/development/README.md) so they are not
maintained twice.

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

## Quick start

Requirements: Python ≥ 3.11, [uv](https://docs.astral.sh/uv/), FFmpeg with
`libass`, and (for live YouTube downloads) Deno or Node ≥ 20.

```bash
git clone https://github.com/RolinShmily/porter-workflow
cd porter-workflow
uv sync --extra all --extra dev
```

Run the gate before opening a PR:

```bash
uv run ruff check src tests scripts
uv run mypy src scripts
uv run lint-imports
uv run pytest -m "not network"
```

---

## Read next

| Topic | Document |
| --- | --- |
| Environment, lockfile rules, troubleshooting | [docs/development/setup.md](docs/development/setup.md) |
| The four gates, test tiers, architecture and licence rules | [docs/development/gate.md](docs/development/gate.md) |
| Adding a platform, ASR or translation backend | [docs/development/extending.md](docs/development/extending.md) |
| Release process and launcher | [docs/development/release.md](docs/development/release.md) |
| Documentation conventions | [docs/development/docs.md](docs/development/docs.md) |
| Architecture | [docs/architecture/overview.md](docs/architecture/overview.md) |

### Licence rules

Porter is MIT. Contributions are accepted under the same terms. Do not add a
hard dependency on a copyleft package, and update
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) whenever a dependency changes.
The full rules are in
[docs/development/gate.md#licence-rules](docs/development/gate.md#licence-rules).

---

## Making a change

1. Fork, then branch from `main`. Use a descriptive name
   (`fix/burn-truncation`, `feat/rumble-platform`).
2. Make the change. Keep commits focused.
3. Add or update tests. A bug fix should come with a test that fails before it.
4. Run the gate above.
5. Update documentation in the same PR: the relevant page under `docs/`, plus
   `README.md` / `README_zh.md` / `CHANGELOG.md` for user-visible behaviour.
6. Open the pull request and fill in the template.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/):
`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `chore:`, `perf:`, `build:`, `ci:`.
English and Chinese messages are both fine.

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
