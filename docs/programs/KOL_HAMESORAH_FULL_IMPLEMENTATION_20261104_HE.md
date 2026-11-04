# Kol HaMesorah — פלטפורמה מלאה לשימור והנגשת שיעורים, קול ומסורות
**תאריך:** 2026-11-04 · **קוד קנוני:** `MeiRR17/Kolmud-Studio` · **מסמך תכנון בלבד — אין ייצור אודיו/פריסה/פרסום/הורדה.**
הפרויקט הוא הרחבה של Kolmud Studio v0.2.0 (Streamlit, FastAPI, SQLite, MLX Whisper, Ollama, XTTS) ושל CLI/DvirOS handoff שכבר נבנו ב־PR #1. אין ליצור ריפו שני למערכת תמלול.

## 1. חזון, צרכים ומשתמשים
לשמר ולהנגיש ידע תורני וקולי באופן שאפשר לעיין בו, לחפש, ללמוד, לתרגם ולבדוק; לאפשר לכל מוסד/מלמד לנהל אוסף מורשה ולשמור מסורות הגייה/טעמים מבלי לחשוף הקלטות שלא הותרו. **מבחן מוצר:** שיעור שלם עולה, מתומלל, עובר ביקורת, מקבל זמן+מקור+חיפוש+כתוביות+שאלות, נשאר נגיש ומתקן שגיאות ללא אובדן.
**ראשוני:** מורה/מגיד שיעור יחיד, לומד יחיד. **בהמשך:** מוסדות, נגישות לימודית, ארכיון מסורות קוליות. יש לאמת עם 2–3 משתמשים לפני פיתוח רב-ארגוני.
**ערך:** הקטנת זמן הפקת שיעור, דיוק בהבנת תנאים הלכתיים, נגישות לאנשים שקשה להם לעקוב אחרי אודיו, חיפוש בתוך אוסף והפחתת עבודה חוזרת.
**לא מטרה:** לשבט קולות של אנשים אחרים ללא הסכמה, לשכפל פלטפורמת וידאו, ליצור תרגומים המתחזים למקור מוסמך או להעמיד פרויקט פרטי כארכיון ציבורי.

## 2. תשתית קיימת ומניעת כפילות
- README מתאר workflow: Hebrew audio→MLX Whisper→editable transcript→local Ollama English translation/phonetic→XTTS v2 audio→MP3/SRT; shadowing practice ותוצאות Pilot אמיתיות/סינתטיות. לא להחליף את כל המנוע במיקרוסרוויסים.
- v0.2 CLI `kolmud run/resume/status/jobs` תומך בתורים, checkpoints, manifest, agent-run.json, מניעת exports לא מעודכנים, איסור ספקי Cloud בתוך CLI. להרחיב provenance והחזרה במקום להפוך אותו ל-scheduler מקביל.
- XTTS-v2 weights במאגר הקיים מתועדים CPML שימוש לא מסחרי בלבד. **אין להתיר הפצה/שימוש מסחרי ב־XTTS מתוך המוצר בלי החלפת מנוע ורישיון מאומת.**
- `Taamify` הוא בעל המודלים והערכת טעמים/קוראים, ו־`Corpora` הוא catalog של מקורות ואסמכתאות. Kolmud שומר שיעור, גרסאות תמלול והסכמות, קורא metadata בלבד.
- `DvirOS` הוא תור החלטות וביקורת הסוכן; import של agent-run הוא ידני/מאושר ולא active connector.

## 3. ארבעה מסלולי משתמש
A. מורה: טוען הקלטה מורשית → פרטי זכויות → transcribe → עריכת עברית → בדיקת שמות/הלכה → transcript ready → subtitles and approved distribution.
B. לומד: מחפש מושג → נוחת בדקה מדויקת → רואה מילות מקור/תמלול/מקור חיצוני מתועד → שומר נקודה → בוחן הבנה.
C. עורך נגישות: מתקן שגיאות, מוסיף speaker segmentation רק כאשר יש רשות, תיאור קטע, חלוקה לנושאים וכתוביות מותאמות.
D. חוקר מסורת: מקשר לקטעי קריאה מורשים, תוויות טעמים, השוואת קריאה שנערכה אנושית, הערכה דרך Taamify; אין להסיק איכות קריאה ממדד גובה צליל בלבד.

