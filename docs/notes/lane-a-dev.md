# Lane A: widget/DEV.md material

Drop-in section for `widget/DEV.md`, written in that file's style. Version number is the
orchestrator's to assign; the text assumes widget `0.30.0`.

---

## v0.30.0 — temperatures without iCUE: HWiNFO, the card, and what the row could not hold

The standalone panel had no temperatures at all (v0.29.0, "there is no sensor source in
this host yet"). crabd now reads HWiNFO's shared memory, `nvidia-smi` and a PDH load
sampler, and serves them inside `host`. The row and the host sheet render them.

### Which source owns a cell

`sensorApi` decides, and it is deliberately **not** `isStandalone()`. The question this
answers is *does something already own these cells*, and an iCUE install whose Sensors
plugin is missing is the same answer as a dev browser. Where a bridge is bound, the
operator has *picked* the two sensors those cells are about and a companion-side list must
not overwrite a choice; where there is none, these readings are the only temperatures the
panel has. Verified both ways off-glass: `?mock=rework` paints the HWiNFO cells,
`?mock=rework&sensors=63,50` paints the bridge's and the lane A cells vanish.

The bridge is untouched. **The bridge-owned row is byte-identical to `d886232`**: 592.1 px
in its forced worst case and 500.6 px with ordinary names, measured on both trees through
the same probe.

### The width budget, which is again where the work was

The row is one line and the Limits zone gives it **561.9 px at 2560x720** (the v0.21.0
figure, re-measured on HEAD this session: unchanged, which is the instrument check). Cells
do not shrink, so the widest the row can ever paint is fixed text plus the capped name.

| what | measured width |
|---|---|
| CPU cell, capped name, `100°` and `100%` | 228.9 px |
| GPU cell, `100°` and `100%` | 145.0 px |
| MEM cell, `100%` | 95.8 px |
| inter-cell gap | 15.8 px |
| **worst case, three cells** | **501.4 px of 560, 58.6 px spare, chevron clear 53.8** |
| one more cell (VRM, no name) | 93.1 px plus a gap |
| **worst case, four cells** | **610.3 px: 50.3 px past the zone edge** |

So the row carries the three cells it has always carried and the extra readings go to the
host sheet, which is one tap away and has width this line does not. The VRM, the drive,
the board, the package power and all four fans are there.

Two changes bought the three cells their fit:

- **The CPU name's cap is 11 vmin (79.2 px) while this block owns the row**, against the
  bridge row's 13.5. The two rows carry different labels: iCUE hands back an
  operator-chosen sensor name (`CPU Package`, 87.0 px), while HWiNFO's is
  `CPU (Tctl/Tdie)`, which the block shortens to `Tctl/Tdie` — 9 characters, inside 79.2
  px with room. Scoped to `.sensors.host-sensors`, so a bridge-owned row keeps its own cap.
- **The unit letter is spent only when it changes the meaning.** Celsius is the scale this
  row's 80/90 thresholds are in and the scale it colours against, so `60°` says everything
  `60°C` does and costs 11.0 px less per cell. A reading in any other unit keeps its
  letter, because there the letter is the whole difference between 140°F and a machine on
  fire.

`shortHostSensorName` is a second shortener and it has to be: `shortSensorName` splits on
`/` to take the last segment of an iCUE device path, and `CPU (Tctl/Tdie)` comes out of it
as `Tdie)`.

### The host sheet

The sheet gains, in order: the GPU line (VRAM used and total, power and its limit, clock),
the load line (disk and net throughput, commit, `top: <name> <pct>%`), the sensor
provenance (how many readings, how old, and the note when there is one), every curated
temperature the row had no width for, the fans, and two 10-minute charts.

**Two charts, not four, and the count is a measurement.** The visible column is 541 px at
2560x720 and a chart costs 145 px of it; the shipped CPU and MEM charts plus their notes
already spend 328. Four new charts paint 1078 px and put half the view below a fold whose
scrolling this panel's own touch handling is not proven to reach (v0.23.0). Two charts
paint **795 px, 254 below the fold**, with every new *number* above it. The two series that
earn a chart are the ones with a shape: the card's utilisation, which swings, and disk
throughput, which spikes. Commit and network keep their numbers in the load line.

A rate chart states its scale (`0-3.1 MB/s`) because a throughput has no natural 100%.
Without the statement a flat idle line and a flat saturated line look identical. The
percentage charts keep the fixed 0-100% the v0.22.0 charts use.

Names in the sheet are **disambiguated rather than suppressed**, which is v0.24.0's rule
turned around for a surface that can afford it. Two cells showing one name name neither;
in a list the answer is to say which device each came from. The cause is measured: this
machine reports `GPU Temperature` from both the discrete card and the integrated one, and
`Drive Temperature` three times from one SSD.

### Staleness, from three clocks

HWiNFO, `nvidia-smi` and the load sampler each carry their own freshness, so the row can
dim one cell and not another. Proved on `?mock=hot`, whose `sensorsSource.stale` is true
while its `gpu.sampledAt` is fresh: the CPU value carries `.stale` at opacity 0.38 **and
keeps its red threshold colour** (the v0.18.0 rule that opacity and colour must not fight
over one property), while the GPU cell stays at opacity 1. Driving `gpu.sampledAt` two
minutes into the past through the shipping `renderHost` dims the GPU temperature and its
utilisation together, and restoring it brings both back.

### What this block wrote, this block clears

A crabd that stops serving `host.sensors` (a downgrade, or HWiNFO closing) used to leave
`Tctl/Tdie` sitting beside the load percentage with no reading behind it: a label for a
temperature that is no longer on the glass. That is v0.21.0's `hideSensor` lesson one
source along. The clear is guarded on having painted, so it can never blank a cell the
bridge owns. Driven end to end: `CPU 60° Tctl/Tdie 34%` becomes `CPU 34%` on the
downgrade, comes back whole on restore, and the row empties on a `host` of null.

### Fixtures and flags

- `?mock=rework` carries the full block: 24 curated sensors, a fresh `sensorsSource`, a
  live `gpu` and a full `load` with a `topProcess`. The row reads
  `CPU 60° Tctl/Tdie 34%   GPU 48° 4%   MEM 58%`.
- `?mock=hot` carries the **stale** case: `sensorsSource.stale` true with the relaunch
  note and a 12-hour age, hot temperatures (CPU 91°, GPU 88°), a card at 99% and 243 W, a
  90 MB/s disk, `commitPct` 91.4, `topProcess` `blender.exe` at 71.6%, and `netRxBps` and
  `netTxBps` null so the load line's net phrase drops out honestly. Its `cpuPct` and
  `memPct` stay null, so the row's only content is the new members, which is the strongest
  off-glass test of them.
- `&host=1` opens the host sheet, unchanged. Against `?mock=rework` it shows the
  "collecting" state for all four charts; seed `hostRing` and `laneARing` from the console
  to shoot the plots, the same technique the v0.22.0 record used.
- The drive's serial number is removed from both fixtures. A fixture is a committed file.

### Verified

`node --check`, `test_ordering.js` 140/140, strict-XML parse of `index.html`, JSON parse of
every fixture plus manifest and translation, `icuewidget validate widget` clean (the known
`icueEvents` warning only).

Headless Edge at 2560x720, DPR 1, a fresh `--user-data-dir` per run, DOM read over CDP:

- **Page overflow 0x0 on every case measured**: `rework`, `hot`, `normal`, the
  bridge-owned row, and both sheet states.
- **The row's worst case, forced through the shipping spans**: 501.4 px of 560, zero
  overflow, 53.8 px of chevron clearance.
- **The instrument check**: the zone's content width reproduced the v0.21.0 record exactly
  (561.9 px) before a line was written.
- Screenshots in `docs/notes/`: `lane-a-row-rework.png`, `lane-a-row-hot-stale.png`,
  `lane-a-row-bridge-wins.png`, `lane-a-sheet-rework.png`, `lane-a-sheet-hot-stale.png`,
  `lane-a-sheet-collecting.png`.

### The live mapping this was built against

Measured 2026-09-21 on the reference machine, HWiNFO64 v8.52-6060, Sensors window open,
elevated:

- Header: signature `HWiS`, version 2, revision 1, **packed** (`poll_time` at offset 12,
  not the 16 an 8-byte-aligning compiler would give). Sensor section at 48, element **392
  bytes**, **23** sensors. Reading section at 9064, element **460 bytes**, **524**
  readings. The four doubles sit at **+284** inside a reading element, the packed position.
  A `dwPollingPeriod` of 2000 ms follows the documented header fields at offset 44.
- Both element sizes are **larger than the documented v1 minimums** (264 and 316): the
  known fields are a prefix and the remainder is newer trailing fields. The parser strides
  by the header's own `dwSizeOf*` and never by a computed `sizeof`, which is what makes an
  unknown tail harmless.
- Both the packed and the 8-byte-aligned layouts are parsed; which one is live is decided
  per mapping by validating the header's offsets against the mapped region and by scoring
  the two candidate double offsets on `min <= value <= max`. Reading the doubles four bytes
  early turns every value into a denormal, which is exactly the kind of wrong number that
  looks like a reading.
- Five readings as measured: `CPU (Tctl/Tdie)` 62.1 °C, `GPU Temperature` (dGPU) 47.6 °C,
  `VRM` 47.0 °C, `AIO Pump` 3435 RPM, `CPU Package Power` 98.2 W.
- 37 of the 524 readings pass the rank test; the round-robin cap serves 24 of them as
  4 CPU, 4 GPU, 4 VRM, 3 drive, 3 board, 2 power, 4 fan.
- `nvidia-smi` on the same machine:
  `NVIDIA GeForce RTX 5070, 610.74, 48, 4 %, 4000 MiB, 12227 MiB, 12.26 W, 250.00 W, 382 MHz`.
