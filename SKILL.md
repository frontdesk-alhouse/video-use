---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews — in Final Cut Pro via SpliceKit, with ffmpeg fallback. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL — and, in FCP mode, Final Cut Pro itself (FCPXML in, FCPXML out). They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles sit above every overlay.** ffmpeg: applied LAST in the filter chain. FCP: burned captions are Basic Titles on lane 2, above every overlay (overlays use lane 1); native captions live on the separate caption lane and are never burned. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat** (ffmpeg path), not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **Audio fades at every segment boundary.** ffmpeg: 30ms `afade` (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). FCP: `fadeIn`/`fadeOut` of ≥ 1 frame on every spine clip (`--fade-s 0.03` rounds up to one frame — ≈33ms at 30fps). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the word-level transcript (FCP Parakeet or Scribe).
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory. In FCP mode, every run imports a NEW project — never open-and-edit the user's existing projects.
13. **FCP: preflight, then reconcile before any post-import step.** `bridge.py status` must exit 0 before any FCP call, and `fcp_import.py` must exit 0 (every clip's start/end within 1 frame of `plan.json`) before you verify, grade, export, or touch the timeline. A failed reconcile means the timeline is not your EDL — fix or fall back, never build on it.
14. **FCP: verify text through XML or caption export, never screenshots alone.** Titles and captions are checked in the round-trip FCPXML (`fcp/roundtrip.fcpxml`) and against `master.srt`. `viewer.capture` can return the wrong pane or a stale frame; a screenshot is evidence, not proof.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    │   └── fcp_report.json      ← FCP mode: round-trip + caption parity + captures report
    ├── fcp/                     ← FCP mode only
    │   ├── project.fcpxml       ← generated from edl.json, DTD-validated
    │   ├── plan.json            ← every snapped time (clips, overlays, captions, markers, grade)
    │   ├── import_check.json    ← timeline vs plan reconciliation (Hard Rule 13)
    │   └── roundtrip.fcpxml     ← what FCP actually holds, exported back
    ├── preview.mp4
    ├── final_fcp.mov            ← FCP mode: Share > Export File output, before loudnorm
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key, optional Final Cut Pro mode). Don't re-run it every session; on cold start just verify:

- **Preflight the FCP bridge first:** `python helpers/fcp/bridge.py status` (MCP equivalent: `bridge_status`). Exit 0 prints FCP + SpliceKit versions → **FCP mode**. Exit 1 prints the fallback hint → **ffmpeg path** (`render.py`), exactly as upstream. Re-run it whenever FCP may have quit or relaunched.
- Transcription provider: `VIDEO_USE_STT=fcp|scribe|auto` (default `auto` = FCP's Parakeet engine when the bridge answers, else Scribe). `--provider` on `transcribe.py` / `transcribe_batch.py` overrides it per call.
- `ELEVENLABS_API_KEY` resolves — either in the environment or in `.env` at the video-use repo root. Required only when Scribe transcribes (bridge down, or `VIDEO_USE_STT=scribe`); optional when the bridge is up. If needed and missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`).
- FCP mode only: `xmllint` on PATH (`brew install libxml2` if missing) — `--validate` fails without it.
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Helpers

- **`transcribe.py <video>`** — single-file transcription. `--provider fcp|scribe|auto` (default `$VIDEO_USE_STT`, else auto), `--num-speakers N` optional. Cached. Output is Scribe-shaped JSON whatever the provider, with a `provider` block naming who transcribed.
- **`transcribe_batch.py <videos_dir>`** — batch transcription. 4 parallel workers with Scribe; the FCP provider runs one at a time (it shares one Final Cut). Use for multi-take.
- **`stt/`** — provider layer behind both. `fcp`: SpliceKit's Parakeet engine (`parakeet`) — builds an inventory project (`video-use · inventory …`) with the takes appended on its timeline, transcribes it, and splits the words back per take. `scribe`: ElevenLabs, needs the API key.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

FCP mode (`helpers/fcp/`, details and verified FCPXML snippets in `helpers/fcp/README.md`):

- **`fcp/bridge.py status`** — preflight. Exit 0 = bridge up (JSON with versions); exit 1 = down, prints the ffmpeg fallback hint. `bridge.py call <method> '<json params>'` for raw JSON-RPC.
- **`fcp/edl_to_fcpxml.py <edl.json> --validate`** — EDL → `fcp/project.fcpxml` (FCPXML 1.11) + `fcp/plan.json` (+ `master.srt` unless `--captions none`). Options: `--captions both|native|titles|none` (default `both`, or `edl.fcp.captions`), `--fade-s`, `--fps`, `--event`, `--project`, `--lang`. `--validate` runs `validate.py`.
- **`fcp/validate.py <fcpxml>`** — xmllint against the vendored DTD in `helpers/fcp/dtd/`, plus semantic checks: contiguous spine, sequence duration = sum, refs resolve, media files exist, no same-lane overlaps.
- **`fcp/fcp_import.py <fcpxml>`** — imports into the running FCP (inline under 1 MB, else File > Import > XML + osascript), opens the project, writes `fcp/import_check.json`. Exit 0 only when every clip is within 1 frame of the plan; 1 = reconcile failed; 2 = bridge down.
- **`fcp/fcp_verify.py <edit_dir>`** — exports `fcp/roundtrip.fcpxml`, checks fades/markers/captions/grade against the plan, caption parity vs `master.srt`, flags clips < 0.5s, and grabs the frames 2 before / 2 after every cut **from FCP's own render** (`final_fcp.mov`) plus `verify/cuts_sheet.png`; before an export exists it falls back to `viewer.capture`. Writes `verify/fcp_report.json`. `--frames export|viewer`, `--no-captures`.
- **`fcp/fcp_export.py <edit_dir>`** — Share > Export File (default) → `final_fcp.mov` → two-pass loudnorm (−14 LUFS / −1 dBTP) → `final.mp4`, plus a duration check vs the plan. The master is ProRes, so the loudnorm pass re-encodes to H.264 (CRF 18). `--skip-export` re-runs only loudnorm + check; `--no-loudnorm` skips it; `--burn-captions` burns `master.srt` with upstream's style when ffmpeg has libass (this Mac's ffmpeg does not — use `--captions both|titles` instead).
- **`fcp/fcp_cleanup.py`** — lists the `video-use · …` scratch projects (inventory timelines, probes). Dry run by default; `--delete` moves them to the library trash one by one and undoes + stops if anything other than the target disappears. Ask the user before `--delete`.
- **`fcp/probe.py`, `timing.py`, `captions.py`, `grade_map.py`** — libraries, not CLIs: ffprobe facts, frame-grid rationals, cue chunking shared with `render.py`, ffmpeg grade → Color Adjustments.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Then: **bridge up → FCP mode** (next section: EDL → FCPXML → import → reconcile); **bridge down → ffmpeg**: apply grade per-segment and compose via `render.py`.
6. **Preview.** FCP mode: the project is open in Final Cut — the user can scrub it there. ffmpeg path: `render.py --preview`.
7. **Self-eval (before showing the user).** FCP mode uses the FCP variant below; the ffmpeg path runs `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   Measure the audio, don't assume it: `ffmpeg -i out.mp4 -af ebur128=peak=true -f null -` for integrated loudness and true peak, plus RMS per section (dialogue, music-only, end card). An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Fresh eyes catch what the author stopped seeing — cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## FCP mode (default when the bridge answers)

Same EDL, same craft, same Hard Rules — the cut is assembled as a Final Cut Pro project instead of an ffmpeg render, so the user gets an editable timeline with markers per beat, captions, grade, and overlays already in place. Driven through SpliceKit's JSON-RPC bridge (`127.0.0.1:9876`) inside FCP.

1. **Preflight.** `python helpers/fcp/bridge.py status` → exit 0. Anything else: ffmpeg path for the whole session.
2. **Build.** `python helpers/fcp/edl_to_fcpxml.py edit/edl.json --validate` → `edit/fcp/project.fcpxml` + `plan.json` (+ `master.srt`). Read the printed `note:` lines (clamped ranges, unmapped grade filters, HDR in a Rec. 709 sequence). Validation must say OK.
3. **Import + reconcile.** `python helpers/fcp/fcp_import.py edit/fcp/project.fcpxml` → exit 0 (Hard Rule 13). It picks the route itself: inline `fcpxml.import` under 1 MB; above that, File > Import > XML with the path typed into the open panel (Cmd+Shift+G) and the wide-gamut "Continue" dialog clicked. If FCP raises a dialog on another screen, the script waits on `dialog.detect`; do not click blind. Verified: a 3-clip project imports and reconciles in ~1.8s; the menu route was verified too.
4. **Preview render.** `python helpers/fcp/fcp_export.py edit` → `final_fcp.mov` + `final.mp4`. FCP's own render is the visual ground truth for the next step (and it's what the user will get). ~40 s for a 10 s cut on this Mac.
5. **Verify.** `python helpers/fcp/fcp_verify.py edit` → `verify/fcp_report.json` must be `"ok": true`; read `verify/cuts_sheet.png`. See Self-eval (FCP variant).
6. **Show the user.** The project is open in FCP. Name it in your message (event + project + the library `fcp_import.py` reported), summarize the report, wait for feedback.
7. **Iterate.** Edit `edl.json` → rebuild → re-import as a new project (bump `--project`, e.g. `… v2`) → re-export → re-verify. Never hand-edit the imported timeline through the bridge to "patch" a cut; the EDL stays the source of truth. The last export after the user's OK is the deliverable.
8. **Persist.** Append to `project.md`, including the FCP event/project names and any manual steps you asked the user to do.

