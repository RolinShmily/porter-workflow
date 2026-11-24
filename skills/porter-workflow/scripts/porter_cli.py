#!/usr/bin/env python3
"""Self-contained Agent CLI for the ``porter-workflow`` skill.

Calls the published ``porter-workflow`` PyPI package's public Python API
(``import porter``) directly and emits structured JSON on stdout, keeping all
diagnostics and logs on stderr.

When invoked from a Python environment that does not already have
``porter >= 0.4.0`` installed, this script transparently re-executes itself
through ``uv run --no-project --with porter-workflow>=0.4.0`` so no manual
virtualenv creation or activation is required.

Usage:
    python3 <SKILL_DIR>/scripts/porter_cli.py --host-check
    python3 <SKILL_DIR>/scripts/porter_cli.py version
    python3 <SKILL_DIR>/scripts/porter_cli.py doctor
    python3 <SKILL_DIR>/scripts/porter_cli.py capabilities [--stage STAGE] [--available]
    python3 <SKILL_DIR>/scripts/porter_cli.py probe "<SOURCE>"
    python3 <SKILL_DIR>/scripts/porter_cli.py plan "<SOURCE>" --out plan.json
    python3 <SKILL_DIR>/scripts/porter_cli.py run --plan plan.json
    python3 <SKILL_DIR>/scripts/porter_cli.py jobs list [--all]
    python3 <SKILL_DIR>/scripts/porter_cli.py jobs status <job_id>
    python3 <SKILL_DIR>/scripts/porter_cli.py jobs cancel <job_id>
    python3 <SKILL_DIR>/scripts/porter_cli.py config path | show | set key=value
    python3 <SKILL_DIR>/scripts/porter_cli.py webui [--host 127.0.0.1] [--port 7860]
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

__all__ = ["main"]

DEFAULT_SPEC = "porter-workflow>=0.4.0"
WEB_SPEC = "porter-workflow[web]>=0.4.0"
MIN_PORTER_VERSION = (0, 4)
_MIN_PYTHON = (3, 8)
_BOOTSTRAPPED_ENV = "_PORTER_SKILL_BOOTSTRAPPED"

#: Environment variables kept inside the host's writable workspace when
#: ``PORTER_SKILL_STATE_DIR`` is configured.
_STATE_DIRS: tuple[tuple[str, str], ...] = (
    ("UV_CACHE_DIR", "uv-cache"),
    ("UV_TOOL_DIR", "uv-tools"),
    ("UV_TOOL_BIN_DIR", "uv-tools/bin"),
    ("UV_PYTHON_INSTALL_DIR", "uv-python"),
    ("UV_PYTHON_BIN_DIR", "uv-python/bin"),
    ("XDG_CACHE_HOME", "cache"),
    ("PORTER_CACHE_DIR", "porter-cache"),
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_MISUSE = 2
EXIT_NOT_FOUND = 127
EXIT_CANCELLED = 130


# ----------------------------------------------------------------------
# Entry point & uv self-bootstrap
# ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    if sys.version_info < _MIN_PYTHON:
        _err(
            f"porter-skill: Python {_MIN_PYTHON[0]}.{_MIN_PYTHON[1]}+ is required "
            f"(found {sys.version.split()[0]})."
        )
        return EXIT_MISUSE

    env = os.environ.copy()
    state = env.get("PORTER_SKILL_STATE_DIR", "").strip()
    if state:
        if not os.path.isabs(state):
            _err(f"porter-skill: PORTER_SKILL_STATE_DIR must be absolute: {state}")
            return EXIT_MISUSE
        _apply_state_dir(env, Path(state))

    if args[:1] == ["--host-check"]:
        return _host_check(env, state_configured=bool(state))

    if not _check_configured_dirs(env):
        return EXIT_ERROR

    if args[:1] == ["webui"]:
        return _launch_webui(args[1:], env)

    force_uv = env.get("PORTER_SKILL_FORCE_UV", "").strip().lower() in ("1", "true", "yes")
    if (force_uv or not _has_compatible_porter()) and not env.get(_BOOTSTRAPPED_ENV):
        return _reexec_with_uv(args, env)

    # Apply any state-dir overrides to the current process too when running in-process.
    if state:
        _apply_state_dir(os.environ, Path(state))

    return _dispatch(args)


def _has_compatible_porter() -> bool:
    try:
        import porter
    except ImportError:
        return False
    version = str(getattr(porter, "__version__", "") or "")
    parts = []
    for piece in version.split(".")[:2]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            return False
        parts.append(int(digits))
    return tuple(parts) >= MIN_PORTER_VERSION


def _uv_command(extra: list[str]) -> list[str]:
    """Build a ``uv`` command list, handling Windows ``.cmd``/``.bat`` shims."""
    resolved = shutil.which("uv")
    if resolved is None:
        return ["uv", *extra]
    if os.name == "nt" and resolved.lower().endswith((".cmd", ".bat")):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", resolved, *extra]
    return [resolved, *extra]


def _reexec_with_uv(args: list[str], env: dict[str, str]) -> int:
    spec = env.get("PORTER_SKILL_SPEC", "").strip() or DEFAULT_SPEC
    script = str(Path(__file__).resolve())
    child_env = dict(env)
    child_env[_BOOTSTRAPPED_ENV] = "1"
    command = _uv_command(
        [
            "run",
            "--no-project",
            "--python",
            ">=3.11,<3.14",
            "--with",
            spec,
            "python",
            script,
            *args,
        ]
    )
    return _spawn(command, child_env)


def _launch_webui(web_args: list[str], env: dict[str, str]) -> int:
    spec = env.get("PORTER_SKILL_WEB_SPEC", "").strip() or WEB_SPEC
    command = _uv_command(
        [
            "run",
            "--no-project",
            "--python",
            ">=3.11,<3.14",
            "--with",
            spec,
            "porter",
            *web_args,
        ]
    )
    return _spawn(command, env)


def _spawn(command: list[str], env: dict[str, str]) -> int:
    try:
        code = subprocess.call(command, env=env)  # noqa: S603
    except FileNotFoundError:
        _err(
            "porter-skill: uv not found on PATH; install uv "
            "(https://docs.astral.sh/uv/) or install porter-workflow into this Python."
        )
        return EXIT_NOT_FOUND
    except OSError as exc:
        _err(f"porter-skill: could not start uv: {exc}")
        return EXIT_NOT_FOUND
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    return 128 - code if code < 0 else code


# ----------------------------------------------------------------------
# Host & state-directory helpers (stdlib only)
# ----------------------------------------------------------------------


def _apply_state_dir(env: Any, state: Path) -> None:
    for name, relative in _STATE_DIRS:
        env[name] = str(state / relative)


def _host_check(env: dict[str, str], *, state_configured: bool) -> int:
    if not state_configured:
        _err(
            "porter-skill: no workspace state directory configured; host defaults "
            "cannot be certified."
        )
        _err(
            "porter-skill: in a restricted host set PORTER_SKILL_STATE_DIR to an "
            "absolute writable workspace path."
        )
    if not _check_configured_dirs(env):
        return EXIT_ERROR
    _err("porter-skill: configured writable directories OK.")
    return EXIT_OK


def _check_configured_dirs(env: dict[str, str]) -> bool:
    for name, _relative in _STATE_DIRS:
        raw = env.get(name)
        if not raw:
            continue
        if not _is_writable(Path(raw).expanduser()):
            _err(
                f"porter-skill: cannot write {raw}; select a writable workspace "
                "with PORTER_SKILL_STATE_DIR"
            )
            return False
    return True


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".porter-skill-write-{uuid.uuid4().hex}"
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        probe.unlink()
    except OSError:
        return False
    return True


# ----------------------------------------------------------------------
# JSON command surface directly over ``import porter``
# ----------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="porter_cli.py",
        description="JSON-first Agent CLI built directly over the porter Python API.",
    )
    parser.add_argument("--config", dest="config_path", metavar="PATH", help="Explicit config file.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Emit package and Python version JSON.")
    sub.add_parser("doctor", help="Probe host dependencies and effective configuration.")

    caps = sub.add_parser("capabilities", help="List stage capabilities, cost and egress.")
    caps.add_argument("--stage", help="Filter by stage (probe/acquire/normalize/transcript/translate/write/render).")
    caps.add_argument("--available", action="store_true", help="Only list currently available capabilities.")

    probe = sub.add_parser("probe", help="Inspect a URL, local media, or subtitle without downloading.")
    probe.add_argument("source", metavar="SOURCE")
    probe.add_argument("--cookies", metavar="FILE")
    probe.add_argument("--cookies-from-browser", metavar="BROWSER")

    plan = sub.add_parser("plan", help="Resolve an executable WorkflowPlan JSON.")
    plan.add_argument("source", metavar="SOURCE")
    _add_plan_args(plan)
    plan.add_argument("--out", metavar="FILE", help="Write the plan JSON directly to FILE (UTF-8).")

    run = sub.add_parser("run", help="Execute a saved plan (--plan) or resolve-and-run a source.")
    run.add_argument("source", nargs="?", metavar="SOURCE")
    run.add_argument("--plan", dest="plan_file", metavar="FILE", help="Saved WorkflowPlan JSON.")
    _add_plan_args(run)
    run.add_argument("--out", metavar="FILE", help="Write WorkflowResult JSON to FILE (UTF-8).")

    jobs = sub.add_parser("jobs", help="Inspect or cancel jobs in the shared JobRegistry.")
    jobs_sub = jobs.add_subparsers(dest="jobs_action", required=True)
    jl = jobs_sub.add_parser("list")
    jl.add_argument("--all", dest="show_all", action="store_true")
    js = jobs_sub.add_parser("status")
    js.add_argument("job_id")
    jc = jobs_sub.add_parser("cancel")
    jc.add_argument("job_id")
    jobs_sub.add_parser("clear")

    cfg = sub.add_parser("config", help="Inspect or update Porter configuration (secrets masked).")
    cfg_sub = cfg.add_subparsers(dest="config_action", required=True)
    cfg_sub.add_parser("path")
    cfg_sub.add_parser("show")
    cs = cfg_sub.add_parser("set")
    cs.add_argument("assignment", metavar="KEY=VALUE")

    return parser


def _add_plan_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-o", "--output-dir", metavar="DIR")
    parser.add_argument("--task", choices=("probe", "transcribe", "subtitle", "release", "convert", "audio"))
    parser.add_argument("--burn", metavar="MODE", help="dual, zh_only, bilingual_only, skip.")
    parser.add_argument("--quality", metavar="QUALITY", help="best, 2160p, 1440p, 1080p, 720p, 480p.")
    parser.add_argument("--target-lang", metavar="LANG")
    parser.add_argument("--transcript", metavar="CAPABILITY")
    parser.add_argument("--translate", metavar="CAPABILITY")
    parser.add_argument("--render", metavar="CAPABILITY")
    parser.add_argument("--subtitle-file", metavar="FILE")
    parser.add_argument("--llm-model", metavar="MODEL")
    parser.add_argument("--cookies", metavar="FILE")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER")
    parser.add_argument("--no-denoise", action="store_true", default=None)
    parser.add_argument("--allow-paid", action="store_true")
    parser.add_argument("--force-translate", action="store_true")
    parser.add_argument("--force", action="store_true")


def _dispatch(args: list[str]) -> int:
    parser = _build_parser()
    try:
        ns = parser.parse_args(args)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else EXIT_MISUSE

    import porter

    porter.configure_logging("warning")

    try:
        if ns.command == "version":
            return _cmd_version()
        if ns.command == "doctor":
            return _cmd_doctor(ns)
        if ns.command == "capabilities":
            return _cmd_capabilities(ns)
        if ns.command == "probe":
            return _cmd_probe(ns)
        if ns.command == "plan":
            return _cmd_plan(ns)
        if ns.command == "run":
            return _cmd_run(ns)
        if ns.command == "jobs":
            return _cmd_jobs(ns)
        if ns.command == "config":
            return _cmd_config(ns)
    except KeyboardInterrupt:
        _err("porter-skill: interrupted")
        return EXIT_CANCELLED
    except Exception as exc:  # noqa: BLE001 - surface structured error to caller
        _emit_json({"ok": False, "error": str(exc)})
        return EXIT_ERROR
    return EXIT_MISUSE


def _resolve_config(ns: argparse.Namespace) -> Any:
    from porter import resolve

    return resolve(ns.config_path)


def _cmd_version() -> int:
    import porter

    _emit_json(
        {
            "name": "porter-workflow",
            "version": porter.__version__,
            "python": sys.version.split()[0],
        }
    )
    return EXIT_OK


def _cmd_doctor(ns: argparse.Namespace) -> int:
    from porter import run_doctor
    from porter.core.doctor import report_payload

    cfg = _resolve_config(ns) if ns.config_path else None
    report = run_doctor(cfg)
    _emit_json(report_payload(report))
    return EXIT_OK if report.ok else EXIT_ERROR


def _cmd_capabilities(ns: argparse.Namespace) -> int:
    from porter import JobOptions, RunContext, capability_catalog

    cfg = _resolve_config(ns)
    ctx = RunContext(job_id="capabilities", options=JobOptions(output_dir=cfg.output_dir), config=cfg)
    items = list(capability_catalog(ctx))
    if ns.stage:
        items = [item for item in items if item.stage.value == ns.stage]
    if ns.available:
        items = [item for item in items if item.available]
    _emit_json([item.model_dump(mode="json") for item in items])
    return EXIT_OK


def _cmd_probe(ns: argparse.Namespace) -> int:
    from porter import JobOptions, RunContext, probe_source

    cfg = _resolve_config(ns)
    ctx = RunContext(
        job_id="probe",
        options=JobOptions(
            output_dir=cfg.output_dir,
            cookies_file=Path(ns.cookies) if ns.cookies else cfg.cookies_file,
            cookies_browser=ns.cookies_from_browser or cfg.cookies_browser,
            video_quality=cfg.video_quality,
        ),
        config=cfg,
    )
    probed = probe_source(ns.source, ctx)
    _emit_json(probed.model_dump(mode="json"))
    has_blocker = any(issue.severity == "blocker" for issue in probed.warnings)
    return EXIT_ERROR if has_blocker else EXIT_OK


def _plan_from_ns(ns: argparse.Namespace, cfg: Any) -> Any:
    from porter import plan_source

    return plan_source(
        ns.source,
        config=cfg,
        task=ns.task or "release",
        output_dir=ns.output_dir,
        target_lang=ns.target_lang or "zh-Hans",
        burn=ns.burn or "dual",
        quality=ns.quality,
        transcript=ns.transcript,
        translate=ns.translate,
        render=ns.render,
        subtitle_file=ns.subtitle_file,
        cookies_file=ns.cookies,
        cookies_browser=ns.cookies_from_browser,
        llm_model=ns.llm_model,
        force=bool(ns.force),
        audio_denoise=None if ns.no_denoise is None else False,
        allow_paid=bool(ns.allow_paid),
        force_translate=bool(ns.force_translate),
    )


def _cmd_plan(ns: argparse.Namespace) -> int:
    cfg = _resolve_config(ns)
    plan = _plan_from_ns(ns, cfg)
    payload = plan.to_dict()
    if ns.out:
        out_path = Path(ns.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _emit_json(payload)
    return EXIT_OK if plan.feasible else EXIT_ERROR


def _cmd_run(ns: argparse.Namespace) -> int:
    from porter import JobState, WorkflowPlan, run_plan

    if not ns.plan_file and not ns.source:
        _err("porter-skill: run requires SOURCE or --plan FILE")
        return EXIT_MISUSE

    cfg = _resolve_config(ns)
    if ns.plan_file:
        text = _read_json_text(Path(ns.plan_file))
        plan = WorkflowPlan.model_validate_json(text)
        runtime_updates: dict[str, Any] = {}
        if ns.output_dir:
            runtime_updates["output_dir"] = str(ns.output_dir)
        if ns.force:
            runtime_updates["force"] = True
        if runtime_updates:
            plan = plan.model_copy(
                update={"runtime": plan.runtime.model_copy(update=runtime_updates)}
            ).sealed()
    else:
        plan = _plan_from_ns(ns, cfg)

    outcome = run_plan(plan, config=cfg)
    payload = outcome.workflow.to_dict()
    if ns.out:
        out_path = Path(ns.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _emit_json(payload)
    if outcome.workflow.state is JobState.DONE:
        return EXIT_OK
    if outcome.workflow.state is JobState.CANCELLED:
        return EXIT_CANCELLED
    return EXIT_ERROR


def _cmd_jobs(ns: argparse.Namespace) -> int:
    from porter import JobRegistry

    registry = JobRegistry()
    if ns.jobs_action == "list":
        records = registry.read(reap=True)
        if not ns.show_all:
            records = [rec for rec in records if not rec.is_finished]
        _emit_json([rec.to_json() for rec in records])
        return EXIT_OK
    if ns.jobs_action == "status":
        record = registry.get(ns.job_id, reap=True)
        if record is None:
            _emit_json({"ok": False, "error": f"unknown job id: {ns.job_id}"})
            return EXIT_ERROR
        _emit_json(record.to_json())
        return EXIT_OK
    if ns.jobs_action == "cancel":
        record = registry.get(ns.job_id, reap=True)
        if record is None or record.is_finished:
            _emit_json({"ok": False, "error": f"cannot cancel job: {ns.job_id}"})
            return EXIT_ERROR
        ok = registry.request_cancel(ns.job_id)
        _emit_json({"ok": ok, "job_id": ns.job_id, "cancel_requested": ok})
        return EXIT_OK if ok else EXIT_ERROR
    if ns.jobs_action == "clear":
        removed = registry.clear()
        _emit_json({"ok": True, "removed": removed})
        return EXIT_OK
    return EXIT_MISUSE


def _cmd_config(ns: argparse.Namespace) -> int:
    from porter import save_key, writable_config_path

    if ns.config_action == "path":
        _emit_json({"path": str(writable_config_path())})
        return EXIT_OK
    if ns.config_action == "show":
        cfg = _resolve_config(ns)
        _emit_json(cfg.masked())
        return EXIT_OK
    if ns.config_action == "set":
        if "=" not in ns.assignment:
            _err("porter-skill: config set expects KEY=VALUE")
            return EXIT_MISUSE
        key, raw_value = ns.assignment.split("=", 1)
        value = _coerce_value(raw_value)
        target = Path(ns.config_path) if ns.config_path else writable_config_path()
        written = save_key(key.strip(), value, target=target)
        _emit_json({"ok": True, "path": str(written), "key": key.strip()})
        return EXIT_OK
    return EXIT_MISUSE


def _coerce_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in ("null", "none"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _read_json_text(path: Path) -> str:
    """Decode UTF-8, UTF-8 BOM, or UTF-16 BOM JSON files (e.g. PowerShell ``>``)."""
    raw = path.read_bytes()
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16")
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig")
    return raw.decode("utf-8")


def _emit_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _err(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


if __name__ == "__main__":
    sys.exit(main())
