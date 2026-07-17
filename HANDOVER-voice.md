# Handover — hands-free voice MCP for iblu-keeper

**Date:** 2026-06-29
**Context window:** previous session was full; this doc is the handover.

## The problem (already diagnosed)

Claude.ai's **Voice Mode** (waveform icon, "you talk / Claude talks back") **does
not surface MCP connectors** to the model — neither built-in (Drive) nor
custom (iblu). Text chat sees the tools fine; voice mode does not. When the
model tries to use a tool that isn't exposed, it fabricates plausible-looking
"stale schema" errors instead of admitting it can't see the tool.

This is a **known open Anthropic bug**, filed 2026-04-04:
[github.com/anthropics/claude-ai-mcp/issues/146](https://github.com/anthropics/claude-ai-mcp/issues/146)
— "No registered MCP connectors are discovered in voice chat." Still open.
Reproducible on web and Android. There is no user-side toggle. Waiting for
Anthropic is not a plan.

The iOS **microphone icon** (dictation → text → send) works fully because it
routes through the same text-chat engine. That's the *only* way MCP tools
currently work with voice in Claude.ai — press-to-dictate, not conversation.

Ignas wants **truly hands-free** — phone in his pocket while driving, no
button presses per turn, tools available.

## Options (bottom-up analysis)

### A. VoiceMode plugin + Claude Code on the Mac — quickest, wrong device

- Setup: `claude mcp add --transport http iblu https://mcp.iblugames.com/mcp`
  then `claude plugin install voicemode@voicemode` then `/voicemode:converse`.
  ~30 minutes.
- Fully hands-free **at the Mac**. Mic + speakers required.
- **Wrong device for the driving use case** — Ignas is in the car, the Mac isn't.
- Useful as a fallback for at-desk work; not the target solution.

### B. Custom voice agent on Hetzner + iOS Shortcut — best for the car

The architecture Ignas actually needs:

```
iPhone (Shortcut or CarPlay/Siri trigger)
    │  record audio
    ▼
POST audio → https://voice.iblugames.com/turn
    │  (endpoint on Hetzner)
    │  1. STT (Whisper API / Anthropic if available / Google Speech)
    │  2. Anthropic Messages API call with:
    │       - system prompt reused from iblu MCP `instructions`
    │       - mcp_servers=[{url:"https://mcp.iblugames.com/mcp", …}]
    │       - conversation memory (SQLite, keyed by device id)
    │  3. TTS (ElevenLabs / OpenAI TTS / Google TTS)
    ▼
respond with audio bytes → Shortcut plays
```

Anthropic's Messages API **natively supports remote MCP servers via the
`mcp_servers` request parameter** — the same iblu server we've built runs
unchanged. No proxy, no bridge.

Effort: **~1–2 days** for a first working version. Cost per turn: pennies
(STT + LLM + TTS all pay-as-you-go).

### C. Wait for Anthropic — no

Not a solution.

## Recommendation

**Go with B.** It's the only architecture that fits "phone in pocket while
driving." A is a fallback for the desk.

## Concrete next steps for the new session

### Decisions to confirm with Ignas before building

1. **STT choice.** OpenAI Whisper API (~$0.006/min, very good), or Google
   Speech (already have Google creds — no new account), or Anthropic if
   they've shipped native STT.
2. **TTS choice.** OpenAI TTS (fast, natural, cheap), ElevenLabs (best
   quality, more expensive), or Google TTS (uses existing creds).
3. **Trigger UX.** iOS Shortcut (works today, tap once), or Siri
   Shortcut with wake phrase ("Hey Siri, iblu…"), or a dedicated iOS app
   (biggest UX win but real build).
4. **Memory model.** Per-device conversation history? Rolling window?
   Persistent long-term memory (Phase 2 of iblu-keeper already stubbed)?

### Suggested first-cut implementation

- **New service** on the Hetzner box, e.g. `iblu-voice.service`, systemd-managed
  like the existing `iblu-mcp` and `iblu-dashboard`. FastAPI or similar.
- **New subdomain**: `voice.iblugames.com`, Cloudflare-fronted (same pattern
  as `mcp.iblugames.com`), Origin cert already covers `*.iblugames.com`.
- **Auth**: shared secret in header for the Shortcut → endpoint call. Not
  OAuth (single-user personal use).
- **Reuse the iblu MCP** wholesale via Anthropic's `mcp_servers` param —
  don't duplicate tool code.
- **Deploy pattern**: mirror the existing MCP server exactly (systemd unit,
  nginx server block, .env with API keys).

### What's already in place (nothing to rebuild)

- **iblu MCP server** at `https://mcp.iblugames.com/mcp` — 30 tools, live,
  Google-auth working, `server_health` for status. Anthropic Messages API
  can point at it directly with `mcp_servers` param.
- **Hetzner box** at `178.104.122.152`, Ubuntu, Docker + nginx + Caddy
  already set up. SSH: `ssh ignas@178.104.122.152`.
- **Cloudflare** manages iblugames.com DNS; Origin cert wildcard covers
  new subdomains.
- **Google OAuth** — 13 scopes granted, refresh token in
  `/home/ignas/iblu/data/token.json`. `server_health` tool tells you the
  live auth state.

### Sample Anthropic Messages API call the new endpoint would make

```python
import anthropic
client = anthropic.Anthropic()
resp = client.messages.create(
    model="claude-sonnet-5",  # or whatever's current
    max_tokens=2000,
    mcp_servers=[{
        "type": "url",
        "url": "https://mcp.iblugames.com/mcp",
        "name": "iblu",
        "authorization_token": "<from iblu's OAuth flow>",
    }],
    messages=[{"role": "user", "content": user_transcript}],
    system="… reuse iblu MCP's instructions field …",
)
```

Anthropic executes tool calls server-side against the MCP endpoint; the
response comes back with the tool results already applied.

### Known unknowns to research first

- Does Anthropic's Messages API `mcp_servers` support the same OAuth flow
  Claude.ai uses (DCR)? If not, we may need to embed a service account or
  a long-lived token.
- Latency budget: STT + LLM + TTS end-to-end for driving — probably needs
  streaming to feel responsive. First version can be non-streaming (a few
  seconds per turn) to validate the loop.

### Rules the new session should follow (Ignas's standing preferences)

- **Non-developer** — explain plainly, one step at a time. Confirm before
  destructive actions. He drives a lot and may be on mobile.
- **SSH command first** — every command targeting the Hetzner box must
  lead with `ssh ignas@178.104.122.152` in the same block. He has multiple
  Mac terminals and has run box-only commands on his Mac before.
- **Automate, don't tell him to remember** — don't say "bookmark this" or
  "remember to check" — embed the guidance into the system.
- **See `/home/ignas/.claude/projects/-home-ignas/memory/MEMORY.md`** for
  any updated user preferences.

## Where the code + docs live

- Repo: [github.com/ignas-hub/Iblu](https://github.com/ignas-hub/Iblu) — branch `main`, latest commit is the shared-drive fix (`a3301f6`).
- Handover docs at repo root: this file, `STATE.md`, `HANDOFF.md`, `DEBUG_FINDINGS.md`, `DEPLOY_HANDOFF.md`.
- Working tree at `/home/ignas/iblu` on the Hetzner box.
