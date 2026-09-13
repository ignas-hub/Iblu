# NEEDS-IGNAS — collected across sessions 5–8 (2026-09-13)

Nothing here blocked a session. Each has a clean seam and the work around it is
finished.

## 1. Restart the MCP server and install two timers

One command. Everything else in sessions 5–8 is already live — the tick, the
collectors and the analyst do not need it.

```bash
sudo cp /home/ignas/iblu/deploy/iblu-analyst.{service,timer} /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now iblu-analyst.timer && sudo systemctl restart iblu-weekly.timer && sudo systemctl restart iblu-mcp
```

What the restart delivers: `gmail_reply` moving into the ask class,
`get_context` returning priorities and baselines, the `account` parameter on
the read tools, and `calendar(action='day' | 'reconstruct')`.
What the timers deliver: the analyst at 17:00 and 20:15 on weekdays, and the
weekly review moving from **Sunday 18:00 to Friday 18:00** (window Monday 00:00
→ Friday 18:00).

No new MCP tool was registered in these four sessions, so there is **no
"Always allow" click to make**. Every new capability went in as a parameter or
a view of an existing tool.

## 2. Book a self-block this week

`displaced` has never fired — five days of real data, zero displaced minutes.
That is not a bug. Displacement needs a calendar event that names a venture, and
nothing this week did; a flight and a dentist appointment are deliberately not
claims on your attention. A self-block with a venture in its title is the
cleanest test, and the one you already said you would book.

## 3. Greta's / GoStellar's email domain

`venture_hints` currently matches GoStellar only on the words "gostellar" and
"kassari". A domain is much stronger evidence and belongs in `DOMAINS`, where it
would attribute her mail correctly without depending on anyone naming the
agency in a subject line.

## 4. Confirm the Secretary calendar is private and silent

Two things IBLU cannot check for itself: that its sharing list is empty, and
that its event notifications are off. `calendars.get` needs a wider OAuth scope
than `calendar.events`, and widening it means re-consenting all three accounts —
not worth it for a one-time check you can do in the UI in ten seconds. It now
holds five days of reconstructed work across every venture, so it should be
visible to you and to nobody else.

## 5. Slack tokens — paused at your request

Built and wired; silently inactive until the tokens exist, and the tick is
unaffected. When you want it: one Slack app per workspace at
[api.slack.com/apps](https://api.slack.com/apps), **User Token Scopes →
`search:read`** (a bot token cannot search at all), install, then paste the two
`xoxp-` tokens. The exact `read -rs` command is in the session transcript and
can be reprinted on request.

## Already done — struck from the plan's own list

The Sessions 5–8 brief asked for `data/oauth_client.choco.json` and
`data/oauth_client.deadlift.json` to be pasted and two browser Allow clicks
made. **Both Workspaces were already authorized earlier the same day**, and all
three have been collecting since. Nothing to do.