## 4. Data model
`Consent {consent_id, subject_or_rightsholder_ref, operations[], expiry, scope, revocation_revision, evidence_ref}`.
`AudioAsset {asset_id, local_private_locator, sha256, duration_ms, capture_permission, voice_rights, retention_rule, source_owner}`.
`Lesson {id, title, languages, teacher_owner_id, visibility, series_id, source_asset_id, current_revision, review_status}`.
`Segment {id, lesson_revision, start_ms, end_ms, speaker_ref?, source_hash, hebrew_text, confidence_flags, inspection_level}`.
`TranscriptRevision {id, parent_revision, editor, hash, created_at, changed_spans[], qa_state}`.
`TranslationRevision {id, transcript_revision_sha, target_locale, terminology_map_revision, text, verified_by?}`.
`SubtitleExport {id, transcript_sha, speaker/word_timing_revision, srt/vtt_digest, generation_id}`.
`SearchIndexRevision {id, lesson_revision, allowed_scope, collection_policy_sha, index_sha}`.
`LearningPrompt {id, source_segment_refs[], question, answer_rubric, human_approval}`.
`MediaPublication {id, artifact_sha, allowed_audience, public_license, review_receipt, published_at, revoked_at?}`.
All raw audio/voiceprints remain in restricted local folders; never export speaker identity, raw sample or token in Corpora/DvirOS events.

## 5. Architecture
Input wizard with explicit rights → media normalization (ffprobe/ffmpeg, isolated path) → MLX local ASR job (existing CLI) → timestamp aligner → editor/revision DB → segment/topic annotation → source link validator → search index (SQLite FTS5 within Kolmud lesson storage) → subtitle/player/export artifact generator → optional translation/voice synthesis gated by engine license and user consent.
**Orchestration:** reuse `kolmud` state-machine and SQLite; add bounded scheduler only if measured demand, with process locks and per-job budgets. Mac sleep/restart -> checkpoint restart, no duplicate master export, source/voice revision invalidation.
**Search:** local FTS over **approved** transcript revisions; no indexing of revoked lesson. Source citations use Corpora SourceRef but no bulk corpus download.
**Serving:** Streamlit/private admin initially, optional FastAPI authenticated lesson read endpoints for a filtered mobile PWA later. Public static portal only from explicit reviewed distribution artifacts; private data never mounted in web worker.
**Taamify** integration versioned read-only model output with rights/reader disjoint evaluation; no model training within Kolmud.

## 6. Product screens
1. Library: series/lessons, source status, rights expiration, accessible language and review queue.
2. Recording intake: consent/rights scope, file hash, metadata, local upload progress and warning for known voices.
3. Editor: waveform+RTL transcript segmented by timestamp, term dictionary, condition-sensitive QA markers; history and diff.
4. Learner Player: audio/optional video, live subtitles, jump-to-source timecode, speed, searchable notes, keyboard access.
5. Translation Studio: Hebrew baseline vs display translation vs TTS script, avoid confusing translated voice with original teacher.
6. Taam/Pronunciation Lab: Taamify model output only as hypothesis; reader/source labels; error review.
7. Publishing: release/checklist, license, consent, accessibility, subtitles and rightsholder revocation.
8. Operator: CPU/Metal scheduling, disk budget, encryption, backups, worker queue, source drift, errors without private transcripts.

## 7. Versions and workstreams
| Milestone | Feature set | Definition of Done |
|---|---|---|
| KM-01 0.3.0-dev | review actual v0.2 CLI, consent model, secure asset import and content-hash graph | malicious media/path/symlink tests, no unsafe provider |
| KM-02 0.4.0-dev | Hebrew timestamped segmented editor + revision lineage | exact source segment pointers, correction audit and invalidation |
| KM-03 0.5.0-dev | local search, glossary/term review, time-coded source results | repeatable FTS indexing, denied/revoked source unreadable |
| KM-04 0.6.0-dev | learner player, accessible SRT/VTT and source-linked questions | WCAG keyboard, 390px/mobile, subtitle validation |
| KM-05 0.7.0-dev | translation quality gate + model license router + private archive management | noncommercial XTTS refused for commercial mode, no unapproved voice |
| KM-06 0.8.0-dev | Taamify optional analysis adapter + reader-disjoint eval refs | zero real audio pushed to model without consent/rights |
| KM-07 0.9.0-rc | 3 actual licensed lessons, full student + teacher feedback, backup restore rehearsal | measured transcription accuracy incl. halachic qualifiers, no stale exports |
| KM-08 1.0.0 | stable private studio with audit and repeatable deployment/rollback | exact artifact/host receipt, 0 P0, owner-approved private GO |
| KM-09 1.1.x | opt-in multi-organization learning archive and public collections | tenant isolation, publication rights, sustainable support, institutional validation |
Milestones are proposed; no tag or package version is changed by this document.

