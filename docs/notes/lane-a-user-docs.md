# Lane A: README, GETTING-STARTED and UPGRADING material

Paragraphs written for the user-facing docs. Place them wherever the orchestrator's
structure wants them; each one stands alone.

---

## README: the hardware row

**Temperatures, without iCUE.** The panel's hardware row shows this machine's CPU
temperature with the sensor's own name beside it, the graphics card's temperature and
utilisation, and memory. Inside iCUE the two temperatures still come from the Sensors
plugin you picked in the widget settings; everywhere else they come from the companion,
which reads them from HWiNFO and from `nvidia-smi`. Tap the row for the last ten minutes
and for everything the row has no width for: the VRM, each drive, the motherboard and
chipset, the CPU package power, every fan and pump, the card's VRAM and power draw, and
what the whole machine is doing with its disks, its network and its memory.

**Nothing is required.** With no HWiNFO the row shows what it always showed, and the sheet
says why there are no temperatures. With no NVIDIA card the GPU cell simply is not there.
An older companion serves none of this and the panel does not miss it.

## GETTING-STARTED: turning the sensors on

The companion reads hardware sensors from **HWiNFO**, which is a separate, free download
from `hwinfo.com`. Install it, then:

1. Open HWiNFO and let it start in **sensors-only** mode, or open the Sensors window from
   the main window. **The shared memory exists only while the Sensors window is open.**
   Minimised counts; closed does not.
2. In **Settings**, turn on **Shared Memory Support**. This is what publishes the readings
   for anything else to read. Without it the panel's sheet says
   *"HWiNFO not running, its Sensors window closed, or Shared Memory Support off"*.
3. Also worth setting, so a relaunch is quiet: **Minimize Main Window**, **Minimize Sensors
   Window**, and **Show Sensors on Startup**.

HWiNFO needs administrator rights for its kernel driver, so it runs elevated. The companion
only ever reads what HWiNFO publishes; it never writes to it and never starts it.

Within about five seconds the panel's hardware row shows a CPU temperature with its sensor
name, and the host sheet lists the rest.

**The graphics card needs nothing.** If `nvidia-smi` is on this machine, which it is
wherever an NVIDIA driver is installed, the card's temperature, utilisation, VRAM and power
appear on their own.

## GETTING-STARTED: the twelve-hour limit, and the relaunch task

**The free build of HWiNFO stops publishing its shared memory about twelve hours after it
starts.** It keeps running and keeps showing you readings; it simply stops sharing them.
The panel notices: the readings dim and the host sheet says *"HWiNFO stopped publishing
(free build 12-hour limit): relaunch HWiNFO"*. Restarting HWiNFO fixes it for another
twelve hours.

To stop having to think about it, register the relaunch task from an **elevated**
PowerShell:

```powershell
pwsh -File .\setup\Register-HwinfoRelaunch.ps1
```

It creates one scheduled task, `SideCrab-hwinfo`, that runs at logon and again every day at
04:00. Each run stops the HWiNFO process at that exact executable path and starts it again,
so the twelve hours begin afresh while you are asleep. Add `-WhatIf` to see what it would
do, and `-Remove` to unregister it. It has to be elevated because HWiNFO is: a limited task
can neither stop nor start it.

**The Pro licence removes the need for this task**, because the paid build publishes for as
long as it runs. The free build is also licensed for **non-commercial use only**. If either
applies to you, buy the licence and skip the task.

## UPGRADING: what is new on the panel

After this upgrade the hardware row and the host sheet carry more, and none of it needs
anything from you unless you want the temperatures:

- The **CPU and GPU cells** fill from the companion wherever no iCUE Sensors bridge owns
  them, which is every standalone panel. The CPU cell names its sensor; the GPU cell adds
  the card's utilisation beside its temperature.
- The **host sheet** gains the card's VRAM and power, a machine-load line (disk, network,
  committed memory, and the busiest process), the sensor provenance and its age, every
  other curated temperature, the fans, and ten-minute charts for GPU utilisation and disk
  throughput.
- **`Test-SideCrab.ps1` has a new `sensors` row.** It passes when a source is simply
  absent and says which one, so a machine with no HWiNFO and no NVIDIA card still reports
  a clean run. `Install-SideCrab.ps1 -Status` prints the same thing in one line.

To get the temperatures, install HWiNFO and follow *Turning the sensors on* above. To keep
them past twelve hours, register the relaunch task. Everything else appears by itself.
