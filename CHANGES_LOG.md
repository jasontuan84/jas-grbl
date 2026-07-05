# Changelog — jasGrbl

All notable changes to the **jasGrbl** Inkscape extension are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/); versions follow the
`__version__` in `jasgrbl_pkg/__init__.py`.

## [Unreleased]

## [1.0.0]

First stable release.

### Added
- **Extensions > jas GRBL > Author** menu entry. Clicking it shows an info alert with the
  application name, version, author and website, read from `jasgrbl_pkg.__init__`. Uses
  Inkscape's bundled GTK 3 `MessageDialog` and falls back to `inkex.errormsg()` when GTK is
  unavailable. `__app_name__` was added to the package metadata.

### Fixed
- **GRBL "Test Laser" button did not fire the beam**. Two problems: the pulse used a `G4`
  dwell (which GRBL turns the laser **off** during), and, more fundamentally, GRBL laser mode
  (`$32=1`) refuses to turn the laser on while the machine is **idle** - it only fires during
  motion - so a stationary test pulse never lit up. Now, like LaserGRBL/LightBurn's "fire"
  button, the test briefly leaves laser mode (`$32=0`), fires with `M3` constant power, then
  sends `M5` and restores laser mode (`$32=1`). The 0.3 s pulse is timed with a non-blocking
  `GLib.timeout_add` so the UI stays responsive.
- **Spooler cut stayed "Sending…" and never auto-stopped after the cut finished**. The send
  worker only reported done when `write_command` fully returned, but on a printer-class
  cutter `EndDocPrinter` can block long after the bytes are sent (such cutters never report
  job completion to the spooler), so `on_done` never fired and the UI stayed locked. The cut
  is now treated as finished (UI unlocks, Stop reverts to Start) the moment **all bytes have
  been handed to the spooler** - fire-and-forget, like the reference - via a new `on_written`
  callback; `EndDocPrinter` still runs but no longer gates the UI.
- **Vinyl "Set Origin"** now works exactly like the proven reference: a **software** work
  origin, not a machine command (the reference's `setOriginHere` sends nothing - `IN;` does
  not re-origin the LH721). The head's current position becomes work (0,0), and that offset
  is added to every absolute coordinate emitted afterwards - jog, Home, Frame, Test Cut and
  the streamed cut (`_v_apply_origin_hpgl`) - so the cut starts where the head was jogged.
  A head-position **readout** was added to the Jog frame; it resets to `X 0.0 Y 0.0` on Set
  Origin, giving visible confirmation (the head itself does not move on click). The work
  origin resets to (0,0) on each connect.
- **Vinyl JOG did not move the head / confused X and Y**. Two problems, both fixed to match
  the proven reference: (1) jog emitted a *relative* HPGL move (`VS..;PU;PR dx,dy;PA;`),
  which cutters like the LH721 ignore - now it tracks the head in software and emits an
  **absolute** `PU x,y;` (Home returns to the origin, connect parks the head with
  `IN;SP1;PU0,0;`); (2) the cutter's HPGL axes are transposed vs the physical carriage, so
  the arrow pad is pre-mapped to the HPGL delta that moves the head in each button's labelled
  direction (+Y→(-1,0), +X→(0,1), -X→(0,-1), -Y→(1,0)) instead of the literal axis.
- **Vinyl axis + Home convention corrected** (preview no longer differed from the real cut).
  Vinyl machines home at **Top-Left** with the pen carriage **X = paper width** and the feed
  **Y = paper height** - the profile Home was Top-Right, and the cut was emitted without the
  axis transpose, so the physical output came out rotated 90° from the design/preview. Now:
  Vinyl Home = **Top-Left**, and the HPGL cut/frame **always transpose** `(x,y)→(y,x)` to
  compensate the cutter's crossed axes (matching the reference `designToMachineMm`), so the
  physical result matches the preview. The old "Swap X/Y axes" toggle is removed (the
  transpose is now a fixed property of the Vinyl profile). GRBL output is unchanged.
