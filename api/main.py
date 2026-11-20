"""FastAPI backend exposing the full pipeline over HTTP (same service layer as the Streamlit UI).

Run:  uvicorn api.main:app --reload --port 8000     ·  docs at http://localhost:8000/docs
"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from core import __version__
from core.audio_utils import VOICE_CHECK_MESSAGES, AudioToolError
from core.config import detect_hardware, get_settings
from core.llm_translator import TranslationError, list_ollama_models
from core.pipeline import LLMChoice, NotFoundError, StudioService, TTSChoice, WorkflowStateError
from core.runtime import PipelineBusyError
from core.schemas import AttemptOut, LessonOut, LessonSummary, MasterOutputs, SegmentOut, SegmentUpdate
from core.schemas import SourceCaptionOutputs, SourceCueOut, SourceCueUpdate, SourceTimelineOut
from core.schemas import DubbingPlan, SynchronizedOutputs, GeneratedVideoOutputs
from core.tts_cloner import TTSError

app = FastAPI(title="Kolmud Studio — TorahVoice & Accent Lab", version=__version__)


@lru_cache(maxsize=1)
def get_service() -> StudioService:
    return StudioService(get_settings())


Service = Annotated[StudioService, Depends(get_service)]


@app.exception_handler(NotFoundError)
async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(FileNotFoundError)
@app.exception_handler(WorkflowStateError)
@app.exception_handler(PipelineBusyError)
async def _missing_file(_: Request, exc: FileNotFoundError) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(TranslationError)
@app.exception_handler(TTSError)
@app.exception_handler(AudioToolError)
async def _engine_error(_: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---------------------------------------------------------------- request bodies
class LessonCreate(BaseModel):
    title: str = ""


class TranscriptUpdate(BaseModel):
    hebrew_transcript: str


class TranslateRequest(BaseModel):
    provider: Literal["ollama", "openai", "anthropic"] | None = None
    model: str | None = None
    api_key: str = ""
    base_url: str = ""
    source_timed: bool = False


class SynthesizeRequest(BaseModel):
    engine: Literal["xtts", "elevenlabs"] | None = None
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""


class DubbingRequest(BaseModel):
    max_speed: float = Field(default=1.15, ge=1, le=1.25, allow_inf_nan=False)


class LexiconEntry(BaseModel):
    term: str
    phonetic: str


def _tts_choice(svc: StudioService, body: SynthesizeRequest | None) -> TTSChoice:
    default = svc.default_tts_choice()
    if body is None or body.engine is None:
        return default
    return TTSChoice(body.engine, body.elevenlabs_api_key or default.elevenlabs_api_key,
                     body.elevenlabs_voice_id or default.elevenlabs_voice_id)


# ---------------------------------------------------------------- system
@app.get("/health")
def health(svc: Service) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "hardware": detect_hardware(),
        "ollama_models": list_ollama_models(svc.settings.ollama_base_url),
        "reference_voice": svc.settings.reference_voice_path.exists(),
    }


@app.put("/voice-profile")
async def upload_reference_voice(svc: Service, file: UploadFile = File(...)) -> dict:
    check = svc.save_reference_voice(file.filename or "voice.wav", await file.read())
    return {"reference_voice_path": str(svc.settings.reference_voice_path), "check": asdict(check),
            "warnings": [VOICE_CHECK_MESSAGES[code] for code in check.warnings]}


# ---------------------------------------------------------------- lessons
@app.get("/lessons", response_model=list[LessonSummary])
def list_lessons(svc: Service) -> list[LessonSummary]:
    return svc.list_lessons()


@app.post("/lessons", response_model=LessonOut, status_code=201)
def create_lesson(body: LessonCreate, svc: Service) -> LessonOut:
    return svc.create_lesson(body.title)


@app.get("/lessons/{lesson_id}", response_model=LessonOut)
def get_lesson(lesson_id: int, svc: Service) -> LessonOut:
    return svc.get_lesson(lesson_id)


@app.delete("/lessons/{lesson_id}", status_code=204)
def delete_lesson(lesson_id: int, svc: Service) -> None:
    svc.delete_lesson(lesson_id)


@app.post("/lessons/{lesson_id}/audio", response_model=LessonOut)
async def upload_lesson_audio(lesson_id: int, svc: Service, file: UploadFile = File(...)) -> LessonOut:
    svc.get_lesson(lesson_id)
    return svc.attach_source_audio(lesson_id, file.filename or "lecture.wav", await file.read())


@app.post("/lessons/{lesson_id}/transcribe", response_model=LessonOut)
def transcribe_lesson(lesson_id: int, svc: Service) -> LessonOut:
    return svc.transcribe(lesson_id)


@app.put("/lessons/{lesson_id}/transcript", response_model=LessonOut)
def update_transcript(lesson_id: int, body: TranscriptUpdate, svc: Service) -> LessonOut:
    return svc.set_transcript(lesson_id, body.hebrew_transcript)


@app.post("/lessons/{lesson_id}/translate", response_model=LessonOut)
def translate_lesson(lesson_id: int, svc: Service, body: TranslateRequest | None = None) -> LessonOut:
    choice = svc.default_llm_choice()
    if body and body.provider:
        choice = LLMChoice(body.provider, body.model or choice.model, body.api_key, body.base_url)
    elif body and body.model:
        choice.model = body.model
    return svc.translate(lesson_id, choice, source_timed=body.source_timed if body else False)


@app.get("/lessons/{lesson_id}/source-timeline", response_model=SourceTimelineOut)
def get_source_timeline(lesson_id: int, svc: Service) -> SourceTimelineOut:
    return svc.get_source_timeline(lesson_id)


@app.patch("/source-cues/{cue_id}", response_model=SourceCueOut)
def update_source_cue(cue_id: int, body: SourceCueUpdate, svc: Service) -> SourceCueOut:
    return svc.update_source_cue(cue_id, body)


@app.post("/lessons/{lesson_id}/source-captions", response_model=SourceCaptionOutputs)
def build_source_captions(lesson_id: int, svc: Service, hebrew_only: bool = False) -> SourceCaptionOutputs:
    return svc.build_source_captions(lesson_id, include_english=not hebrew_only)


@app.get("/lessons/{lesson_id}/source-captions/{language}")
def download_source_captions(lesson_id: int, language: Literal["he", "en"], svc: Service) -> FileResponse:
    result = svc.build_source_captions(lesson_id, include_english=language == "en")
    return FileResponse(result.hebrew_srt_path if language == "he" else result.english_srt_path,
                        media_type="application/x-subrip", filename=f"lesson_{lesson_id}.source.{language}.srt")


@app.post("/lessons/{lesson_id}/synthesize", response_model=LessonOut)
def synthesize_lesson(lesson_id: int, svc: Service, body: SynthesizeRequest | None = None,
                      only_missing: bool = True) -> LessonOut:
    choice = _tts_choice(svc, body)
    for seg in svc.get_lesson(lesson_id).segments:
        path = svc.abs_path(seg.audio_path)
        if not only_missing or path is None or not path.is_file() or seg.audio_is_stale:
            svc.synthesize_segment(seg.id, choice)
    return svc.get_lesson(lesson_id)


@app.post("/lessons/{lesson_id}/master", response_model=MasterOutputs)
def build_master(lesson_id: int, svc: Service) -> MasterOutputs:
    return svc.build_master(lesson_id)


@app.post("/lessons/{lesson_id}/generated-video", response_model=GeneratedVideoOutputs)
def generate_lesson_video(lesson_id: int, svc: Service) -> GeneratedVideoOutputs:
    return svc.build_generated_video(lesson_id)


@app.get("/lessons/{lesson_id}/master/{kind}")
def download_master(lesson_id: int, kind: Literal["wav", "mp3", "srt"], svc: Service) -> FileResponse:
    master = svc.get_master(lesson_id)
    path = Path(getattr(master, f"{kind}_path"))
    return FileResponse(path, filename=f"lesson_{lesson_id}.{kind}")


# ---------------------------------------------------------------- segments
@app.patch("/segments/{segment_id}", response_model=SegmentOut)
def update_segment(segment_id: int, body: SegmentUpdate, svc: Service) -> SegmentOut:
    return svc.update_segment(segment_id, body)


@app.post("/segments/{segment_id}/regenerate-phonetic", response_model=SegmentOut)
def regenerate_phonetic(segment_id: int, svc: Service) -> SegmentOut:
    return svc.regenerate_phonetic(segment_id)


@app.post("/segments/{segment_id}/synthesize", response_model=SegmentOut)
def synthesize_segment(segment_id: int, svc: Service, body: SynthesizeRequest | None = None) -> SegmentOut:
    return svc.synthesize_segment(segment_id, _tts_choice(svc, body))


@app.put("/segments/{segment_id}/recording", response_model=SegmentOut)
async def upload_segment_recording(segment_id: int, svc: Service, file: UploadFile = File(...)) -> SegmentOut:
    return svc.attach_segment_recording(segment_id, file.filename or "english.wav", await file.read())


@app.get("/lessons/{lesson_id}/dubbing-plan", response_model=DubbingPlan)
def get_dubbing_plan(lesson_id: int, svc: Service, max_speed: float = 1.15) -> DubbingPlan:
    return svc.get_dubbing_plan(lesson_id, max_speed)


@app.post("/lessons/{lesson_id}/synchronized-audio", response_model=SynchronizedOutputs)
def build_synchronized_audio(lesson_id: int, svc: Service, body: DubbingRequest | None = None) -> SynchronizedOutputs:
    return svc.build_synchronized_audio(lesson_id, body.max_speed if body else 1.15)


@app.get("/segments/{segment_id}/audio")
def segment_audio(segment_id: int, svc: Service,
                  speed: Annotated[float, Query(ge=0.5, le=2.0)] = 1.0) -> FileResponse:
    return FileResponse(svc.segment_audio(segment_id, speed), media_type="audio/wav")


@app.post("/segments/{segment_id}/practice", response_model=AttemptOut)
async def practice(segment_id: int, svc: Service, file: UploadFile = File(...)) -> AttemptOut:
    suffix = Path(file.filename or "attempt.wav").suffix or ".wav"
    return svc.evaluate_practice(segment_id, await file.read(), suffix)


@app.get("/segments/{segment_id}/attempts", response_model=list[AttemptOut])
def attempts(segment_id: int, svc: Service) -> list[AttemptOut]:
    return svc.list_attempts(segment_id)


# ---------------------------------------------------------------- lexicon
@app.get("/lexicon")
def get_lexicon(svc: Service) -> dict[str, str]:
    return svc.lexicon.entries()


@app.put("/lexicon")
def upsert_term(entry: LexiconEntry, svc: Service) -> dict[str, str]:
    try:
        svc.lexicon.upsert(entry.term, entry.phonetic)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return svc.lexicon.entries()


@app.delete("/lexicon/{term}")
def delete_term(term: str, svc: Service) -> dict[str, str]:
    if not svc.lexicon.delete(term):
        raise HTTPException(404, f"'{term}' is not in the lexicon")
    return svc.lexicon.entries()


@app.post("/lexicon/apply")
def apply_lexicon(body: dict[str, str], svc: Service) -> dict[str, str]:
    return {"text": svc.lexicon.apply(body.get("text", ""))}