## 8. Concrete implementation backlog (42 tickets)
**Rights/storage:** KM-T01 signed consent policy; T02 rightsholder authorization; T03 revocation handler; T04 original media hash; T05 private media store; T06 dedupe+retention; T07 privacy purge policy; T08 engine/model license registry.
**ASR/QA:** T09 ffprobe safety; T10 segmentation; T11 timestamps; T12 Hebrew punctuation terms; T13 human correction history; T14 changed-span invalidation; T15 halachic-condition marker review; T16 WER/CER with human gold; T17 multi-speaker rights-aware metadata.
**Learning:** T18 series; T19 FTS5 search; T20 jump-to-time; T21 SRT/VTT; T22 player accessibility; T23 glossary; T24 quiz source anchoring; T25 learning feedback; T26 optional translation QA.
**ML/audio:** T27 local model resource budget; T28 MPS/CPU fallback; T29 provider license gate; T30 voice consent; T31 Taamify readonly evidence; T32 independent-reader test; T33 no stale synthesized master.
**Ops:** T34 CLI checkpoint replay; T35 crash restart; T36 DB backup and restore; T37 export scan for secrets; T38 authz/tenant tests; T39 onboarding; T40 three lesson pilot; T41 smoke deployment receipt; T42 rollback/runbook.
Each issue: owner, dependent modules, approved input, API contract, unit/e2e/security tests, version target, evidence SHA, review status.

## 9. One-month high-output execution strategy
Days 1–3: source review, baseline, consent/rights schema + mock ingestion; establish real lesson permission candidates.
Days 4–7: timestamped editor and correction/invalidation tests; real ASR only on material owned/authorized.
Days 8–14: searchable learner player, subtitles, glossary and original teaching flow; browser test against real sample.
Days 15–19: translation and voice provider gates, media export lineage; independent quality sampling.
Days 20–24: Taamify integration if signed dataset rights, and 3-lesson pilot; user interviews.
Days 25–27: performance/memory testing on measured local hardware, backup restore, private auth and threat model.
Days 28–30: private deployment rehearsal and rollback. External public release gated on approvals not token quota.
Heavy ASR/TTS should queue one at a time initially, subject to disk/RAM and current Corpora transfer. Scheduler disabled until authorized.

## 10. Test matrix and thresholds
- **Audio source:** corrupted file, overlarge file, unsafe links, duration mismatch, codec edge cases, crash mid-transcode.
- **Correctness:** independently human-checked Hebrew excerpts, WER/CER + serious condition omissions tracked separately. Average WER alone insufficient.
- **Edits:** transcript change invalidates translations/subtitle/audio/search; rollback maintains original media; edits during export cannot yield stale "current".
- **Privacy:** revoked consent blocks search/public export, secret/PII redaction, zero Cloud calls in local CLI, private voice cannot leak.
- **Licensing:** XTTS noncommercial weights cannot be selected for commercial deployment; actual terms reviewed for each voice/model/dataset.
- **Usability:** mobile RTL search, playback at variable speed, transcript keyboard and screen-reader semantics, 200% zoom.
- **Operations:** 3 permissioned real lessons, repeatable job resume, backup→isolated restore, exact artifact checksum and deployment checks.
**MVP done** when teacher/editor approves an entire lesson and a learner searches, plays a segment, studies and gives feedback; system logs actual source revision and rights state.
**No false claims:** 3-lesson private pilot is not validated public archive or automatic teacher-quality translation.

## 11. Delivery/security and dependencies
Private first: local Mac app with encrypted backup, 0700/0600 data and authenticated localhost network; no open ports by default. For an approved external portal use static redacted/licensed exports with CDN, TLS/CSP, rate limits and human editorial review. Never put raw voice samples in a public build.
Related repos: `MeiRR17/Taamify` model/consent/evaluation; `MeiRR17/Corpora` permitted source references; `MeiRR17/dvir-os` review card (manual agent-run integration from existing PR #1). `Video_to_text` remains legacy utility; no competing scheduler.
**Owner approval gates:** real recordings, permission from speakers/rightsholders, license decision, institutional users, deployment/publishing, paid model providers. Existing app's README is source of actual runtime requirements; do not assume current host or account can run production 24/7.
