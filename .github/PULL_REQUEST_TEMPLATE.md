## Summary

<!-- What changes, and why. One or two sentences is usually enough. -->

Closes #<!-- issue number, if any -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Breaking change
- [ ] Refactor / internal cleanup
- [ ] Documentation
- [ ] Build, CI or release

## How this was verified

<!-- The commands you ran, and anything a reviewer should reproduce by hand. -->

```bash
ruff check src tests scripts
mypy src scripts
lint-imports
pytest -m "not network"
```

## Checklist

- [ ] The gate above passes locally.
- [ ] Tests were added or updated. A bug fix includes a test that failed before it.
- [ ] **Engine purity holds:** no `print()` in `src/porter/core/` (or the root
      facade), and `porter.core` imports none of `porter.cli`, `porter.mcp` or
      `porter.webui`.
- [ ] The zero-configuration path still works without the optional extras.
- [ ] **Dependencies:** none added or removed — *or* `pyproject.toml` and
      [`THIRD_PARTY_NOTICES.md`](https://github.com/RolinShmily/porter-workflow/blob/main/THIRD_PARTY_NOTICES.md) are both updated.
- [ ] **No copyleft dependency was introduced** as an in-process import.
- [ ] Documentation is updated in the same PR (`docs/`, `README.md`,
      `README_zh.md`, `CHANGELOG.md` as applicable).
- [ ] No secrets, tokens, cookies or personal data are included in the diff or logs.

## Notes for the reviewer

<!-- Optional: design trade-offs, things you are unsure about, follow-up work. -->
