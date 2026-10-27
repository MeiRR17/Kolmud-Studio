"""Machine-readable CLI for local execution and explicit review handoff."""

import argparse
import json
import shutil
import sys
from contextlib import redirect_stdout
from pathlib import Path

from core import __version__


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="kolmud", description="Local lesson processing; outputs JSON for orchestration.")
    parser.add_argument("--version", action="version", version=f"Kolmud Studio {__version__}")
    parser.add_argument("--data-dir", type=Path, help="Local lesson/job storage (default: existing project data directory).")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Create a job; default: transcription only, no cloned speech.")
    run.add_argument("source", type=Path)
    run.add_argument("--title")
    run.add_argument("--goal-id", action="append", default=[])
    run.add_argument("--until", choices=("transcript", "translation", "audio", "master"), default="transcript")
    resume = commands.add_parser("resume", help="Resume an existing job; optionally extend its target stage.")
    resume.add_argument("job_id")
    resume.add_argument("--until", choices=("transcript", "translation", "audio", "master"))
    status = commands.add_parser("status", help="Inspect a saved checkpoint.")
    status.add_argument("job_id")
    commands.add_parser("jobs", help="List persisted jobs.")
    commands.add_parser("doctor", help="Inspect local prerequisites without running inference.")
    args = parser.parse_args(argv)

    # --help and --version remain usable without loading model dependencies.
    from core.config import PROJECT_ROOT, Settings, detect_hardware
    from core.jobs import PipelineRunError, PipelineRunner, loopback_url
    from core.pipeline import StudioService

    try:
        overrides = {}
        if args.data_dir:
            directory = args.data_dir.expanduser().resolve()
            overrides = {"data_dir": directory, "reference_voice_path": directory / "voice_profiles" / "my_reference_voice.wav"}
        settings = Settings(**overrides)
        settings.ensure_dirs()
        seed = PROJECT_ROOT / "data" / "lexicon.json"
        if not settings.lexicon_path.exists() and seed.is_file():
            shutil.copyfile(seed, settings.lexicon_path)
        # Model libraries often print progress to stdout. Keep the command's
        # JSON response parseable by the coordinator and route chatter to stderr.
        with redirect_stdout(sys.stderr):
            if args.command == "doctor":
                from core.llm_translator import list_ollama_models
                models = list_ollama_models(settings.ollama_base_url) if loopback_url(settings.ollama_base_url) else []
                result = {"version": __version__, "dataDir": str(settings.data_dir.resolve()), "hardware": detect_hardware(),
                          "ffmpeg": bool(shutil.which("ffmpeg")), "ollamaModelInstalled": settings.ollama_model in models,
                          "cliLocalPolicy": settings.llm_provider == "ollama" and loopback_url(settings.ollama_base_url) and settings.tts_engine == "xtts",
                          "referenceVoiceExists": settings.reference_voice_path.is_file(), "xttsLicenseAccepted": settings.xtts_accept_cpml}
            else:
                runner = PipelineRunner(StudioService(settings))
                started = lambda job_id: print(f"Job ID: {job_id} (resume with: kolmud resume {job_id})", file=sys.stderr)
                if args.command == "run":
                    result = runner.run(args.source, until=args.until, title=args.title, goal_ids=args.goal_id, on_started=started)
                elif args.command == "resume":
                    result = runner.run(job_id=args.job_id, until=args.until, on_started=started)
                elif args.command == "status":
                    result = runner.read(args.job_id)
                else:
                    result = runner.list_jobs()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except PipelineRunError as exc:
        print(json.dumps(exc.job, ensure_ascii=False, indent=2))
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
