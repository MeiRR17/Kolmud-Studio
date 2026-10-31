"""agent-run.json must stay importable by Dvir OS (lib/review.ts `parseRun`).

The Python mirror below always runs (also in CI). When a Dvir OS checkout and Node are
available locally — DVIR_OS_DIR, or ../dvir-os next to this repository — the real
TypeScript parser is run on the same files as well.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from core.config import PROJECT_ROOT
from core.jobs import PipelineRunError, PipelineRunner

MAX_TEXT = 200_000  # lib/review.ts


def parse_run_errors(raw: object) -> list[str]:
    """Mirror of Dvir OS parseRun validation (lib/review.ts); returns its error list."""
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["not an object"]

    def text(key: str, value: object, required: bool) -> None:
        if value is None or value == "":
            if required:
                errors.append(f"missing {key}")
        elif not isinstance(value, str):
            errors.append(f"{key} must be text")
        elif len(value) > MAX_TEXT:
            errors.append(f"{key} too long")

    def one_of(key: str, value: object, allowed: tuple[str, ...], required: bool) -> None:
        if value is None:
            if required:
                errors.append(f"missing {key}")
        elif value not in allowed:
            errors.append(f"{key} must be one of {allowed}")

    def url(key: str, value: object) -> None:
        text(key, value, False)
        if isinstance(value, str) and value and not value.lower().startswith(("http://", "https://")):
            errors.append(f"{key} must start with http(s)://")

    def items(key: str, value: object, check) -> None:  # noqa: ANN001
        if value is None:
            return
        if not isinstance(value, list):
            errors.append(f"{key} must be a list")
            return
        for i, item in enumerate(value):
            if check is None:
                text(f"{key}[{i}]", item, True)
            elif not isinstance(item, dict):
                errors.append(f"{key}[{i}] invalid")
            else:
                check(f"{key}[{i}]", item)

    agent = raw.get("agent")
    if not isinstance(agent, dict):
        errors.append("missing agent")
        agent = {}
    text("id", raw.get("id"), False)
    text("title", raw.get("title"), True)
    one_of("kind", raw.get("kind"), ("code", "research", "learning", "content"), True)
    text("agent.name", agent.get("name"), True)
    text("agent.runtime", agent.get("runtime"), False)
    text("agent.model", agent.get("model"), False)
    text("startedAt", raw.get("startedAt"), False)
    text("finishedAt", raw.get("finishedAt"), False)
    text("input", raw.get("input"), True)
    text("summary", raw.get("summary"), True)
    items("goalIds", raw.get("goalIds"), None)
    items("permissions", raw.get("permissions"), None)
    items("steps", raw.get("steps"), lambda k, x: (text(f"{k}.at", x.get("at"), False),
                                                   text(f"{k}.tool", x.get("tool"), True),
                                                   text(f"{k}.detail", x.get("detail"), True)))
    items("artifacts", raw.get("artifacts"), lambda k, x: (
        text(f"{k}.label", x.get("label"), True),
        one_of(f"{k}.kind", x.get("kind"), ("diff", "file", "link", "text"), True),
        text(f"{k}.content", x.get("content"), False), url(f"{k}.url", x.get("url"))))
    items("evidence", raw.get("evidence"), lambda k, x: (
        text(f"{k}.source", x.get("source"), True), text(f"{k}.anchor", x.get("anchor"), False),
        text(f"{k}.quote", x.get("quote"), False), url(f"{k}.url", x.get("url"))))
    items("checks", raw.get("checks"), lambda k, x: (
        text(f"{k}.name", x.get("name"), True),
        one_of(f"{k}.status", x.get("status"), ("pass", "fail", "skipped"), True),
        text(f"{k}.detail", x.get("detail"), False)))
    one_of("confidence", raw.get("confidence"), ("low", "medium", "high"), False)
    items("openQuestions", raw.get("openQuestions"), None)
    text("rollback", raw.get("rollback"), False)
    return errors


def _reports(service, wav_bytes: bytes, tmp_path: Path) -> list[Path]:
    """A completed full-pipeline report and a failed (undecodable input) report."""
    service.settings.xtts_accept_cpml = True
    service.settings.reference_voice_path.parent.mkdir(parents=True, exist_ok=True)
    service.settings.reference_voice_path.write_bytes(wav_bytes)
    good = tmp_path / "lesson.wav"
    good.write_bytes(wav_bytes)
    done = PipelineRunner(service).run(good, until="master", goal_ids=["goal-1"])
    broken = tmp_path / "broken.wav"
    broken.write_text("not audio")
    with pytest.raises(PipelineRunError) as failed:
        PipelineRunner(service).run(broken)
    return [Path(done["reviewPath"]), Path(failed.value.job["reviewPath"])]


def test_agent_run_reports_satisfy_parse_run_contract(service, wav_bytes, tmp_path) -> None:
    completed, failed = _reports(service, wav_bytes, tmp_path)
    for path in (completed, failed):
        report = json.loads(path.read_text(encoding="utf-8"))
        assert parse_run_errors(report) == [], path
        assert report["kind"] == "content" and report["confidence"] == "low"
    statuses = lambda p: [c["status"] for c in json.loads(p.read_text())["checks"]]  # noqa: E731
    assert statuses(completed) == ["pass", "pass", "skipped"]  # human review is never auto-passed
    assert statuses(failed)[0] == "fail"


def test_parse_run_mirror_rejects_what_dvir_os_rejects() -> None:
    base = {"title": "t", "kind": "content", "agent": {"name": "a"}, "input": "i", "summary": "s"}
    assert parse_run_errors(base) == []
    assert parse_run_errors({**base, "kind": "video"})
    assert parse_run_errors({**base, "artifacts": [{"label": "x", "kind": "file", "url": "file:///tmp/x"}]})
    assert parse_run_errors({**base, "checks": [{"name": "x", "status": "approved"}]})
    assert parse_run_errors({k: v for k, v in base.items() if k != "summary"})


def _dvir_os_dir() -> Path | None:
    candidate = Path(os.environ.get("DVIR_OS_DIR", PROJECT_ROOT.parent / "dvir-os"))
    return candidate if (candidate / "lib" / "review.ts").is_file() else None


@pytest.mark.skipif(_dvir_os_dir() is None or shutil.which("node") is None,
                    reason="Dvir OS checkout or Node not available (set DVIR_OS_DIR)")
def test_real_dvir_os_parser_accepts_reports(service, wav_bytes, tmp_path) -> None:
    reports = _reports(service, wav_bytes, tmp_path)
    script = Path(__file__).parent / "contracts" / "check-agent-run.mjs"
    proc = subprocess.run(["node", str(script), str(_dvir_os_dir()), *map(str, reports)],
                          capture_output=True, text=True, timeout=120)
    results = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
    if proc.returncode != 0 and not results:
        pytest.skip(f"Node could not load Dvir OS parser: {proc.stderr.strip()[-300:]}")
    assert [r["ok"] for r in results] == [True, True], results
