# Reviewing a Video with the Edit Log

This is a short guide for checking a processed video and fixing anything the
automatic cutting got wrong. No technical background needed.

## 1. What automatic cutting does (and doesn't)

After you upload and process a recording, the tool automatically finds the
"dead time" -- stretches where nothing is really happening between plays --
and removes it, so you end up with a tighter highlight video without cutting
it by hand.

It's good, but it's not perfect. Every so often it will cut a moment it
shouldn't have, or leave in a stretch of downtime it should have cut. **That's
expected, not a sign something is broken.** A quick pass through the Edit Log
after processing is a normal part of using this tool every time, not a
fallback for when something goes wrong.

## 2. Opening the Edit Log

Click **Edit Log** in the left sidebar. It's always there, separate from the
Upload / Calibrate / Process / Done steps -- you can open it any time after a
video has finished processing.

You'll see two lists:

- **The cut list** (at the top, right under the video player) -- everything
  the tool removed. If something here shouldn't have been cut, you can put it
  back.
- **Kept segments** (further down) -- everything the tool kept in the final
  video. If something here shouldn't be in the highlight reel (a warm-up,
  practice swings, etc.), you can cut it.

Nothing here is permanent or risky to click around in -- cutting or restoring
a segment just changes what's in the final video; you can always change your
mind and toggle it back.

### The warning banner

If you see an orange banner like this near the top of the cut list:

> ⚠ 3 segments were automatically cut out of the middle of live action, not
> just detected as dead time -- listed first below. Preview and restore any
> that removed something real.

Check these first. They're flagged separately because they were cut from the
*middle* of what looked like a live play, not just a quiet gap between plays
-- a higher-risk kind of cut. Each one also carries its own small tag:
**⚠ Auto-cut mid-play — review recommended**. These always sort to the top of
the cut list so you see them before anything else.

## 3. Checking a segment before you decide

Every entry in either list shows a thumbnail, a timestamp range, and how long
the segment is, so you can usually tell what it is at a glance without
watching anything.

If you're not sure:

- Click **Preview** on that entry. It plays just that segment, right there in
  the list -- not the whole recording, just the few seconds in question. Click
  **Hide preview** to collapse it again.
- If the segment already made it into the current output video, you'll also
  see a **↑ Jump to output** button. Click it to scroll up and jump the main
  video player straight to that exact moment, so you can see it in context
  with what comes before and after -- useful when a clip alone doesn't tell
  you enough.

Once you're sure, click the action button on that row:

- In the cut list: **Restore** puts a segment back into the video.
- In the kept list: **Cut** removes a segment from the video.
- Either one can be undone the same way -- a restored segment can be cut
  again, and a cut segment can always be restored again.

Every time you cut or restore something, the video re-exports automatically
in the background. Give it a moment, then check the "Current output" player
and download link at the top of the page for the updated video. If it was one
of the higher-risk mid-play cuts (see below), that re-export can genuinely
take 15-25 seconds rather than being instant -- that's expected, not stuck.

### Jumping straight to the flagged ones

Right above the warning banner, there's a **↓ Next flagged** button with a
counter next to it (e.g. "1 of 6 flagged segments"). Click it to jump straight
to the next mid-play cut, wherever it is in the list -- the page scrolls to it
and briefly outlines it in blue so you don't lose track of where you landed.
Keep clicking to work through all of them in order without scrolling past the
ordinary cuts in between. It keeps working even after you've restored one, so
you can use it to double back and double-check your own decisions too.

### Keyboard shortcuts while previewing

Once a preview is open, you don't need the mouse for the two most common
actions:

- **Space** -- play or pause the preview.
- **Enter** -- do the same thing as the action button on that row (Restore /
  Cut / Cut again), whichever it currently says.

Both are shown as a small reminder right under the preview player. Handy if
you're going through a long list and fixing several things in a row.

## 4. If something goes wrong

Occasionally something on the technical side hiccups. You'll see a plain
message telling you what happened, not a raw error code:

- **"Can't reach the server right now"** -- the tool's backend isn't
  responding. Make sure it's running, then use the **Try again** button.
- **"Server is out of disk space"** -- the computer running this has run out
  of storage. There's no **Try again** button for this one on purpose: it
  won't fix itself by clicking around. Someone needs to free up space on that
  computer first -- once they have, come back and pick up where you left off.
- Any other message -- use the **Try again** button.

Where it appears, **Try again** genuinely re-checks with the server for where
things actually stand, rather than just reloading the page -- so it's always
safe to click, and it will pick up your work correctly whether the problem
has cleared up or not.

If **Try again** doesn't work after a couple of tries, that's the point to
ask for help rather than keep retrying.
