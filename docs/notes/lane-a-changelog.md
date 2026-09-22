# Lane A: CHANGELOG.md material

Drop-in entries for `CHANGELOG.md`. Version numbers are the orchestrator's to assign; the
text below assumes crabd `0.32.0` and widget `0.30.0`.

---

### Added

- **Temperatures are back, from HWiNFO.** crabd reads this machine's sensors out of
  HWiNFO's shared memory and serves a curated, capped list of 24 in `host.sensors`: CPU
  die and package temperatures, GPU temperatures, VRM, each drive, motherboard and
  chipset, CPU package power, and every fan and pump. The panel's hardware row shows the
  CPU temperature with the sensor's own name wherever no iCUE Sensors bridge owns that
  cell, which is every standalone panel.
- **`host.sensorsSource`**, the provenance beside them: which provider answered, HWiNFO's
  own poll time, how old it is, and whether that makes it stale. The free build of HWiNFO
  stops publishing about twelve hours after launch and leaves its mapping in place with
  the last poll time frozen in it, so "the mapping is there" is not freshness. A stale
  source dims the readings and says why.
- **`host.gpu`**, the NVIDIA card from `nvidia-smi`: name, driver, temperature,
  utilisation, VRAM used and total, power and its limit, and the clock. The row's GPU cell
  shows the temperature and the utilisation; the host sheet adds the VRAM and the power.
  A machine with no NVIDIA card gets `available: false` and a note, never a row of
  em-dashes.
- **`host.load`**, what the whole machine is doing: disk read and write throughput,
  network in and out, committed memory, and the busiest process with its share of the
  machine. The host sheet gains a load line and a 10-minute disk-throughput chart beside a
  10-minute GPU utilisation chart.
- **`setup/Register-HwinfoRelaunch.ps1`**, which registers the `SideCrab-hwinfo` scheduled
  task: at logon and daily at 04:00 it stops HWiNFO by its path-filtered process id and
  starts it again, so the free build's twelve-hour limit never strands the panel on
  yesterday's temperatures. `-Remove` unregisters it, `-WhatIf` is supported, and the Pro
  licence removes the need for it entirely.
- **A `sensors` row in `Test-SideCrab.ps1`** and a one-line `sensors:` summary in
  `Install-SideCrab.ps1 -Status`. Both treat an absent source as a state rather than a
  fault: no HWiNFO, a closed Sensors window, no NVIDIA card and an older crabd all pass
  with a note that says which. The row fails only on a block that is present and
  malformed, which is the shape that would let the panel show an old reading as a current
  one.

### Changed

- `host` is now served whenever any of its sources answered, not only when the kernel
  counters could be read. A machine whose `GetSystemTimes` and `GlobalMemoryStatusEx` both
  fail can still have a readable card, and dropping a measurement that was taken because
  an unrelated counter was not is a second failure invented from the first. Every member
  has been presence-detected since v0.22.0, so nothing that read the block correctly
  changes.
- The host sheet no longer says "no hardware sensor reading" while the companion is
  serving two dozen of them. That line was about the iCUE bridge, which is no longer the
  only source of a temperature.
