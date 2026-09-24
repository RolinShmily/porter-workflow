# Integration tests

End-to-end tests that shell out to a **real** `ffmpeg`. They are marked `slow`
so they stay out of the fast feedback loop:

```bash
pytest -m "not slow"        # default developer loop
pytest -m "not network"     # what CI runs: the fast suite plus every offline test
pytest -m slow              # every integration test
pytest -m network           # only the ones that reach the internet (opt-in)
```

`slow` means "needs a real system binary; deterministic". `network` means
"reaches a public endpoint". CI runs everything except `network`, so the gate
never depends on a third party being up, while the ffmpeg tier — the one mocks
cannot substitute for — still runs.

## What belongs here

| Test | Verifies |
| --- | --- |
| `test_media_pipeline.py` | Standardisation, audio extraction and probing against real ffmpeg |
| `test_burn_pipeline.py` | The whole pipeline through BURN: two playable release videos, subtitles actually visible in the pixels, and the options-binding contract |
| `test_job_cancel.py` | Cross-process cancellation: the CLI cancels a job running here, and it actually stops |

## Conventions

* Never assert on download *content* — URLs rot. Assert on structure
  (platform detected, duration > 0, aspect ratio sane).
* Never leave artifacts behind: write under `tmp_path`.
* Skip, do not fail, when an external dependency is absent
  (`shutil.which("ffmpeg") is None` → `pytest.skip`).
* Mark anything that reaches the internet `@pytest.mark.network` as well as
  `slow`.

`test_job_cancel.py` spawns the real CLI as a subprocess, so the two processes
have to agree on the registry path. They agree through `XDG_CACHE_HOME` rather
than by being handed a path, which is how it works in production; a second test
in that file asserts the fixture actually achieved that, because otherwise a path
mismatch would masquerade as a cancellation bug.

The `test_burn_pipeline.py` fixtures use a video title containing an
apostrophe on purpose: `sanitize_filename` keeps apostrophes, so the task
directory contains one, and that is the case v0.1's escaped filtergraph path
could not burn. A test with a plain directory name would not have caught it.
