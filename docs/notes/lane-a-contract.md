# Lane A: the additive contract text for `host`

Drop-in text for `docs/STATE-CONTRACT.md`, written in that file's house style. It is
additive: `schema` stays **5**, every member is presence-detected, and an older widget
ignores all of it.

---

## v0.32.0 (2026-09-21 — ADDITIVE: `host.sensors`, `host.sensorsSource`, `host.gpu`, `host.load`; schema stays 5)

Four new members inside the existing top-level `host` key, from three samplers that each
own a thread and a 5 s cadence. No existing member moves and none changes meaning, so
`schema` stays **5** and no widget import is needed.

**`host` may now exist without its v0.22.0 members.** Until this release the block was
present only when `GetSystemTimes` or `GlobalMemoryStatusEx` could be read. A machine
whose kernel counters both fail can still have a readable GPU or a readable HWiNFO
mapping, so the block is now served whenever *any* of its sources answered. Readers that
already test each member for presence (the contract has required this since v0.22.0) need
no change; a reader that assumed `host` implies `cpuPct` does.

### 1. `host.sensors` — a curated list of this machine's hardware sensors

```jsonc
"sensors": [                       // present only when the HWiNFO reader is wired
  { "name":   "CPU (Tctl/Tdie)",   // the user label, falling back to the original
    "device": "CPU [#0]: AMD Ryzen 9 9950X3D2: Enhanced",   // the sensor it belongs to
    "kind":   "temp",              // temp | fan | volt | power | clock | usage | other
    "unit":   "°C",                // as HWiNFO spells it
    "value":  62.1 }               // number, or null when the reading is not finite
]
```

**Read from HWiNFO's shared memory, stdlib only** (`OpenFileMappingW` plus `ctypes`, never
`mmap`'s `tagname`, which *creates* the section when none exists). HWiNFO publishes
`Global\HWiNFO_SENS_SM2` while its Sensors window is open; crabd opens it read-only, takes
one copy, and parses that.

**The list is CURATED and CAPPED at 24, and the cap is a policy rather than a slice.** On
the reference machine 37 of 524 readings pass the rank test, of which 10 are CPU
temperatures and 10 are GPU temperatures. Filling the list in rank order and taking the
first 24 keeps those twenty and drops every fan, every drive and the package power, which
is a cap deleting whole *kinds*. So the list is filled one reading per rank per pass: every
kind that exists gets a place, and only depth within the largest kinds is trimmed.

What is served, in rank order: CPU temperatures (`Tctl`, `Tdie`, CCD, die, package), GPU
temperatures, VRM, drive, motherboard and chipset, CPU package power, and every fan and
pump. The ranks are matched against the reading's **label**, never against the sensor name:
the per-core temperatures sit under a sensor called `CPU [#0]: …`, so a device-side match
sweeps all sixteen of them in as CPU temperatures. Specificity decides the walk order, not
rank order, because this board labels its VRM probes `CPU VDDCR_VDD VRM (SVI3 TFN)`.

**Four exclusions, each measured rather than imagined.** `Accumulated CPU Temperature`
reads 194,117,396 °C and `Accumulated CPU Power` 135,046,525 W: counters wearing a
temperature's name and unit. `Temp9` is an unpopulated board header, a number with no
subject. The sixteen per-core temperatures, the two L3 cache temperatures and the eight
`GPU Memory A0..C1` rows are detail a one-line row has no width for. Under the name gates
sit range gates, applied only when the unit says which scale it is: a temperature outside
-50..150 °C (or its Fahrenheit equivalent) and a power above 5000 W are not readings.

**A fan at 0 RPM is a measured zero, not an absence.** Both card fans read 0 at idle, which
is fan-stop working. `value: 0` and `value: null` are different claims and both are served.

**Values carry sensor precision, not float precision**: 1 dp for temperatures and power,
3 dp for volts, whole numbers for RPM and MHz. The mapping hands back seventeen digits of a
die temperature the silicon reports to about half a degree.

Strings are ANSI and NUL-terminated, decoded as latin-1: `°C` arrives as one `0xB0` byte,
which utf-8 refuses and utf-8-with-replacement turns into a question mark.

### 2. `host.sensorsSource` — where those readings came from, and how old they are

```jsonc
"sensorsSource": {
  "provider":  "hwinfo",
  "pollTime":  "2026-09-21T23:14:49Z",   // HWiNFO's own poll time, or null
  "ageSec":    1.9,                      // seconds since it, 1 dp, or null
  "stale":     false,                    // ageSec > 30
  "available": true,
  "note":      null                      // a string whenever there is something to say
}
```

**`stale` is derived from the poll time, never from whether the read succeeded, and that
is the whole point of this member.** The free build of HWiNFO stops publishing about twelve
hours after launch and leaves the mapping in place with its last poll time frozen in it. A
reader that only checked for the mapping would serve half-day-old temperatures as current,
forever. The note then reads *"HWiNFO stopped publishing (free build 12-hour limit):
relaunch HWiNFO"*, and `sensors` still carries the readings: they were real when they were
taken, and the panel dims them. Dropping them would turn *old* into *absent*.

Three states, three answers:

| What happened | What is served |
|---|---|
| The mapping is there and its poll time is recent | `available: true`, `stale: false`, `note: null` |
| The mapping is there and its poll time is older than 30 s | `available: true`, `stale: true`, the relaunch note, and the readings |
| No mapping at all | `available: false`, `stale: false`, `pollTime` and `ageSec` null, `sensors: []`, note *"HWiNFO not running, its Sensors window closed, or Shared Memory Support off"* |
| A mapping whose header does not parse | `available: false`, note `"unreadable"` |

The absent note names all three causes because the reader cannot tell them apart:
`OpenFileMappingW` answers `ERROR_FILE_NOT_FOUND` for every one. The Sensors-window cause
is the one an operator is least likely to guess, and it is real: with HWiNFO's main window
up and no Sensors window there is no mapping at all.

A poll time in the future is a clock that moved, not a reading from ahead. `ageSec` clamps
at 0 rather than going negative.

### 3. `host.gpu` — the NVIDIA card, from `nvidia-smi`

```jsonc
"gpu": {
  "name": "NVIDIA GeForce RTX 5070", "driver": "610.74",
  "tempC": 48.0, "utilPct": 4.0,
  "memUsedMB": 4000.0, "memTotalMB": 12227.0,
  "powerW": 12.26, "powerLimitW": 250.0, "clockMHz": 382.0,
  "available": true, "note": null,
  "sampledAt": "2026-09-21T23:19:27Z"
}
```

**The block is served with `available: false` rather than omitted** once the sampler exists.
"This machine has no NVIDIA card" is an answer; a missing key is not, and a reader would
have to tell it apart from an older crabd. Every member is null in that state, and `note`
says which failure it was: *"nvidia-smi not found - no NVIDIA driver on this machine"*,
*"nvidia-smi timed out"*, or *"nvidia-smi returned no readable row"*.

**`sampledAt` is this block's own freshness and it is not optional.** The sampler runs on a
5 s cadence against a 2 s document, so `generatedAt` would date the figure up to two polls
young. Every value that can be stale carries its own clock.

**Nine columns, and the count is exact.** The answer comes back with
`--format=csv,noheader`, so the column order *is* the contract. Fewer than nine means a
column vanished; MORE means one was split, which is what a comma decimal separator does,
and both shift every field after the break. A shifted row is refused rather than served as
a plausible wrong reading. `[N/A]` in one column nulls that field and leaves the other
eight standing.

### 4. `host.load` — what the whole machine is doing

```jsonc
"load": {
  "diskReadBps": 1267326, "diskWriteBps": 410492,   // whole machine, whole numbers
  "netRxBps": 36919, "netTxBps": 125770,
  "commitPct": 45.5,
  "topProcess": { "name": "pwsh.exe", "pid": 8124, "cpuPct": 3.1 },
  "sampledAt": "2026-09-21T23:08:56Z"
}
```

Disk and network come from PDH counters held in one long-lived query
(`\PhysicalDisk(_Total)\Disk Read Bytes/sec` and its write twin,
`\Network Interface(*)\Bytes Received/sec` and its sent twin). Commit comes from
`GlobalMemoryStatusEx`, the same call the v0.22.0 sampler already makes. The busiest
process comes from a Toolhelp snapshot plus `GetProcessTimes` deltas.

**The first sample of every delta is null**, exactly as `cpuPct` has been since v0.22.0.
PDH answers the first collect of a rate counter with `PDH_CSTATUS_INVALID_DATA`, and that
becomes a null rather than a zero. No baseline collect is taken at open, because two
collects back to back produce a rate over a sub-millisecond window and serving it would be
the trap `GetSystemTimes` already documents.

**Every member fails independently.** A PDH query that cannot open does not take the commit
figure with it; a snapshot that cannot be taken does not blank the throughput.

**`topProcess` is null until there are two snapshots**, and it is keyed on
(pid, creation time) because Windows reuses pids: without the creation time a short-lived
process hands its baseline to an unrelated one and produces a fabricated spike. `cpuPct` is
a percentage of the whole machine, so one fully busy core on a 32-thread host is 3.1%.
Processes that refuse `PROCESS_QUERY_LIMITED_INFORMATION` are skipped rather than counted
as zero: a protected process is one this reader cannot measure, and 0% would be a claim
about it. A process that started since the last snapshot has no baseline and is skipped for
one interval.

**`netRxBps` and `netTxBps` are a filtered sum, and the filter is a heuristic.** PDH's
`Network Interface` set lists every pseudo-interface beside the real adapters, including a
virtual switch that carries the *same* bytes as the NIC underneath it. Instances whose
names contain `loopback`, `isatap`, `teredo`, `pseudo`, `tunnel`, `vethernet`, `virtual`,
`miniport`, `filter`, `qos`, `bluetooth`, `vpn` or `tap-` are excluded. On a host with an
unusually named virtual adapter the sum can still double-count, so this figure is a
throughput indicator and not an accounting number. Null when PDH could not answer at all:
an empty sum would be a measured zero.

### Hard rules this keeps

- Unknown is `null` or `available: false`, never `0`.
- Every value that can be stale carries its own freshness (`sensorsSource.ageSec`,
  `gpu.sampledAt`, `load.sampledAt`).
- No sampler runs in the request path or in `build()`. A wedged sampler shows up as a
  growing `ageSec`, not as a feed that stopped.
- Nothing here can raise into `build()`. A malformed mapping is `available: false` with
  `note: "unreadable"`.