**Fallback.** Any failure in steps 1–3 that one retry doesn't fix → `render.py` for that session, and tell the user why in one line. The outputs list stays the same as upstream (`preview.mp4`, `final.mp4`).

**Naming.** Event `video-use · <videos_dir name>`; project `<videos_dir name> · YYYY-MM-DD HH:MM`. Override with `--event`/`--project` or the EDL `fcp` block. A fresh timestamp per run means a new project per run (Hard Rule 12).

**What lands in the XML (verified round-trip on FCP 12.4 / SpliceKit 3.3.9):**

- Spine: one `asset-clip` per EDL range, contiguous on the sequence frame grid, `audioRole="dialogue"`.
- Fades: `<adjust-volume><param name="amount"><fadeIn/><fadeOut/></param></adjust-volume>` on every clip with audio (Hard Rule 3).
- Markers: one per clip at its first frame, `value` = beat name, `note` = reason.
- Overlays: connected `asset-clip` on lane 1, anchored to the spine clip under its start.
- Captions: native `<caption lane="1" role="iTT?captionFormat=ITT.<lang-REGION>">` (editable, exported as a separate track) and/or Basic Title on lane 2 (burned into the render). Default `both`.
- Camera log: `customLUTOverride` on the asset — Apple Log is `112 (Apple_Log)`, auto-detected (see Color grade).
- Grade: Color Adjustments `<filter-video>` per clip (see Color grade). The grade lives in the XML — no post-import grading step.
- Mixed frame rates: FCP adds `<conform-rate>` itself on import.

**Bridge lessons (each cost a broken timeline once):**

- Spine-wide effects: `selectAll` + `apply_effect`. Never `batch_apply_effect` — it walks connected clips too (overlays, titles).
- Never pass multi-MB XML as a string to `import_fcpxml`; `fcp_import.py` switches to the menu route.
- Export File never burns native captions: its Roles tab ships them as a separate caption track. Burned text = titles.
- The menu import route (`File > Import > XML`) lands in the library **selected in the browser**, which may not be the one the inline route uses. `fcp_import.py` prints the library; tell the user if it isn't the expected one.
- SpliceKit reports `Menu execute failed` for `share.export` and `File > Import > XML` even when the sheet/panel opened. The helpers go by `dialog.detect`, not by that error.
- Overlays must be `.mov` (ProRes 4444 when alpha is needed). WebM does not import.

**Verified end-to-end (2026-10-26, FCP 12.4 / SpliceKit 3.3.9):** inline and menu import + reconcile, fades, markers, ProRes 4444 overlay composited with its alpha, native captions with SRT parity, burned Basic Titles above the overlay, Color Adjustments grade, Apple Log Camera LUT, portrait iPhone takes, Share > Export File + loudnorm, and Parakeet transcription of two real takes. **Start timecode rule:** camera files carry a start timecode (`ffprobe` tag `timecode`, e.g. `14:02:48:02`); FCP addresses such media on its timecode timeline, so the asset's `start` must be that timecode and every clip `start`, marker and anchored offset is timecode + source time. `probe.py`/`edl_to_fcpxml.py` do this; declaring `start="0s"` makes FCP drop every clip of that asset silently (that was the "dropped HEVC/Apple Log" mystery). If `fcp_import.py` still reports fewer clips than the plan, fall back to ffmpeg (`render.py`) for that session and tell the user.

### Self-eval (FCP variant)

Same bar as the ffmpeg self-eval; different instruments. `fcp_verify.py` does the mechanical part:

- **Round-trip XML** (`fcp/roundtrip.fcpxml` from `fcpxml.export`) — authoritative. Clip count = plan; one `fadeIn` + `fadeOut` per clip with audio; markers = beat names in order; caption count = plan; `filter-video` on every clip when a grade was requested.
- **Caption parity** vs `master.srt` — same chunking as the ffmpeg path, so texts must match and starts stay within 1 frame (Hard Rule 14).
- **Pacing** — clips under 0.5s are listed; keep them only if intended.
- **Cut frames** — `verify/cut_NN_a.png` / `_b.png` (2 frames either side of every cut) and `verify/cuts_sheet.png`, taken from `final_fcp.mov`, i.e. exactly what FCP rendered. Read the sheet for jumps, flashes, overlay placement, caption readability. So export a preview before the visual pass. Without an export the tool falls back to `viewer.capture`, which always grabs the largest pane and can repeat a stale image (duplicates are counted): evidence, not proof.

Then, after export: `ffprobe` duration vs plan, and the same `ebur128` loudness/true-peak + per-section RMS measurement on `final.mp4`, plus `timeline_view` on `final.mp4` at 2–3 cuts for waveform spikes. The critic sub-agent rule and the 3-pass cap apply unchanged. Report anything you had to leave manual or that moved (caption language role, the library the project landed in).

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

**FCP mode:** the same `grade` value (preset name, raw `eq`/`curves`/`colorbalance` chain, or `auto` per clip) is mapped by `grade_map.py` to Final Cut's Color Adjustments (Contrast, Saturation, Brightness, Shadows, Highlights, Warmth, Tint) and written into each clip as `<filter-video>`. The mapping is an approximation — same intent, different maths; filters it can't map are listed as a `note:`. The grade ships in the XML: no post-import grading step. Set `"fcp": {"grade_mode": "none"}` to leave clips ungraded for the user's own grade. Apple Log sources get Camera LUT = Apple Log automatically (`customLUTOverride`; detected as make=Apple + BT.2020 primaries + an unnamed transfer; iPhone HLG is left alone) — override with `fcp.camera_lut`. HDR sources in the default Rec. 709 sequence are tone-mapped by FCP; override with `fcp.colorSpace`.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. ~2-word chunks, UPPERCASE, break on punctuation and pauses ≥ 0.3s, grow to 3 words rather than flash a cue < 0.35s (`chunk_words` in `render.py`), Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

