"""Machine-readable CLI for local execution and explicit review handoff."""

import argparse
import json
import logging
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
    run.add_argument("--until", choices=("transcript", "translation", "audio", "master", "synchronized", "video"), default="transcript")
    run.add_argument("--input-kind", choices=("audio", "text"), default="audio", help="text accepts a UTF-8 Hebrew script; no source recording required.")
    run.add_argument("--source-timed", action="store_true", help="Keep explicit source-cue links and export captions on the original timeline.")
    resume = commands.add_parser("resume", help="Resume an existing job; optionally extend its target stage.")
    resume.add_argument("job_id")
    resume.add_argument("--until", choices=("transcript", "translation", "audio", "master", "synchronized", "video"))
    resume.add_argument("--source-timed", action="store_true", default=None)
    for command in (run, resume):
        command.add_argument("--voice-mode", choices=("synthetic", "recorded", "mixed"), default=None,
                             help="recorded never calls TTS; mixed preserves recordings and synthesizes missing paragraphs.")
        command.add_argument("--max-speed", type=float, help="Synchronized speech speed cap (1–1.25; default 1.15).")
    status = commands.add_parser("status", help="Inspect a saved checkpoint.")
    status.add_argument("job_id")
    commands.add_parser("jobs", help="List persisted jobs.")
    commands.add_parser("doctor", help="Inspect local prerequisites without running inference.")
    timeline = commands.add_parser("timeline", help="Inspect current source cues, timing and review flags.")
    timeline.add_argument("lesson_id", type=int)
    captions = commands.add_parser("source-captions", help="Export SRT on the original recording's timeline, without TTS.")
    captions.add_argument("lesson_id", type=int)
    captions.add_argument("--hebrew-only", action="store_true")
    recording = commands.add_parser("recording", help="Attach your English recording for one translated paragraph.")
    recording.add_argument("segment_id", type=int)
    recording.add_argument("source", type=Path)
    video = commands.add_parser("video", help="Generate an English text-slide video from current paragraph audio; no filmed video needed.")
    video.add_argument("lesson_id", type=int)
    for name, description in (("dubbing-plan", "Inspect English audio fit to the original source windows."),
                              ("synchronize", "Render English audio on the original source timeline.")):
        command = commands.add_parser(name, help=description)
        command.add_argument("lesson_id", type=int)
        command.add_argument("--max-speed", type=float, default=1.15)
    connect = commands.add_parser("youtube-connect", help="Explicitly connect your Google Desktop OAuth app in the browser.")
    connect.add_argument("client_config", type=Path)
    connect.add_argument("--credentials", type=Path)
    prepare = commands.add_parser("youtube-prepare", help="Prepare an immutable caption generation for owner review (no network).")
    prepare.add_argument("lesson_id", type=int)
    prepare.add_argument("video_id")
    prepare.add_argument("--channel-id", required=True)
    prepare.add_argument("--hebrew-only", action="store_true")
    approve = commands.add_parser("youtube-approve", help="Record your review of specific captions, timings and selected video.")
    approve.add_argument("review", type=Path)
    approve.add_argument("--reviewed", action="store_true", required=True)
    publish = commands.add_parser("youtube-publish", help="Upload/update reviewed native captions and verify remote content.")
    publish.add_argument("review", type=Path)
    publish.add_argument("--credentials", type=Path)
    video_prepare = commands.add_parser("youtube-video-prepare", help="Prepare a generated MP4 for reviewed upload to a new private YouTube video.")
    video_prepare.add_argument("lesson_id", type=int)
    video_prepare.add_argument("manifest", type=Path)
    video_prepare.add_argument("--channel-id", required=True)
    video_approve = commands.add_parser("youtube-video-approve", help="Record owner review of generated video and selected upload channel.")
    video_approve.add_argument("review", type=Path)
    video_approve.add_argument("--reviewed", action="store_true", required=True)
    video_publish = commands.add_parser("youtube-video-publish", help="Resume private video upload, then deliver and verify its native English captions.")
    video_publish.add_argument("review", type=Path)
    video_publish.add_argument("--credentials", type=Path)
    args = parser.parse_args(argv)
    # Engine warnings (e.g. XTTS falling back from Metal to CPU) go to stderr; stdout stays JSON.
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

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
            if args.command == "youtube-connect":
                from core.youtube import DEFAULT_CREDENTIALS, connect
                result = connect(args.client_config, args.credentials or DEFAULT_CREDENTIALS)
            elif args.command == "doctor":
                from core.llm_translator import list_ollama_models
                models = list_ollama_models(settings.ollama_base_url) if loopback_url(settings.ollama_base_url) else []
                result = {"version": __version__, "dataDir": str(settings.data_dir.resolve()), "hardware": detect_hardware(),
                          "ffmpeg": bool(shutil.which("ffmpeg")), "ollamaModelInstalled": settings.ollama_model in models,
                          "cliLocalPolicy": settings.llm_provider == "ollama" and loopback_url(settings.ollama_base_url) and settings.tts_engine == "xtts",
                          "referenceVoiceExists": settings.reference_voice_path.is_file(), "xttsLicenseAccepted": settings.xtts_accept_cpml}
                if settings.reference_voice_path.is_file():
                    from dataclasses import asdict

                    from core.audio_utils import check_and_trim_voice
                    result["referenceVoiceCheck"] = asdict(check_and_trim_voice(settings.reference_voice_path))
            else:
                runner = PipelineRunner(StudioService(settings))
                started = lambda job_id: print(f"Job ID: {job_id} (resume with: kolmud resume {job_id})", file=sys.stderr)
                if args.command == "run":
                    result = runner.run(args.source, until=args.until, title=args.title, goal_ids=args.goal_id, on_started=started,
                                        source_timed=args.source_timed, voice_mode=args.voice_mode, max_speed=args.max_speed,
                                        input_kind=args.input_kind)
                elif args.command == "resume":
                    result = runner.run(job_id=args.job_id, until=args.until, on_started=started, source_timed=args.source_timed,
                                        voice_mode=args.voice_mode, max_speed=args.max_speed)
                elif args.command == "status":
                    result = runner.read(args.job_id)
                elif args.command == "timeline":
                    result = runner.service.get_source_timeline(args.lesson_id).model_dump(mode="json")
                elif args.command == "source-captions":
                    result = runner.service.build_source_captions(args.lesson_id, include_english=not args.hebrew_only).model_dump()
                elif args.command == "video":
                    result = runner.service.build_generated_video(args.lesson_id).model_dump()
                elif args.command == "recording":
                    result = runner.service.attach_segment_recording(args.segment_id, args.source.name,
                                                                      args.source.read_bytes()).model_dump(mode="json")
                elif args.command == "dubbing-plan":
                    result = runner.service.get_dubbing_plan(args.lesson_id, args.max_speed).model_dump()
                elif args.command == "synchronize":
                    result = runner.service.build_synchronized_audio(args.lesson_id, args.max_speed).model_dump()
                elif args.command.startswith("youtube-"):
                    from core.publication import CaptionPublication, VideoPublication
                    publication = VideoPublication(runner.service) if args.command.startswith("youtube-video-") else CaptionPublication(runner.service)
                    if args.command == "youtube-video-prepare":
                        result = publication.prepare(args.lesson_id, args.manifest, args.channel_id)
                    elif args.command == "youtube-prepare":
                        result = publication.prepare(args.lesson_id, args.video_id, args.channel_id,
                                                     include_english=not args.hebrew_only)
                    elif args.command in ("youtube-approve", "youtube-video-approve"):
                        result = publication.approve(args.review, reviewed=args.reviewed)
                    else:
                        from core.youtube import DEFAULT_CREDENTIALS, CaptionClient
                        client = CaptionClient(args.credentials or DEFAULT_CREDENTIALS)
                        try:
                            result = publication.publish(args.review, client)
                        finally:
                            client.close()
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