- **Spooler cutter: commands buffered / did not reach the head**. The spooler does not
  forward bytes to a usbprint device until `EndDocPrinter`, so holding one print document
  open for the whole session (or never closing it) meant jog/reset just buffered and the
  head never moved. Reworked to mirror the proven reference: keep the printer **HANDLE** open
  for the session but send **one short RAW document per command** (StartDoc → Write → EndDoc),
  so each jog/reset flushes to the cutter immediately; the cut is one document wrapping the
  whole stream. (See `_SpoolJob.write_command`.)
- **"Clear Print Job" did not clear stuck jobs**. It force-restarted the spooler via
  PowerShell/`Start-Process`, which fails under Inkscape's bundled Python (pythonw, no
  console, rewritten PATH) - so nothing happened and the jobs stayed. Reworked to match the
  proven reference tool and made all-ctypes / PowerShell-free: it force-restarts the whole
  Print Spooler (`net stop spooler` → delete every spool file → `net start spooler`) elevated
  through the native UAC prompt (`ShellExecuteEx 'runas'` on `cmd.exe`), then confirms the
  service is Running again via the Service Control Manager. The unreliable API route
  (`SetJob DELETE` / `SetPrinter PURGE`) is deliberately not used - such USB cutters wedge a
  spooling job in a "deleting" state. The button **disconnects the cutter first** so no open
  handle keeps the service from stopping cleanly.

### Added
- **Stream behaviour (GRBL + Vinyl)**: clicking **Start** now locks the whole UI - every
  setting/generate/connect/jog control is disabled until the job finishes or errors, with
  only the **Stop** button live - and a centred **"Sending…"** spinner overlays the
  preview. A stream **error** stops immediately and pops an alert dialog.
- **Error Log tab** (always last, always visible): a newest-first table of every error the
  extension hits while running - **DateTime, Actor, Message** - with a **Clear Log** button
  and a keyword **filter**. The tab shows a **red badge** with the count of new errors the
  user has not yet viewed; opening the tab clears it. Generation, serial I/O and streaming
  errors are all funnelled here.
- **GRBL Serial Log actions**: GRBL control actions now log an explicit line to the Serial
  Log, including every **Jog** (direction + step) with the resulting **X/Y** work position,
  plus Home, Set Home, Frame and settings queries.
- **True GRBL position from status reports**: while connected to a GRBL machine the
  extension polls `?` at ~5 Hz and parses the `<State|MPos/WPos/WCO|…>` reports (caching
  WCO to derive work position from `MPos`). A live **Pos: X.. Y.. [State]** readout sits
  in the Jog frame, and the jog/home/set-home log lines now report the real settled X/Y
  instead of an open-loop estimate. Polling is GRBL-only (an HPGL cutter would choke on a
  stray `?`) and pauses during a stream; status reports are kept out of the Serial Log.
- **VINYL tab** (was an empty placeholder): now mirrors the **GRBL** tab layout
  (**Connect** + **Jog** side by side) but tuned for a vinyl cutter. Differences from
  GRBL: no **Serial Log**; a **Reset Machine** button (ESC.R + HPGL `IN;`) replaces
  **Grbl Setting**; **Test Cut** (a 10 mm test square using the Cut Setting) replaces
  **Test Laser**; no **Continuous** toggle; **Jog Feed** defaults to **500** (mm/s);
  **Set Origin** (HPGL `IN;`) replaces **Set Home**. Where the GRBL tab keeps its Serial
  Log, the VINYL tab shows a **Cut Setting** frame - **Speed** (mm/s, default **250**)
  and **Force** (g, default **80**), persisted in the config and mapped to HPGL `VS`/`FS`.
  Jog/Frame/Test-Cut send HPGL (`PR/PU/PD`); **Start** writes the generated `.plt` to the
  machine in one shot (HPGL has no per-line `ok` handshake). Cut controls are disabled
  until connected.
