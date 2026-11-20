# Native YouTube captions (unreleased)

This connector delivers the current Hebrew/English captions to an **existing owned
video**, or a **new generated video with English captions** using the separate
workflow below. No existing video is needed for the generated route. Processing jobs
never publish implicitly. The local FastAPI interface has
no publishing endpoint; no Google account is connected by installing this branch.

## Generated video upload

Generate a video with `kolmud run SCRIPT --input-kind text --until video`, an audio
input with `--until video`, or `kolmud video LESSON_ID` for an existing lesson. These
produce text slides and English narration without a filmed source. The result contains
`manifest_path`, MP4, narration WAV, SRT, poster and storyboard paths.

With the same data directory and an explicitly connected channel:

```bash
kolmud youtube-video-prepare LESSON_ID /absolute/path/generated_video/GENERATION/manifest.json --channel-id CHANNEL_ID
# Watch the MP4; review narration, captions, rights, metadata and selected channel.
kolmud youtube-video-approve /absolute/path/publications/REQUEST/youtube-review.json --reviewed
kolmud youtube-video-publish /absolute/path/publications/REQUEST/youtube-review.json
```

The review shows exact title/description, Education category and **private** visibility.
Synthetic narration is marked in upload metadata. Only private uploads are supported;
public visibility/scheduling is a separate owner action. Changed title, source text,
audio, generated files, font or channel invalidates delivery. Preparing another request
creates another upload identity: retry the **same review** after an interruption.

Upload follows [Google's resumable protocol](https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol)
and [videos.insert](https://developers.google.com/youtube/v3/docs/videos/insert). A private
receipt saves the server session **before** sending media, uploads bounded 8 MiB chunks,
and queries the acknowledged offset after a lost response. The returned video ID is
checkpointed before captions, so a caption failure reuses that video. An expired or
ambiguous session fails for reconciliation; it never automatically starts a second
upload. The session URI is a private capability excluded from stdout/errors and stored
only in mode-0600 receipts. Don't share that file while a session remains active.

After upload, ownership, duration, title and private visibility are read back, followed
by native **English** captions on the generated narration timeline. Original Hebrew
audio times are not attached to the newly paced video. `verified_private_delivery`
means those checks and caption readback passed, not public publication, linguistic
approval or pixel-for-pixel YouTube transcoding verification.

`awaiting_youtube_processing` means captions are still processing; retry later. Video
processing can temporarily prevent duration verification: the receipt keeps the video
ID in `interrupted_reconcile_required`, and retry reuses it. Real OAuth/upload/channel
behavior remains unverified until an owner-selected pilot is run.

## One-time owner setup

1. In your own Google Cloud project enable YouTube Data API v3, configure the OAuth
   consent screen/test user as required by Google, and create a **Desktop app** client.
2. Keep its downloaded client JSON outside all source repositories. Run:

   ```bash
   kolmud youtube-connect /private/path/client_secret_desktop.json
   ```

3. Choose the intended channel/account in the browser and consent. The command uses
   PKCE, unpredictable callback state, an ephemeral IPv4 loopback listener and fixed
   Google endpoints. Consent denial, scope refusal or timeout does not save credentials.

The default refresh-token file is `~/.config/kolmud/youtube.json`, mode 0600, with a
new parent directory mode 0700. Access tokens remain in memory. An optional
`--credentials /private/path/youtube.json` must be used consistently on connect and
publish; credential storage inside the Kolmud checkout is refused. Do not commit the
client JSON or refresh tokens. You can revoke app access in your Google account; local
credentials are not automatically removed by revocation. No refresh-token expiry or
consent-screen testing limit is assumed; reconnect if Google requires it.

The required scope is `youtube.force-ssl`, one of the scopes accepted by the
[caption insert](https://developers.google.com/youtube/v3/docs/captions/insert),
[update](https://developers.google.com/youtube/v3/docs/captions/update) and
[download](https://developers.google.com/youtube/v3/docs/captions/download) APIs.
It allows broader YouTube changes than this connector performs; the code uses only
channel/video ownership reads and caption operations. Desktop OAuth follows
[Google's installed-app flow](https://developers.google.com/identity/protocols/oauth2/native-app).

## Prepare, review, deliver

First create a source-timed translation (README). A Hebrew-only export needs no
translation. Substitute the exact video ID and the intended `UC…` channel ID:

```bash
kolmud youtube-prepare LESSON_ID VIDEO_ID --channel-id CHANNEL_ID
# Or add --hebrew-only to exclude the English track.
```

The JSON response contains `reviewPath` and the SRT/manifest paths. Review the source
fidelity, translation, times and **match between the local source and selected video**.
Channel ownership and similar duration alone cannot prove the video contains the
same recording. Pending source flags remain available in the manifest. A corrected
cue keeps original ASR evidence; its word timing is not silently forced-aligned.

After actually reviewing that generation:

```bash
kolmud youtube-approve /absolute/path/youtube-review.json --reviewed
kolmud youtube-publish /absolute/path/youtube-review.json
```

Approval binds the caption-manifest bytes and file hashes, source snapshot,
translations, languages and selected video/channel. Any relevant edit invalidates
it. Approval records the owner's declaration; it does not manufacture a model QA
score or change the draft export's `publicationReady: false` field. Human review is
stored in the separate `youtube-approval.json` artifact.

Publishing verifies the connected channel owns the selected video and checks its
duration against the local recording (2 s rounding allowance). It then inserts or
updates only tracks named `Kolmud Studio (he)` / `Kolmud Studio (en)`, leaving other
tracks intact. Reserved names identify managed tracks; don't use those names for
manual caption tracks. Upload uses multipart/related with original timestamps,
never YouTube's deprecated `sync` parameter. The selected tracks become non-draft.

## Results and recovery

`youtube-receipt.json` stores track IDs, actions and per-language verification:

| Status | Meaning and next action |
| --- | --- |
| `verified` | Both requested tracks (or Hebrew only) are serving, and downloaded SRT text and millisecond times match the reviewed generation. |
| `awaiting_youtube_processing` | Upload was acknowledged or an existing track is still syncing. Run the **same** publish command later to reconcile; this status is not publication success. |
| `interrupted_reconcile_required` | An operation failed, inputs changed, or remote readback differed. A language might already be live. Inspect the receipt/Studio, then retry the same unchanged review or prepare/review the corrected generation. |

Successful languages are checkpointed before continuing. Retries list managed tracks
and compare remote content before writing; a lost insert acknowledgement does not
cause a blind second insert. A per-video local lock prevents simultaneous CLI writers
sharing the credential directory. There is no distributed lock across computers;
an ambiguous API conflict fails for reconciliation. Uploads are not atomic across
languages, and a remote write cannot be undone automatically after a concurrent
local edit. The receipt never labels such a run verified. Unrecognized remote content
or failures require inspection; the connector does not delete tracks.

Errors omit response bodies/tokens. Google quotas apply to reads, upload/update and
readback; retry manually instead of polling continuously. The application does not
enable billing or use paid AI APIs for this workflow. Live OAuth, actual upload and
channel-specific behavior remain unverified until an owner-selected pilot is run.

## Dubbed audio is a separate delivery gap

The synchronized WAV/MP3/MP4 generation is implemented locally. No public API route
for adding our own secondary audio track to the same YouTube video has been verified.
[YouTube's documented flow](https://support.google.com/youtube/answer/13338784?hl=en)
uses desktop Studio, and access depends on channel features. Do not report native
caption publication as completed dubbed-audio delivery. New automatically generated
English videos use the explicit workflow above; that is separate from adding a
secondary track to an existing filmed source video.
