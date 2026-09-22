# Lane C — the user-facing paragraphs

*Two drop-ins: the first for README.md's "Using it", the second for docs/GETTING-STARTED.md.
Neither file is edited in this lane.*

---

## For README.md, "Using it"

### Four views, one display

The Sessions half of the panel shows one of four things, and the chips at the right of its header
choose which. You can also swipe left or right across that header row to step through them.
Whichever you pick is remembered, so the panel comes back the way you left it.

**Sessions** is the card grid: one card per live Claude Code session, which is what the panel shows
out of the box. The **All** and **Comfortable** chips beside the switcher narrow and tighten this
view and no other.

**Burn** is today's spend at full width. It lists what each live session has produced and what each
model has produced, draws the last 24 hours as a chart with the hours marked, and adds the day's
totals, the dollar figure when Claude Code's telemetry is flowing, and how much of your daily
budget you have used. Where the companion has no figure to give, the panel leaves the half out and
says so rather than showing you a zero.

**Week** is the last seven days: sessions finished and commits made, one column per day. Tap a day
and its history opens underneath the strip, newest first. A day the companion cannot read leaves
the strip where it is and tells you why; tapping it again tries again.

**Detail** is one session as a page. It carries the things a card has no room for: the whole
question with nothing trimmed, the full permission request with the same Approve and Deny buttons
the card's sheet gives you, how full the context window is in tokens as well as on the bar, every
subagent, the whole event list, and the continue buttons. Open it by tapping a card, then
**Full view**; or tap the **Detail** chip, which opens whichever session most wants your attention.
**Back** returns you to the cards.

**A question never hides behind a view.** If a session starts waiting while you are looking at
Burn, Week or Detail, the Sessions chip grows a pulsing count of how many. The panel does not
switch views on its own — it will not move the glass while your finger is on the way to it.

On a smaller widget slot there is no room for the switcher, so the panel shows the cards and hides
the chips. Your choice is not lost: it comes back on a slot wide enough to show it.

### The crab

Claw'd is animated now. He breathes while he is idle, blinks every eight to ten seconds, sweeps an
arm when a session wants you, sweats when a usage window goes red, and juggles, hops, snaps a claw
or dances when the fleet gives him a reason. He still wears his sunglasses when everything is
running and nothing is hot, his party hat when a session lands, and his nightcap during quiet
hours. Tapping him still acknowledges every waiting session at once.

During quiet hours, and on a machine set to reduce motion, he holds still. Nothing on this panel
moves in either of those states, which is the point of them.

---

## For docs/GETTING-STARTED.md

Add after the paragraph that first describes the session cards:

> The Sessions zone has four views, and the chips at the right of its header switch between them:
> the **session cards**, a **Burn** page with today's tokens by session, by model and by hour, a
> **Week** strip you can tap a day of to read its history, and a **Detail** page that shows one
> session in full — the whole question, the whole permission request with its Approve and Deny
> buttons, every subagent and every event. A swipe across the header row steps through them, and
> the panel remembers which one you were on.
>
> You will not miss a question while you are reading one of the other views: if a session starts
> waiting, the Sessions chip grows a pulsing count. The panel never switches views for you.
