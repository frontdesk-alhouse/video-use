# helpers/fcp — Final Cut Pro mode

How `edl.json` becomes a Final Cut Pro project, and what Final Cut actually accepts. Everything marked **verified** was round-tripped on FCP 12.4 / SpliceKit 3.3.9: built here, imported, exported back with `fcpxml.export`, and diffed. Daily usage lives in `SKILL.md` → "FCP mode"; this file is the reference for changing the builder.

## Pipeline

```
edl.json ──edl_to_fcpxml.py──> fcp/project.fcpxml + fcp/plan.json (+ master.srt)
         ──validate.py──────> xmllint (dtd/FCPXMLv1_11.dtd) + semantic checks
         ──fcp_import.py────> FCP project opened → fcp/import_check.json   (exit 0 = reconciled)
         ──fcp_verify.py────> fcp/roundtrip.fcpxml + verify/fcp_report.json + verify/*.png
         ──fcp_export.py────> final_fcp.mov → loudnorm → final.mp4
```

`plan.json` holds every snapped time (clips, overlays, captions, markers, grade, fade frames). Import reconciliation and verification read it — never re-derive times from the EDL downstream.

## Time

- All math is in integer frames (`timing.Timebase`), written as rationals on the frame grid: `1001/30000s`, `72072/30000s`, `0s`. FCP rejects off-grid values.
- Spine clip: `offset` on the **sequence** grid (contiguous: offset_n = offset_{n-1} + duration_{n-1}); `start` on the **source** grid **plus the file's start timecode**; `duration` on the sequence grid. Starts floor, durations round, never past the media's last frame (FCP silently drops a clip that claims more media than the file has — asset `duration` floors for the same reason).
- **Anchored items are in source time.** Everything inside an `asset-clip` — overlays, captions, titles, markers — has its `offset` on the parent clip's local timeline, which starts at the parent's `start`, not at 0 and not at the sequence offset. An item that should appear 1.0s into a clip whose `start="300s"` gets `offset="301s"`. Getting this wrong puts the item off-screen or on the wrong clip with no error.
- Mixed frame rates: FCP inserts `<conform-rate>` itself on import (**verified**); the builder doesn't write it.

## Child order inside `asset-clip` (DTD 1.11)

```
note?, conform-rate?, timeMap?,
intrinsic video params (adjust-crop … adjust-colorConform)?,
adjust-volume?, adjust-panner?,
anchored items (asset-clip | caption | title | …)*,
markers*,
audio-channel-source*,
filter-video*,
filter-audio*,
metadata?
```

The builder emits: `adjust-transform` → `adjust-volume` → anchored overlays + captions → `marker` → `filter-video`. Out-of-order children fail xmllint; FCP's import is less predictable (it may drop the element silently).

## Elements

### Fades — **verified**

```xml
<adjust-volume amount="0dB"><param name="amount">
  <fadeIn type="easeIn" duration="1/30s"/><fadeOut type="easeOut" duration="1/30s"/>
</param></adjust-volume>
```

On every spine clip with audio. `--fade-s 0.03` rounds to ≥ 1 frame (one frame ≈ 33ms at 30fps), capped at half the clip. Satisfies Hard Rule 3. FCP round-trips it as `<adjust-volume><param name="amount"><fadeIn/><fadeOut/></param></adjust-volume>`.

### Marker — **verified**

```xml
<marker start="72072/30000s" duration="1001/30000s" value="HOOK" note="Cleanest delivery…"/>
```