- **Per-profile connection settings**: the USB **port** and **baud** are now remembered
  separately for GRBL and Vinyl (GRBL defaults to 115200, Vinyl to 9600). Connecting on
  one profile no longer overwrites the other's saved port/baud.
- **Vinyl connection technique (flow control + paced send)**: HPGL cutters send no
  per-line `ok`, so the VINYL **Start** no longer dumps the whole `.plt` at once. The
  Connect frame gains a **Flow Control** selector - **Software (XON/XOFF)** (default),
  **Hardware (RTS/CTS)**, or **None** - applied when opening the port so the cutter can
  throttle the host (GRBL still uses none; it paces via its ACK). **Start** now streams
  the plot in chunks on a background thread (`write()` blocks while XOFF/CTS is asserted)
  with byte progress, and toggles to a red **Stop** while cutting. An **Advanced**
  expander exposes the pacing fallback - **Chunk (bytes)** and inter-chunk **Delay (ms)** -
  which matters mainly when Flow Control is None. All persisted in the config.
- **Clear Print Job** button (VINYL tab, below **Set Origin**): force-cancels and resets
  every stuck job in the OS print queue - the recovery button for a cutter-as-printer job
  that hangs forever. Cross-platform: **macOS/Linux** use CUPS (`cancel -a -x`, `lprm -`,
  then `cupsenable`/`cupsaccept` to recover a paused queue); **Windows** removes all jobs
  via PowerShell and, when run as Administrator, restarts the Print Spooler and purges its
  spool folder. Runs off the GTK thread and reports the outcome in a dialog. Unlike the
  other jog controls it stays enabled while **disconnected**, since a wedged job usually
  needs clearing exactly then.
- **Printer-class USB ports in Connect**: the port drop-down now also lists USB
  printer devices, so a cutter that enrols as a *printer* rather than a serial device
  (e.g. the **Refine LH721**) is still selectable. Scans Linux `/dev/usb/lp*`, macOS
  `tty.usb*`, and CUPS-registered USB printers (`lpstat -v`) in addition to pyserial's
  comports (deduped).
- **Grbl Setting button**: below **Connect**, a **Grbl Setting** button queries the
  controller's settings (`$$`); enabled only while connected.
