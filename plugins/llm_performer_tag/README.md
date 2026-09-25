# LLM Performer Tag Plugin

This Stash plugin identifies performers in images and video scenes using a
vision-capable LLM (OpenAI-compatible API), matches the results against your
existing performer catalog, and applies the performers you select.

## Features

- Adds a UI dropdown action on the image and scene pages: `Tag performers (LLM)`.
- **Images:** sends the single image to your configured LLM endpoint.
- **Scenes:** detects scene changes via ffmpeg's `select` filter, supplements them
  with evenly-spaced frames, and sends the extracted frames (base64 PNGs, kept
  entirely in memory) to the LLM.
- Displays live `Thinking` and `Output` progress in a non-blocking side panel
  while the LLM streams its response.
- Sends your existing performer catalog as context, including aliases and
  performers you marked `ignore_auto_tag` being excluded.
- Optionally adds each performer's disambiguation and gender, and each
  performer's custom image, to help the model match visually.
- Shows every suggestion in a selection modal, badged `existing` or `new`, all
  pre-selected, with `Select all` / `Clear` helpers.
- Applies selected performers via GraphQL mutations, creating any new performers
  and merging them with the performers already on the image or scene.
- Registers as a task via `window.registerTask` for task-compatible UIs.

## Requirements

- Python 3 (invoked as `python` by the plugin's `exec` entry).
- **ffmpeg** and **ffprobe** on the system PATH — scenes only.
- **Pillow** (`pip install Pillow`) — optional; only needed to convert WebP
  images to PNG before they are sent to the LLM. The rest of the plugin uses
  only the standard library.
- The plugin does not require the CommunityScrapers repo at runtime; a bundled
  `stash_helper_fallback.py` is used when `StashPluginHelper` is unavailable.

## Installation

1. Install the optional dependencies you need: `pip install Pillow` for WebP
   images, and make sure `ffmpeg`/`ffprobe` are on PATH if you will tag scenes.
2. Place this folder in your Stash plugins directory as `llm_performer_tag`
   (e.g. `~/.stash/plugins/llm_performer_tag`).
3. Reload plugins in the Stash UI.

## Usage

- Open an image or scene page and use the operations menu (three dots) to run
  `Tag performers (LLM)`, or use the registered task if your UI supports it.
- A side panel opens showing live progress, then the suggested performers with
  checkboxes. Untick anything you don't want, then click `Apply Performers`.
- The panel is non-blocking: the page behind it stays interactive. `Cancel`
  (or the X) closes the panel and stops the running job.

## Configuration (Settings)

- **llmBaseUrl** (env: `LLM_BASE_URL`; default `http://localhost:11434/v1`) —
  OpenAI-compatible API base URL.
- **llmModel** (env: `LLM_MODEL`; default `gemma3:4b-it-q8_0`) — Model name used
  for tagging.
- **llmTemp** (env: `LLM_TEMP`; default `0.7`) — Sampling temperature.
- **llmMaxTokens** (env: `LLM_MAX_TOKENS`; default `-1` = backend default) —
  Maximum tokens to request.
- **llmTimeout** (env: `LLM_TIMEOUT`; default `3600`) — Timeout in seconds for
  LLM requests.
- **enableThinking** (env: `LLM_ENABLE_THINKING`; default `true`) — When
  disabled, sends `enable_thinking`, `chat_template_kwargs`, `reasoning_control`
  and `reasoning_format` to turn off internal chain-of-thought reasoning, which
  can speed up responses on models that support it.
- **includePerformerDescriptions** (env: `LLM_INCLUDE_PERFORMER_DETAILS`; default
  `true`) — Send performer disambiguation and gender alongside performer names.
- **includePerformerImages** (env: `LLM_INCLUDE_PERFORMER_IMAGES`; default
  `false`) — Send each performer's custom image to help the model identify them.
  Only performers with a manually-set image are included; generated default
  placeholders are skipped.
- **maxPerformerImages** (env: `LLM_MAX_PERFORMER_IMAGES`; default `50`) —
  Maximum number of performer images to send (`0` or `-1` = unlimited). Only
  applies when performer images are enabled.
- **sceneThreshold** (env: `LLM_SCENE_THRESHOLD`; default `0.3`) — ffmpeg scene
  detection sensitivity (0.0–1.0). Higher values detect fewer scene changes.
  Scenes only.
- **maxFrames** (env: `LLM_MAX_FRAMES`; default `20`) — Maximum number of frames
  extracted and sent to the LLM (clamped to 1–50). Scenes only.
- **frameWidth** (env: `LLM_FRAME_WIDTH`; default `640`) — Resize extracted
  frames to this width in pixels (`-1` keeps original size). Scenes only.
- **zzdebugTracing** (BOOLEAN; default `false`) — Enables extra debug logging.
- **saveDebugLog** (env: `LLM_SAVE_DEBUG_LOG`; default `false`) — Write the
  sanitized LLM request payload (images stripped) to
  `results/debug_last_run.json`. Each run overwrites the previous log.

## Configuration (Environment Variables)

- `LLM_API_KEY`: API key for authenticated LLM endpoints.
- `LLM_PERFORMER_PROMPT`: Custom system prompt override for the
  performer-identification assistant.

## Notes

- **How suggestions are matched:** each name returned by the LLM is looked up
  with `findPerformers`. A case-insensitive exact name match is applied to that
  existing performer and badged `existing`. Names that match only an existing
  performer's *alias* are dropped — they are neither listed nor applied. Any
  remaining name is badged `new` and created as a new performer on apply.
- **Applying is additive:** the performers already on the image or scene are
  read first and merged with the selection, so nothing is ever removed. New
  performers are created with just a name (no gender, details, or aliases) and
  there is no separate confirmation step for them. If a create fails, that name
  is logged to the browser console and skipped.
- **Catalog size:** the whole performer catalog is fetched and sent as prompt
  context, so very large libraries produce large prompts (and cost/latency).
  Performers flagged `ignore_auto_tag` are excluded.
- **The Tasks page entries** (`Tag image performers (LLM)` / `Tag scene
  performers (LLM)`) require `image_id` or `scene_id` arguments, which the
  frontend supplies when it queues a job. Launched without them, the job logs
  an error and does nothing.
- **No bulk mode:** the plugin only ever targets the single image or scene
  identified by the current URL. There is no batch, wall, or multi-select mode.
- **No thinking switch on the LLM's reasoning output:** whatever the model emits
  is parsed as a JSON array of names, with `<think>` blocks stripped.
- **Results directory:** intermediate results are written to `results/`
  (served as plugin assets for the UI).
  - Streaming progress files are deleted once the final result is written.
  - Completed result files are cleaned up on the next plugin run if older than
    1 hour.
- **Frame extraction is entirely in-memory**; no frame files are written to disk.
  If scene detection yields no frames, extraction falls back to an evenly-spaced
  `fps` filter.

## Troubleshooting

- **`ffmpeg not found on PATH`** — install ffmpeg (which provides ffprobe), or
  tag images instead of scenes.
- **`PluginApi is not available in this UI context`** — the frontend UI script
  requires Stash's `PluginApi`/React/Bootstrap libraries; reload plugins and
  hard-refresh the page.
- **Empty or nonsensical results** — confirm the model is vision-capable, and
  that the prompt and the media are reaching your endpoint. Turn on
  `zzdebugTracing` for server-side logging, or `saveDebugLog` to inspect the
  exact request payload the plugin sent.
- **A performer you expected is missing** — the model did not return that name,
  it was dropped as an alias-only match, or it was excluded via
  `ignore_auto_tag`.
- **Slow responses** — try disabling `enableThinking`, lowering `maxFrames`, or
  disabling `includePerformerImages`.