One per spine clip at its first source frame (`start` = the clip's `start`). `value` = EDL `beat`, `note` = `reason` (or `quote`).

### Overlay — **verified**

```xml
<asset-clip ref="r7" lane="1" name="render" offset="301s" start="0s" duration="5s" format="r6" tcFormat="NDF"/>
```

Connected to the spine clip under the overlay's `start_in_output`, lane 1, offset in that clip's source time. Overlay files must be `.mov` (ProRes 4444 for alpha); WebM doesn't import.

### Rotation (`edl.fcp.transform`) — DTD-valid, not yet round-tripped

```xml
<asset-clip ref="r4" …><adjust-transform rotation="180"/><adjust-volume …>…</asset-clip>
```

Per source, on every spine clip and B-roll cut of it. `adjust-transform` is an intrinsic video param, so it goes before `adjust-volume`. Independent of the −90 metadata rotation, which `display_size` already resolves.

### B-roll cut from a take (overlay with `source`) — DTD-valid, not yet round-tripped

```xml
<asset-clip ref="r4" lane="1" name="C0108" offset="<parent local>" start="<tc + source_start>" duration="2.5s" format="r3" tcFormat="NDF">
  <adjust-transform rotation="180"/><adjust-volume amount="-96dB"/>
</asset-clip>
```

Reuses the source's own asset. `start` = the take's timecode base + `source_start` on its grid; the duration is clamped to the media after `source_start` (plan note). Muted by default. It may run past its parent clip — FCP keeps it connected to that clip; `plan.overlays[].past_parent_s` records by how much.

### Gap and title card in the spine — DTD-valid, not yet round-tripped

```xml
<gap name="BLACK · gap" offset="2s" start="3600s" duration="1s"><marker …/></gap>
<format id="r5" name="FFVideoFormatRateUndefined" width="1080" height="1920" colorSpace="1-13-1 (sRGB IEC61966-2.1)"/>
<asset id="r6" name="card_white_1080x1920.png" start="0s" duration="0s" hasVideo="1" format="r5" videoSources="1">…</asset>
<video ref="r6" name="TITLE · card" offset="3s" start="3600s" duration="3s">
  <title ref="r9" lane="1" offset="3600s" … ><!-- one Basic Title per line, lane = line number --></title>
  <marker …/>
</video>
```

Gaps and stills have no media time of their own: like titles, their local timeline starts at 3600 s, and anchored items use that base. The card background is a PNG drawn by ffmpeg (`color=c=<background>`) into `edit/fcp/`; the text stays editable as Basic Titles (flat style, no stroke/shadow, default Herculanum — it has Í/Á/É/Ó/Ú/Ñ). `plan.clips[]` carries `kind: gap|card`, `has_audio: false`; captions skip them and `seg_offset` advances by their duration, so `master.srt` parity holds. `fcp_verify` counts captions and grades on `asset-clip`s only; `fcp_import.reconcile` accepts a detailed state that omits gaps.

### Native caption — **verified**

```xml
<caption lane="1" offset="301s" name="NINETY PERCENT" duration="21/25s" role="iTT?captionFormat=ITT.es-MX">
  <text placement="bottom"><text-style ref="ts1">NINETY PERCENT</text-style></text>
  <text-style-def id="ts1"><text-style font="Helvetica" fontSize="28" bold="1"
      fontColor="1 1 1 1" backgroundColor="0 0 0 0"/></text-style-def>
</caption>
```

- Cues come from `render.chunk_words` (imported, not copied) so texts match `master.srt` exactly.
- Same lane may not overlap by even a frame; the builder nudges a cue's start one frame past the previous end.
- FCP forces an opaque caption background regardless of `backgroundColor`.
- Language needs a region. Probed on FCP 12.4: `ITT.es` and `ITT.fr` come back as `ITT.en`, `SRT.es` as `SRT.en`; `ITT.es-MX`, `ITT.es-419`, `SRT.es-MX` and `ITT.de` survive. `edl_to_fcpxml.caption_locale` adds a default region (`es` → `es-MX`, `en` → `en-US`, …).
- Export File never burns native captions (Roles tab → "Closed Captions" = a separate track). For burned text use titles (`--captions both`, the default, emits both from the same cues).

### Basic Title (`--captions titles|both`) — **verified**, burned into the export

```xml
<effect id="r9" name="Basic Title" uid=".../Titles.localized/Bumper:Opener.localized/Basic Title.localized/Basic Title.moti"/>
<title ref="r9" lane="2" offset="301s" name="NINETY PERCENT" start="3600s" duration="21/25s">
  <text><text-style ref="ts1">NINETY PERCENT</text-style></text>
  <text-style-def id="ts1"><text-style font="Helvetica" fontSize="54" fontFace="Bold" fontColor="1 1 1 1"
      strokeColor="0 0 0 0.5" strokeWidth="-4" shadowColor="0 0 0 0.2" shadowOffset="1 315"
      shadowBlurRadius="6" alignment="center"/></text-style-def>
  <adjust-transform position="0 -19"/>
</title>
```

Lane 2 keeps titles above overlays (Hard Rule 1; verified: the title renders over a 50 % red ProRes 4444 overlay). `position` Y is % of frame height from centre; −19 ≈ 31 % up from the bottom = upstream's `MarginV=90`, clear of vertical-video UI. Verify text through the round-trip XML, never a viewer capture (Hard Rule 14).

### Grade: Color Adjustments `filter-video` — **verified**

```xml
<effect id="r8" name="Color Adjustments" uid="FxPlug:7E2022A5-202B-4EEB-A311-AC2B585D01B0"/>
…
<filter-video ref="r8" name="Color Adjustments">
  <param name="Control Range" key="19" value="0 (SDR)"/>
  <param name="Contrast" key="17" value="12"/>
  <param name="Saturation" key="16" value="-12"/>
</filter-video>
```

Parameter keys (as FCP writes them; all values are percentages centred on 0):

| Param | key | | Param | key |
|---|---|---|---|---|
| Black Point | 1 | | Midtones Warmth | 12 |
| Brightness | 2 | | Midtones Tint | 13 |
| Exposure | 3 | | Shadows Warmth | 14 |
| Shadows | 4 | | Shadows Tint | 15 |
| Highlights | 7 | | Saturation | 16 |
| Highlights Warmth | 10 | | Contrast | 17 |
| Highlights Tint | 11 | | Control Range | 19 (`"0 (SDR)"` / `"1 (HDR)"`) |

`grade_map.ffmpeg_filter_to_params` maps the EDL grade (preset from `grade.py`, raw `eq`/`curves`/`colorbalance`, or `auto` per clip): `eq` contrast/saturation/brightness/gamma → Contrast/Saturation/Brightness; `curves` master 0.25/0.75 points → Shadows/Highlights; `colorbalance` r−b per zone → Warmth, g → Tint. An approximation; unmapped filters are reported as plan notes. Only non-zero params are written.

### Camera LUT: `asset@customLUTOverride` — **verified**

```xml
<asset id="r3" name="IMG_0132.mov" start="758521/15s" … customLUTOverride="112 (Apple_Log)">
```

Value is `"<logID> (<name>)"`. The IDs aren't in any plain-text resource; they were read back from FCP 12.4 by importing 1 s clips with IDs 0–139 and exporting (unknown IDs return `0 (None)`): ARRI 1/3/4, Canon 16–18, Sony 33–36, BMD 48–50, Panasonic V-Log 64, DJI D-Log 80, Nikon N-Log 96, **Apple Log 112, Apple Log 2 113**, Fujifilm F-Log 128/129 (`probe.LOG_MODES`). `SourceInfo.log_profile` detects Apple Log as make=Apple + BT.2020 primaries + a transfer ffprobe can't name; iPhone HLG declares `arib-std-b67` and is left alone. Verified on a real iPhone 16 Pro Apple Log take: the export has normal contrast and colour instead of the flat log image.

### Assets and formats

One `format` per distinct (width, height, frame duration, colorSpace) — width/height are the **display** size (rotated iPhone takes are 2160×3840, not their coded 3840×2160, or FCP letterboxes them); one `asset` per source with `<media-rep kind="original-media" src="file:///…"/>` (percent-encoded). colorSpace strings: `1-1-1 (Rec. 709)`, `9-1-9 (Rec. 2020)`, `9-16-9 (Rec. 2020 PQ)`, `9-18-9 (Rec. 2020 HLG)`. HDR sources in the default Rec. 709 sequence are tone-mapped by FCP.

**Start timecode (solved 2026-10-26):** media with a `timecode` tag (iPhone, cameras) lives on its timecode timeline in FCP. The asset declares `start=<tc>` (`probe.SourceInfo.tc_start_frames`, drop-frame aware) and every clip `start`, `marker start` and anchored `offset` adds that base. With `start="0s"` FCP imports the project but silently drops all clips of the asset — no dialog, no error, `importOK: true`. `fcp_import.py` catches it as a clip-count mismatch.

## Import recipe

- **Request under 60 000 bytes** (`fcp_import.import_route`, measured on the JSON-RPC request, escaped quotes included): `fcpxml.import` with `xml=<string>, internal=True`. A 3-clip fixture imported and reconciled in ~1.8s. **SpliceKit 3.3.9 parses each 64 KB socket read as a separate message**, so a bigger inline request never parses and the call hangs until timeout with no dialog and nothing imported (measured 2026-10-27: 54.8 KB raw imported, 68.9 KB hung; sending the same request in slow chunks returns `Parse error` per chunk). `fcpxml.import` takes no path parameter — `xml` only.
- **Over the limit** (or `--force-menu`) — any project with burned captions on more than a minute of speech — **verified**: never send the XML as a string. `menu.execute ["File","Import","XML"]` (reports "Menu execute failed" even though the panel opens; wait for a dialog titled Import XML) → activate FCP + `AXRaise` the panel (it can open on another screen) → osascript: Cmd+Shift+G, type the absolute path, Return, Return → poll `dialog.detect` for up to 10s and click `Continue` / `OK` / `Import` (a wide-gamut library asks for the color mode; the menu route also shows an "imported with the following warnings" alert whose text isn't readable by accessibility). **This route imports into the library selected in the browser** — the fixture landed in another library than the inline route's. `fcp_import.py` prints and records the library. Needs Accessibility/Automation permission for the terminal.
- Preflight refuses to start with a **modal** dialog or sheet open, or a library mid-save. Non-modal panels such as `FFOSCOverlayWindow` (on-screen controls) are ignored.
- Then `project.open` by name until `timeline.getDetailedState` reports that sequence, and reconcile: spine items (lane 0, no transitions) vs `plan.clips`, start/end within `--tolerance` frames (default 1). End = start + duration — `endTime` comes back on a coarse timescale.

## Export recipe — **verified**

`share.export destination="Export File (default)"` (may report "Menu execute failed" while the sheet opens — trust `dialog.detect`) → `dialog.click "Next"` → NSSavePanel filled by osascript (Cmd+Shift+G to the folder, Cmd+A, type the name, Return) → click `Replace` if asked → wait until the file stops growing and `backgroundRender.status` is idle → ffprobe duration vs plan → two-pass loudnorm with upstream's targets (−14 LUFS / −1 dBTP / LRA 11) that **re-encodes** the video (ProRes `.mov` can't be stream-copied into `.mp4`). Container/codec of `final_fcp.mov` are whatever the user's Export File destination is set to (here ProRes 422 + 24-bit PCM). Measured: −14.0 LUFS. `--burn-captions` needs an ffmpeg with libass; Homebrew's current bottle has neither libass nor drawtext.

## Bridge RPC methods per helper

JSON-RPC 2.0, newline-delimited, `127.0.0.1:9876` (`SPLICEKIT_HOST` / `SPLICEKIT_PORT` override), fresh socket per call; event frames (with `method`) are skipped.

| Helper | Methods |
|---|---|
| `bridge.py` | `bridge.alive` (liveness, off the main thread), `system.version` (`status`) |
| `fcp_import.py` | `system.version`, `system.callMethod` (`FFLibraryDocument isAnyLibraryUpdating`), `dialog.detect`, `fcpxml.import`, `menu.execute`, `dialog.click`, `project.open`, `timeline.getDetailedState` |
| `fcp_verify.py` | `system.version`, `fcpxml.export`, `timeline.getDetailedState`; only without an export: `playback.seekToTime`, `viewer.capture`, `timeline.capture` |
| `fcp_cleanup.py` | `project.open` (its error lists every project), `timeline.action revealProjectInBrowser`, `timeline.action moveToTrash`, `timeline.action undo` |
| `fcp_export.py` | `system.version`, `share.export`, `dialog.detect`, `dialog.click`, `backgroundRender.status` |
| `stt/fcp.py` | `fcpxml.import` of an inventory project built by `edl_to_fcpxml` (every take full length, event `video-use · inventory`), `project.open`, `timeline.getDetailedState`, `transcript.setEngine parakeet`, `transcript.open`, `transcript.getState`, `transcript.close`; fallback when clips get dropped: `media.importFile` (lands in the library's first event) + `browser.appendClip` |

## Lessons (each broke a timeline once)

- Spine-wide effects: `selectAll` + `apply_effect`. `batch_apply_effect` walks connected clips too.
- Never pass multi-MB XML as a string.
- `viewer.capture` ignores every parameter and grabs the largest pane (often the browser), sometimes a stale image. `fcp_verify.py` takes the cut frames from the exported render instead; the round-trip XML and caption parity vs `master.srt` stay the authoritative text checks.
- Transcription: FCP's own engine is broken in this build and Apple Speech is unauthorized; Parakeet works (9.7s Spanish take → 24 words with punctuation, word times, confidence, silences, in ~18s).
- Every run is a new project. Never edit the user's existing projects.
- iPhone takes report a jittery `avg_frame_rate` (`3706800/123563`, `30700/1023`) next to `r_frame_rate` `30/1`. `probe` snaps to the nominal rate within 0.05 % (NTSC vs integer is 0.1 %, so 29.97 stays 29.97); otherwise each take got its own off-grid format.
- The menu import route and `fcp_export.py` drive save/open panels with osascript: they fail while the screen is locked (`ioreg -n Root -d1 -a | grep -A1 CGSSessionScreenIsLocked`). Inline import and every RPC keep working.
- FCP-engine transcription (`VIDEO_USE_STT=fcp`) hung for 37 min on a 22-take, 11-minute inventory (`transcript.getState` stuck at `transcribing`, progress 0/0) and left the takes plus `video-use · inventory` projects in the library selected in the browser. Close it with `transcript.close` before the next import.