- **Jog panel (GRBL tab)**: a **Jog** frame now sits beside **Connect** (renamed from
  *GRBL Connect*). Both frames are pinned to the top of the tab (each hugging its own
  height) with the **Serial Log** filling the remaining space below. It has a directional pad (**+Y / -X · Home · +X / -Y**, Home icon =
  `$H`), a vertical **Jog Feed** slider (500–5000, default 3000 mm/min), a **Step** input
  (default 10 mm) with a **Continuous** hold-to-jog toggle, and a **Set Home** button
  (`G10 L20 P1 X0 Y0`). A second row holds icon-only **Start** (green, streams the job and
  turns into a red **Stop** while running — replaces the old *Send To Machine* button),
  **Frame** (traces the generated job's bounding box with the laser off), and **Test
  Laser** (a brief 5% pulse). Jog controls are disabled until connected.
- **Engraving | Plotter output mode** (GCode tab): a highlighted two-button group
  (default **Engraving**) selects the target machine before generating.
  - *Engraving* drives the laser (`M3/M4 S…` power, `M5` off); per-layer settings are
    **Power %, Speed (mm/min), Pass**.
  - *Plotter* drives a servo pen (`M3` pen-down / `M5` pen-up with a settle dwell,
    feed = speed×60); per-layer settings are **Force (g, default 80), Speed (mm/s,
    default 250)**. Switching mode swaps the layer-table columns in place (typed
    values are preserved) and, because the mode changes the emitted code, clears any
    existing output so it must be regenerated (skipped when already empty).
- **HPGL output**: **Generate** plans the toolpaths once and emits **both** a temporary
  GRBL `.gcode` and a temporary HPGL `.plt` (units 0.025 mm, `PU/PD` with per-layer `VS`
  velocity and `FS` force). The single **Save** button writes whichever format matches the
  active machine profile.
- **Generation spinner**: unchanged centred overlay ("Generating G-code…") shown over
  the preview while the background worker runs, hidden when the toolpath appears.
- **GRBL | Vinyl Cutter machine profile** (Generate tab): a two-button group (default
  **GRBL**) placed where the Home dropdown used to be. It selects the target machine,
  which fixes both the Home corner and the export format - **GRBL** → G-code with
  Bottom-Left home; **Vinyl Cutter** → HPGL with Top-Right home. Switching profile
  re-maps the axes and clears the output. Independent of Engraving | Plotter, which still
  drives the per-layer columns.

### Added
- **Vinyl cutting over the Windows print spooler (USB printer-class cutters)**: a cutter
  that enrols as a Windows printer (e.g. the **Refine LH721** on a `USB001` port) is not a
  COM port, so pyserial can neither see nor open it. The USB Port dropdown now lists such
  printers by name (enumerated with the Win32 `EnumPrinters` API via ctypes - not
  PowerShell, which can fail or stall under Inkscape's bundled Python - filtered to `USB*`
  ports), and selecting one routes
  the whole Vinyl transport through the **print spooler** instead of pyserial: Connect,
  Jog, Home, Set Origin, Frame, Test Cut, Reset and the full **HPGL cut Send** are all
  submitted as **RAW passthrough** print jobs (`winspool.drv` via ctypes, no pywin32
  dependency), so HPGL reaches the cutter byte-for-byte. Stop aborts the in-flight job
  mid-write; **Clear Print Job** still flushes a wedged job. Baud/flow-control are ignored
  in spooler mode (the spooler paces the link). Serial (COM) cutters are unaffected.

### Fixed
- **Generation spinner never appeared**: the centred "Generating…" overlay had
  `no-show-all` set at build (so the top-level `show_all()` would skip it), which also
  blocked the direct `show_all()` used to reveal it while generating. It is now cleared
  before showing, so the spinner actually spins during generation.

### Changed
- **"Clear Print Job" now deletes the cutter's queued jobs** directly (spooler API), and
  only force-restarts the whole Print Spooler as a fallback when a job is wedged too hard to
  delete. It disconnects the cutter first. (Details under **Fixed**.)
- **"GCode" tab renamed to "Generate"**; **"Machine" tab renamed to "GRBL"**.
- **Generate button shows progress**: its label switches to **"Generating…"** (and the
  centred preview spinner runs) while the background worker generates, then reverts.
- **Preview grid labels decluttered**: only every other gridline is labelled (starting at
  0), on both axes, with the value and `mm` unit.
- **Single action row** on the Generate tab, left to right: **GRBL | Vinyl Cutter**,
  **Engraving | Plotter**, **Generate**, **Clear**, **Save**. Generate takes the
  remaining width; Clear and Save stay compact.
- **Button colours standardised**: the GRBL | Vinyl Cutter group highlights **green**
  (matching its status pill), Engraving | Plotter stays **blue**, and **Generate** is now
  **orange**.
- **Layer-list header restyled**: black background with bold white column labels (was the
  dimmed default).
- **Per-layer show/hide is now an eye-icon toggle** (was a checkbox): open eye = layer
  shown/included, slashed eye = hidden/excluded. Click to toggle. Uses the theme's
  reveal/conceal icons, falling back to a text glyph if unavailable.
- **Tab highlight restyled**: the active notebook tab is now marked with a rounded grey
  border around its label instead of the default blue underline.
- **Resize handle affordance**: the divider between the preview (left) and the notebook
  (right) is a slim handle showing three vertical dots, so it reads as draggable.
- **Serial Log recoloured** with a vibrant per-token palette: the timestamp is violet
  italic (was flat grey), and every actor token is now bold with its own distinct hue —
  GRBL cyan, TX blue, RX teal (was grey), OK green, INFO indigo (was near-black), WARN
  amber, ERROR red, ALARM magenta (now distinct from ERROR). Only the short
  `[datetime]`/`[ACTOR]` tokens are tinted, so they stay legible on light and dark themes.

### Added (WIP)
- **Opens on the Generate tab**: the dialog always starts with the Generate tab active,
  regardless of the machine profile.
- **VINYL tab** (empty placeholder) added after the GRBL tab. Only the machine tab
  matching the current profile is **shown**: **GRBL** profile → GRBL tab, **Vinyl Cutter**
  profile → VINYL tab; the other is hidden. Switching profile while a machine tab is open
  falls back to the Generate tab.
- **Home position is now derived from the machine profile** instead of a dropdown, per
  `docs/knowledge/basic/machine-home-position-coordinate-system.md`: GRBL homes
  **Bottom-Left** (X→right, Y→up), Vinyl Cutter homes **Top-Right** (X→left, Y→down).
  Selecting a profile re-maps the axes immediately (the preview updates) and clears any
  generated output so it is regenerated from scratch. Coordinate mapping is done purely
  by `MachineSpace.to_machine` (Home-corner mirror/flip) - no extra rotation/transpose.
- **Work area info moved into the Preview**: the document-size readout is now a compact
  overlay pinned to the top-left of the preview canvas instead of a row in the Machine
  tab's GRBL Connect frame.
- **Status pills in the Preview** (top-right, same row as Work area): a green pill shows
  the machine profile (**GRBL** / **Vinyl**) and a blue pill shows the output mode
  (**Engraving** / **Plotter**); both update live when their button group changes.
- **Borderless preview**: the "Preview" frame label and border are gone; the canvas now
  fills the whole left pane. The bottom hint (Scroll/Drag/Right-drag/Double-click) is
  centred.
- **Default export filenames**: Save defaults to `printable.gcode` (GRBL) or
  `printable.plt` (Vinyl Cutter).

### Removed
- **Home position dropdown**: removed; Home now follows the machine profile (see Changed).
- **Separate GCODE / HPGL save buttons merged into one Save** (floppy-disk icon): it saves
  the active profile's format - G-code for GRBL, HPGL for Vinyl Cutter. Stays disabled
  until that output has been generated (and while streaming).
- **`GenOptions.swap_xy` / X-Y transpose**: removed. The earlier axis-transpose workaround
  is superseded by proper Home-corner transforms driven by the machine profile.
- **Machine Name field**: removed from the Machine tab's GRBL Connect frame along with
  all related logic (the `machine_name` config field, `GenOptions.machine_name`, and its
  use in the G-code header comment and the Send confirmation dialog). The send prompt now
  names the serial port instead.
- **GCode tab buttons**: *Generate GCode* → **Generate**; *Clear* is now an icon-only
  button.
- **G-code pipeline split** (`gcode.py`): `generate_program` is refactored into a shared
  `plan_toolpaths` (fills + nearest-neighbour ordering) feeding independent `emit_grbl`
  (mode-aware) and `emit_hpgl` back-ends, so the preview, GRBL and HPGL outputs describe
  exactly the same motion. `generate_program` is retained as a thin wrapper.

### Previously (algorithm standardisation)

Standardised the G-code generation pipeline against the machine-agnostic algorithm
knowledge base (`docs/knowledge/code-generate-algorithm`). Behaviour-preserving for the
UI rules (no direction arrows on the toolpath; return-to-Home travel stays dashed orange).
- **Robust distance-field offset engine** (`fills/offset_dt.py`, new): Contour and Spiral now
  offset via a chamfer distance transform + marching-squares iso-lines instead of a per-vertex
  miter inset. This handles **holes, concavity and multiple disjoint regions** — the cases where
  the old inset self-intersected or gave up and silently fell back to Zigzag. Pure Python (NumPy
  optional), topology-agnostic, ~15 ms on typical shapes.
- **Spiral connects across any topology** (doc 03 §3.5): offset rings are joined into the fewest
  continuous strokes possible (one per nested region) by a nearest-ring walk with per-ring
  re-rooting and a flush-on-far-jump rule. Treating the laser as a pen plotter, this **minimises
  tool lifts** — e.g. a donut fill drops from ~52 lifts (Contour) to ~2 (Spiral).
- **Auto fill re-tuned to be lift-optimal** (`fills.auto_select`): because Spiral is now robust,
  Auto prefers it for essentially every real filled region (round blobs, concave shapes, holed
  shapes, text), keeping **Zigzag** for rectangles/straight bars (boustrophedon along the long
  axis) and **Contour** for hairline regions. Previously Auto routed holed/concave shapes to
  Contour (many lifts) because it assumed a convex-only Spiral, so most non-trivial fills
  collapsed to a Zigzag/hatch look; they now use the strategy that actually fits the shape.
- **Shade-by-colour fill density** (doc 03 §2): fill line spacing now scales with the fill
  colour's perceptual luminance — a **darker** fill engraves **denser**, a **lighter** fill
  **sparser** (up to `SHADE_MAX_MULT` = 5× the base spacing). `fill_spacing` is the spacing for a
  fully-dark fill. Toggled by a **"Shade by color" checkbox** in the GCode tab (persisted as
  `config.shade_density`, default on); colour is parsed via inkex with a hex/rgb/named fallback.
- **Toolpath ordering — loop re-rooting** (knowledge doc 07 §3): closed rings are now entered
  at the vertex nearest the tool instead of always at their stored first point; open chains
  still reverse when their tail is nearer. Same burned length, markedly less rapid travel.
- **Dense-fill ordering cutoff** (doc 11 §5): above 2000 chains, nearest-neighbour ordering
  falls back to an O(n log n) boustrophedon sweep so large hatch jobs no longer risk freezing
  the dialog.
- **Zigzag hole-aware connectors** (doc 03 §3.3): a boustrophedon connector between scan lines
  is kept only when it stays inside the region (nudged midpoint inside-test); connectors that
  would bridge a hole now lift instead of burning across it.
- **Spiral ring re-rooting** (doc 03 §3.5): each inner offset ring is entered at the vertex
  nearest the previous ring's end, shortening the burned step between rings.

### Fixed
- Flattened polylines are now scrubbed of non-finite (NaN/inf) coordinates and consecutive
  duplicate points (doc 12 §3.1/§4), so a malformed input can never emit `G1 Xnan` or a
  zero-length segment.

### Tests
- `tests/test_core.py` now has **38** tests (added loop re-rooting, open-chain reversal,
  dense-fill fallback, hole-bridging rejection, polyline hygiene, Spiral-vs-Contour lift count,
  Auto routing for holed/concave shapes, colour luminance, and shade-by-colour density).

## [0.1.0] - 2026-07-01

First feature-complete build of the GRBL laser G-code extension for Inkscape 1.4.2.

### Added
- **Extension scaffold**: `jasgrbl.inx` (menu **Extensions ▸ jas GRBL ▸ GCode Generate**)
  and `jasgrbl.py` — an `inkex` effect extension that launches a custom **GTK 3** window.
- **Dialog** (≥1000 px): a `Gtk.Paned` split — **Preview (≈60%)** on the left, a
  **Notebook (≈40%)** on the right with two tabs: **GCode** and **Machine**.
- **GCode tab**: fill controls (top), per-layer settings table (middle, fills height),
  action buttons (bottom): **Generate GCode**, **Clear**, **Export GCode**.
- **Machine tab**: **GRBL Connect** frame (Machine Name, USB Port + refresh, Baud,
  Width, Height, **Margin**, Home position, **Connect**, **Send To Machine**) and the
  **Serial Log** (colorized `[time] [actor] [message]`) with a command Send box.
- **Per-layer settings**: enable, Power %, Speed, Pass count, Stroke Text — persisted by
  layer id. Numeric inputs are compact plain text entries (no +/- steppers).
- **Config persistence** (JSON in the user config dir), including Margin and per-layer.
- **Theme follows Inkscape**: reads `preferences.xml` (`gtkTheme`, `preferDarkTheme`) and
  applies it via GtkSettings, so the dialog is dark when Inkscape is dark.
- **In-dialog toolpath preview** (Cairo, theme-aware):
  - board view with the **Home corner at its physical corner** and **X (red) / Y (green)
    axes pointing into the board**, each with an arrowhead and axis name;
  - grid with a **value + unit label at every gridline**; orange Home origin dot;
  - blue toolpath: burn solid, travel dashed, **return-to-Home dashed orange**;
  - **zoom (scroll, toward cursor) / pan (drag) / rotate (right-drag) / reset (double-click)**;
  - a centered **spinner** while generation runs on a background thread.
- **G-code generation** (GRBL laser): `G21/G90/G17`, `M4` dynamic (or `M3`) laser, `S`
  power scaling from `$30`, multi-pass, greedy nearest-Home travel ordering.
- **True curves**: curved toolpaths are emitted as real **`G2`/`G3` arcs** (`arcs.py`,
  greedy line/arc fit), not chains of tiny `G1` segments.
- **Smart fills**: **Auto** (default), Hatch, Cross-Hatch, Zigzag, Contour, Spiral,
  Hilbert, Peano, Voronoi (with documented fallbacks — never crashes).
- **Auto fill** (`fills.auto_select`), adapted from the Plotter "Vinyl Draw"
  `FillEngine::chooseAuto`: classifies each region by net area, min-area oriented bbox
  (principal axis), elongation, rectangularity, convexity, compactness and holes, then
  picks the time-optimal connected fill (Zigzag / Spiral / Contour) with the scan angle
  aligned to the long axis.
- **Single-stroke text** (Stroke Text per layer): geometric **medial axis** via Delaunay
  triangulation + **Chordal Axis Transform** (`centerline.py`), so straight stems stay
  straight (no raster wobble).
- **Text handling**: text is auto-converted to paths at Generate (headless Inkscape on a
  **copy**, editable text preserved). Outline vs fill follows the glyph fill — white/none
  fill → outline, any visible (non-white) fill → hatch.
- **Home-relative placement**: the design is moved next to the Home corner — its nearest
  corner lands at `(margin, margin)` (`MachineSpace.solve_offset`) — for both preview and
  G-code, regardless of where it sat in the document.
- **Output**: Export `.gcode` to a file; **Send To Machine** streams the temp G-code over
  serial with a confirmation, and the button doubles as **Stop** while running.
- **Serial**: connect/disconnect, colorized log, manual commands, simple send/`ok`
  streaming (pyserial; gracefully disabled if not installed).
- **Tooling**: `tools/dev_install.(ps1|sh)`, `tools/package.py` (release zip), and a
  pure-Python test suite (`tests/test_core.py`).

### Changed
- Default fill strategy is **Auto** (chooses the optimal per shape).
- **Generation output is the preview**, not a log panel; generation no longer writes to
  the Serial Log (which now shows only machine/serial activity). Fatal errors pop a dialog.
- Generation runs on a **background thread** with a spinner (the dialog no longer freezes).
- Changing **Home Position** immediately repositions the preview axes and clears the
  (now-stale) preview toolpath + temp G-code.

### Removed
- The **SVG "simulation layer"**. The toolpath preview is now drawn entirely inside the
  dialog (Cairo), so the extension is **read-only on the document**.

### Fixed
- Curves exported as faceted straight segments → real `G2`/`G3` arcs + finer flattening.
- Stroke-Text centerline wobble from raster thinning → clean Delaunay/CAT medial axis.
- Filled text losing its fill after object-to-path (absent fill now treated as the SVG
  default black); white/none-fill text is engraved as an on-path outline.
- Contour/Spiral inset collapsing after 1–2 rings on real polygons (bevel sharp vertices +
  per-pass cleanup) → circles/ellipses now fill fully.
- Spurious straight chords across curves in the preview (removed the downsampled arrow
  overlay; arrows dropped entirely).
- Dialog showing as a big blank/"not responding" window during generation (now threaded).
- Dialog not matching Inkscape's dark theme.

[0.1.0]: initial tagged feature set.