**FCP mode** — same chunking (`captions.py` imports `render.chunk_words`, so cues can't drift from `master.srt`), two renderings:

- **`--captions both`** (default) — native captions **and** burned titles from the same cues. The deliverable has burned text like the ffmpeg path; the native captions stay editable and export as a sidecar/caption track.
- **`--captions native`** — FCP captions on the caption lane only (iTT role, Helvetica 28 bold). Editable in FCP's caption editor and exportable as SRT, but **not** burned into the export. FCP forces an opaque caption background. The role carries a language-region (`es` → `es-MX`): FCP 12.4 rewrites some bare codes on import (`ITT.es`, `ITT.fr` → `ITT.en`) but keeps `es-MX`, `es-419`, `de`. Pass `--lang es-419` etc. to pick another region.
- **`--captions titles`** — Basic Title on lane 2 only, the bold-overlay look (Helvetica 54 Bold, white with a soft stroke and shadow, 31% up from the bottom like upstream's `MarginV=90`, clear of vertical-video UI). Static per cue, no per-word animation.
- **`--captions none`** — no captions, no `master.srt`.

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**FCP mode:** overlays must be `.mov` — ProRes 4444 when they need alpha (`-c:v prores_ks -profile:v 4444 -pix_fmt yuva444p10le`). WebM does not import into Final Cut. Point the EDL overlay `file` at the `.mov`.

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Fonts fail silently.** A web font that didn't load renders in a fallback face with no error — the video ships in "almost Arial". In HyperFrames/Remotion, await the font load and then assert it: `if (!document.fonts.check('700 76px "Inter"')) throw new Error(...)`. In PIL, pass an explicit font path; never rely on the default.

**Worked example — "show the edit" hook** (a launch video for this tool). Instead of a title card, the first 3s visualize the editing itself: each transcript word pops in on its Scribe timestamp, with bars under it drawn from the real audio envelope; a filler ("ummm") grows letter by letter while it is spoken, turns orange and is cut out on screen at the same frame the audio cuts, and the next line lands immediately. It works because the picture is *driven by the same data as the sound* — one composition (Remotion) reads frame-exact word/envelope JSON produced in Python, so nothing can drift. Use the idea whenever the story is "we removed something": make the removal visible.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Music and sound effects (when requested)

Sound is where generated videos sound cheap. Worked rules from launch edits:

- **Fewer effects.** Every effect is tied to something visible (a cut, a landing, a click). ~20 stock whooshes/risers/impacts in 18s reads as generic; ~8 reads as designed.
- **Hit on the frame.** Most effects have an attack (silence or a build before the transient). Measure it (first sample above ~-30 dBFS of the peak) and start the file `attack` seconds *before* the visible contact frame.
- **Duck music under speech** (roughly -12 to -15 dB relative to its music-only level), and ramp it out before a stinger or end card instead of letting its own tail decay under your CTA.
- **Master once:** mix to PCM, then two-pass loudnorm (-14 LUFS, true peak ≤ -1 dBTP) on the final mix. Then measure per section (see Self-eval).
- **Music taste is the user's call.** Generated music defaults to "hype"; offer two contrasting beds and let the user listen. Don't claim a mix sounds good — you can only measure it.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--filter` or edit the extract command for other targets. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "edit/master.srt",
  "total_duration_s": 87.4
}
```

`grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

**Optional `fcp` block** (FCP mode only; `render.py` ignores it). Every key is optional; CLI flags win over the block:

```json
"fcp": {
  "event": "video-use · launch", "project": "launch · v2",
  "fps": "30000/1001", "width": 1080, "height": 1920,
  "colorSpace": "1-1-1 (Rec. 709)", "lang": "es",
  "captions": "both", "fade_s": 0.03, "grade_mode": "xml",
  "camera_lut": "auto"
}
```

- `event`, `project` — names in the library (default: see FCP mode → Naming).
- `fps` — sequence rate (default: first source's). `width`, `height` — sequence size (default 1920×1080, or 1080×1920 when the first source is portrait).
- `colorSpace` — sequence color space (default Rec. 709).
- `lang` — caption language for the iTT role (default: transcript's `language_code`, else `en`).
- `grade_mode` — `xml` (default: Color Adjustments in the XML) or `none`.
- `captions` — `both` (default) | `native` | `titles` | `none`. `fade_s` — audio fade per edge (default 0.03 → 1 frame).
- `camera_lut` — `auto` (default: Apple Log detected from metadata gets `112 (Apple_Log)`), `none`, a mode name (`Sony_SLog3_SGamut3Cine`, `Panasonic_VLog_VGamut`, …, see `probe.LOG_MODES`), or a per-source map `{"C0103": "none"}`.
- `transform` — per-source rotation for takes shot upside down: `{"C0103": {"rotation": 180}}`. Applies to every spine clip and B-roll cut of that source. Metadata rotation (−90 on iPhone) is handled already; this is on top of it.

**FCP-mode extras in `ranges` and `overlays`** (`render.py` refuses gap/card ranges):

```json
"ranges": [
  {"source": "C0103", "start": 2.42, "end": 6.85, "beat": "COLD OPEN"},
  {"gap": 1.0, "beat": "BLACK"},
  {"card": {"lines": [{"text": "LIBRO CERO:", "size": 70},
                      {"text": "NADIE TIENE UNA TRAYECTORIA LINEAL", "size": 110}],
            "duration": 3.0, "background": "white", "font": "Herculanum"}, "beat": "TITLE"}
],
"overlays": [
  {"source": "C0108", "source_start": 12.0, "start_in_output": 20.5, "duration": 2.5}
]
```

- `{"gap": secs}` — black and silence on the spine.
- `{"card": {…}}` — a still PNG background (`background`: ffmpeg colour, default white) with one editable Basic Title per line. Per card: `font` (default Herculanum), `color` (FCP RGBA, default black on light / white on black), `duration` (default 3). Per line: `text`, `size` (default 90), `position_y` (% of frame height from centre; default spreads lines 9 % apart), `font`, `color`.
- Overlay `source` (instead of `file`) cuts B-roll from an EDL source: `source_start` = in point in the take, `mute` (default true), `rotation` (default: that source's `fcp.transform`). Lane 1, under the burned captions.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU (incl. whisper.cpp) for cut decisions.** Slow, phrase-level, and it normalizes fillers. Use FCP's Parakeet engine via SpliceKit (word-level, with times and confidence) or hosted Scribe. FCP's own transcriber and Apple Speech are not options in this build (broken / unauthorized).
- **`batch_apply_effect` for spine-wide effects.** It walks connected clips (overlays, titles). `selectAll` + `apply_effect`.
- **`import_fcpxml` with a multi-MB XML string.** Unreliable. Let `fcp_import.py` route it through File > Import > XML.
- **Editing the user's existing FCP projects.** Every run imports a new project.
- **Trusting a viewer screenshot for text.** Verify titles/captions in the round-trip XML or the caption export. (Hard Rule 14.)
- **Post-import steps on an unreconciled timeline.** (Hard Rule 13.)
- **WebM overlays in FCP mode.** They don't import. `.mov`, ProRes 4444 for alpha.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
