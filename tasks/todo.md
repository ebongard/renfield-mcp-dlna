# Plan: Comprehensive, robust UPnP/DLNA device support (reviewed)

Reviewed via /plan-eng-review on 2026-06-05. All decisions below are user-approved.
Goal: turn a renderer-only controller (external URLs, hand-rolled SSDP, hardcoded
protocolInfo) into a full UPnP **AV control point** — renderers **and** MediaServers —
robust on real networks, with per-device-class playback backends.

Confirmed scope: renderers + MediaServers; device classes OpenHome (Linn), DLNA TVs
(Samsung/LG/Sony), standard AVTransport (HiFiBerry/Kodi/VLC), Sonos; IPv4 multi-interface
live-cache discovery.

## Progress (branch `feat/upnp-control-point-phase1`, 129 tests green)

Landed and verified with the mock suite (real-device validation still pending where noted):

- ✅ **T5** `resolve_session`/`resolve_renderer` helpers + `_error()` — tool guards de-duplicated.
- ✅ **T3** `PlaybackBackend` ABC + `AvTransportBackend` extracted from `QueueSession`; device
  I/O (DmrDevice, RC volume/mute, polling, raw event parse) now lives in the backend.
- ✅ **Code review** of the diff: found + fixed a real played-gate regression (sticky flag →
  faithful `_prev_transport_state`) and an ABC signature lie. Regression test added.
- ✅ **T2** `ControlPoint` replaces module-global infra; lazy-init race closed with a lock.
- ✅ Discovery identity capture (`manufacturer`/`model_name`) + `is_openhome` detection; backend
  factory seam wired.
- ✅ **T6 (partial)** P2 read+write surface: enriched `get_status`
  (position/duration/capabilities/volume/muted/valid_play_modes), new `get_mute`, `seek`,
  `set_play_mode` tools. *Design change:* one `set_play_mode` instead of separate
  set_repeat/set_shuffle — UPnP exposes a single CurrentPlayMode enum, so independent toggles
  would clobber each other.
- ✅ CLAUDE.md architecture section updated for the new layout.

**Pending (needs a real LAN / hardware to verify — not shipping blind):** T1 full SSDP-listener
discovery rewrite, T4 OpenHome spike, T8 TV protocolInfo validation, T7 metadata hot-path,
T9 MediaServer, T10 OpenHomeBackend, T11 Sonos, T12 session hardening, T13 real-device harness.

## Decisions (from review)

| # | Decision |
|---|----------|
| Step0 | Adopt `async-upnp-client` for **discovery** wholesale (delete raw-socket SSDP). |
| T1 | Metadata is **HYBRID**: library `construct_play_media_metadata` is the default for audio/standard renderers; **`didl.py`'s logic is refactored into a per-device-family protocolInfo strategy** that is the PRIMARY path for TVs (correct `DLNA.ORG_PN`/`OP`/`FLAGS`). Do NOT delete `didl.py` until validated against a real Samsung/LG. |
| Issue1 | **PlaybackBackend ABC** + per-family impls: `AvTransportBackend` (DmrDevice), `OpenHomeBackend` (custom av-openhome-org), `SonosBackend` (soco). QueueSession owns queue+event state and delegates device I/O to the backend, chosen by a factory at discovery from identity+services. |
| T2 | ABC is **provisional** until both "we own the queue" (AVTransport) and "device owns the queue" (OpenHome) are seen. **OpenHome spike lands in P1**; freeze ABC after. |
| Issue2 | **ControlPoint** object owns requester, notify/event server, discovery, session registry, server registry, backend factory. Replaces module globals. |
| T3a | **Single discovery semantic**: streamable-http = primary live-cache (`SsdpListener`); stdio = degraded mode (startup/on-demand `async_search` + lazy listener within the persistent session). One shared "device-gone" contract — not two drifting systems. |
| T3b | Device registry keyed **strictly by UDN**. Renderer + server are usually **separate root devices/UDNs** (Kodi/Plex) — correlate by host/IP for presentation only, never merge identity. Verify vs a real Kodi before fixing the schema. |
| Issue4 | `SonosBackend` **wraps `soco`** (Layer-1), isolated to one adapter file, in a later PR. |
| Issue5 | `resolve_session()` helper + error formatter kills repeated per-tool guard boilerplate. |
| Issue6 | Mock suite = deterministic backbone (port the 6 event/volume regressions to assert through the backend). **+ opt-in env-gated real-device harness per backend. + capture real SOAP request/response fixtures** as replay regression data so mocks assert real protocolInfo, not invented strings. One gating real-device smoke per family per phase. |
| Issue7/T3d | Metadata hot-path: accept caller-supplied **MIME** hint only (drop caller DLNA flags — an LLM can't produce a valid `DLNA.ORG_PN`); HEAD-negotiate when absent; **memoize negotiated metadata by `(url, device-UDN)`**, invalidate on backend change. |
| T3c | **Session hardening is its own phase** (P7) with explicit acceptance criteria, not "folded in". |
| Note | Once `OpenHomeBackend` exists, the Linn bogus-volume-range workaround is **no longer on Linn's path** (Linn uses its OpenHome Volume service); it remains only as the generic AVTransport fallback. |

## What already exists (reuse, don't rebuild)

- `discovery.py` (~310 lines raw-socket SSDP incl. the Linn `upnp:rootdevice` workaround) →
  **replaced** by `async_search` + `SsdpListener`; the Linn workaround **ports** as an extra
  search target. ~300 lines deleted.
- `didl.py` → **refactored, not deleted** into the device-family protocolInfo strategy (T1).
- `queue_manager.py` event logic (gapless detect, auto-advance, `_advancing` dedupe,
  played-gate, list-not-dict event folding) → **kept**, moved behind the backend; covered by
  6 existing tests that get ported.
- Library `DmrDevice` (seek/playmode/position/caps/`construct_play_media_metadata`) and
  `DmsDevice` (browse/search) → adopted instead of hand-rolling.

## Architecture (target)

```
                              ControlPoint  (owns shared infra + registries)
              ┌──────────────────────┼───────────────────────────────┐
        SsdpListener /          notify/event server              backend factory
        async_search            (GENA callbacks)                 (identity+services → backend)
              │                        │                               │
      device registry (key: UDN; role flags; host-correlated for combo)│
              │                                                         │
   ┌──────────┴───────────┐                          ┌─────────────────┴─────────────────┐
 renderers              servers (DmsDevice)      PlaybackBackend (ABC, provisional→frozen)
   │                       │                     ┌──────────┬──────────────┬─────────────┐
 QueueSession ────────────────────────────────▶ AvTransport  OpenHome       Sonos
 (queue state + events,                          (DmrDevice)  (av-openhome   (soco;
  delegates device I/O)                          we own queue  -org Playlist;  device+
                                                              device owns Q)  zones)
```

Backend selection:

```
discover device ──▶ identity (mfr/model) + advertised services
   │
   ├─ av-openhome-org:Playlist present?        ──▶ OpenHomeBackend   (device owns queue)
   ├─ Sonos (ZonePlayer / mfr=Sonos)?          ──▶ SonosBackend (soco)
   └─ else AVTransport present?                ──▶ AvTransportBackend (we own queue)
                                                     └─ TV family → per-family protocolInfo strategy
```

QueueSession transport-state machine (kept; gets an inline ASCII comment):

```
            set_uri+play
  idle ───────────────────▶ buffering ──PLAYING──▶ playing ──STOPPED(after played)──▶ next/end
   ▲                            │                    │  ▲ gapless: CurrentTrackURI==preloaded
   │                     STOPPED/NO_MEDIA            │  └───────── preload next (SetNext) ─────────┐
   └──────── cleanup ◀── (transient, pre-play:      │                                              │
              (last track)   ignored — played-gate) └── no-SetNext: STOPPED→_auto_advance (deduped)┘
```

## Phases (revised)

- **P1 — Foundation.** Library-backed discovery (single semantic, multi-interface, Linn
  rootdevice ST) · ControlPoint + UDN-keyed device registry (host-correlated combo) ·
  PlaybackBackend **provisional ABC** + AvTransportBackend · hybrid-metadata scaffold (library
  audio default + family-strategy seam) · `resolve_session` helper · **OpenHome spike**
  (throwaway: one real Linn Playlist play to validate the device-owns-queue shape) · port all
  85 tests + 6 regressions to the backend. **Freeze ABC after the spike.**
- **P2 — Renderer playback.** Tools: `seek`, `set_repeat`, `set_shuffle`, `get_mute`; enriched
  `get_status` (position/duration/playmode/capabilities); `capabilities` in `list_renderers`.
  Metadata hot-path (caller MIME hint + memoize by (url,UDN)).
- **P3 — TV family.** Device-family protocolInfo strategy (`DLNA.ORG_PN`/`OP`/`FLAGS`), video
  items, subtitle/`captionInfo`. **Validate vs a real Samsung/LG**, then prune any dead
  `didl.py` paths.
- **P4 — MediaServer.** `DmsDevice`: `list_servers`, `browse` (paginated, capped),
  `search`, `play_from_server` (resolve `res` URL → play_tracks). Combo-device correlation.
- **P5 — OpenHome full.** `OpenHomeBackend` (native Playlist queue + OpenHome Volume); retire
  the Linn RC volume workaround to AVTransport-fallback-only.
- **P6 — Sonos.** `SonosBackend` wrapping `soco` (queue + zone awareness), isolated adapter;
  reconcile Sonos discovery with the registry by UDN.
- **P7 — Session hardening (own phase).** Per-UDN `asyncio.Lock` on mutating ops · GENA
  subscription auto-renew before timeout · NOTIFY-gap watchdog → re-subscribe + poll fallback.
  Acceptance: subscription survives a device reboot; concurrent tool calls on one UDN serialize;
  status never goes permanently stale on a multi-interface host.
- **P8 — Docs + interop.** README/CLAUDE.md update · real-device interop matrix · captured SOAP
  fixtures committed as regression data.

## Failure modes (per new codepath)

| Codepath | Realistic prod failure | Test? | Error handling? | User sees |
|---|---|---|---|---|
| TV metadata | TV rejects DIDL lacking exact DLNA.ORG_PN | P3 family-strategy test + real Samsung smoke | family strategy supplies PN | clear "renderer rejected media" (after fix) |
| Event callback (multi-NIC) | device on VLAN B can't POST GENA events → stale status, gapless dead | **CRITICAL GAP today** | P7 watchdog + poll fallback | **silent** until P7 ships → flag |
| OpenHome on AVTransport fallback | Linn has no/partial AVTransport → play errors in P1–P4 | OpenHome spike in P1 surfaces it early | OpenHomeBackend (P5) | errors until P5 (accepted, sequenced) |
| `play_from_server` | ContentDirectory item has no playable `res` | P4 unit test | explicit error, not silent | "no playable URL for item" |
| Metadata memoize | transcode profile changes → stale cached metadata | P2 test | invalidate on backend change | correct after invalidation |
| Per-UDN concurrency | two tool calls race one renderer | P7 lock test | per-UDN lock | serialized |

**Critical gap flagged:** multi-interface GENA event callback unreachability is silent today
and only fixed in P7 — P7 must not slip, or long-lived http sessions rot silently.

## NOT in scope (deferred, with rationale)

- **IPv6 SSDP** — user chose IPv4 only; revisit if IPv6 UPnP gear appears.
- **Chromecast / AirPlay** — not UPnP (Cast/mDNS, RAOP); out of protocol scope. Document.
- **Sonos zone *control*** (create/break groups) — P6 ships zone *awareness* only; grouping
  control is a follow-up.
- **ConnectionManager PrepareForConnection negotiation** — most renderers use connID 0; add
  only if a real device needs it.
- **Photo/image items** — audio+video cover the use case; trivial to add later via DmsDevice.
- **Persisting sessions across restarts** — in-memory is fine for the service model.

## Worktree parallelization

| Step | Modules | Depends on |
|---|---|---|
| P1 foundation | control_point, discovery, backends/base+avtransport, server | — |
| P2 renderer tools | server, backends/avtransport | P1 (frozen ABC) |
| P3 TV family | metadata strategy, backends/avtransport | P1 |
| P4 MediaServer | mediaserver, server | P1 |
| P5 OpenHome | backends/openhome | P1 (frozen ABC) |
| P6 Sonos | backends/sonos | P1 (frozen ABC) |
| P7 hardening | control_point, queue_session | P1 |

P1 is a hard barrier (everything depends on the frozen ABC + ControlPoint). After P1:
- **Lane A:** P3 (TV) — touches metadata strategy
- **Lane B:** P4 (MediaServer) — independent module
- **Lane C:** P5 (OpenHome) → then P6 (Sonos) — both backends, sequential to avoid factory churn
- **Lane D:** P2 (renderer tools) + P7 (hardening) — both touch server/queue_session → sequential
Conflict flags: P2 and P7 both touch `queue_session`/`server` (Lane D sequential). P5/P6 both
touch the backend factory registration (Lane C sequential).

## Implementation Tasks
Synthesized from review findings. P1 blocks ship; checkbox as you ship.

- [ ] **T1 (P1, human: ~2d / CC: ~1-2h)** — discovery — replace raw-socket SSDP with `async_search`+`SsdpListener`, single semantic, multi-interface, port Linn rootdevice ST
  - Surfaced by: Step 0 scope challenge
  - Files: src/renfield_mcp_dlna/discovery.py, control_point.py
  - Verify: discovery unit tests (alive/byebye cache, Linn ST, stale evict) green
- [ ] **T2 (P1, human: ~1d / CC: ~45min)** — control_point — ControlPoint owns infra + UDN registry (host-correlated combo)
  - Surfaced by: Issue 2 + T3b
  - Files: src/renfield_mcp_dlna/control_point.py, server.py
  - Verify: tests construct a fresh ControlPoint (no module-global monkeypatch)
- [ ] **T3 (P1, human: ~2d / CC: ~1-2h)** — backends — PlaybackBackend provisional ABC + AvTransportBackend; port 85 tests + 6 regressions through backend
  - Surfaced by: Issue 1 + Issue 6 regression rule
  - Files: src/renfield_mcp_dlna/backends/base.py, avtransport.py, queue_manager.py, tests/
  - Verify: full suite green via session.backend; 6 event/volume regressions assert behavior
- [ ] **T4 (P1, human: ~1d / CC: ~1h)** — backends/openhome — throwaway OpenHome spike: one real Linn Playlist play; finalize ABC after
  - Surfaced by: Tension 2
  - Files: spike branch; backends/base.py (freeze)
  - Verify: real Linn plays via Playlist; ABC models device-owned queue
- [ ] **T5 (P1, human: ~2h / CC: ~20min)** — server — resolve_session helper + error formatter across all tools
  - Surfaced by: Issue 5
  - Files: src/renfield_mcp_dlna/server.py
  - Verify: not-found / no-session / success unit tests; consistent error shape
- [ ] **T6 (P2, human: ~1d / CC: ~1h)** — server — seek/set_repeat/set_shuffle/get_mute + enriched get_status + capabilities
  - Surfaced by: Section 2 (missing features)
  - Files: server.py, backends/avtransport.py
  - Verify: capability-gated success + graceful unsupported per tool
- [ ] **T7 (P2, human: ~3h / CC: ~30min)** — metadata — caller MIME hint + HEAD fallback + memoize by (url,UDN)
  - Surfaced by: Issue 7 + T3d
  - Files: backends/avtransport.py, metadata strategy module
  - Verify: hint path skips HEAD; absent path negotiates; re-advance doesn't re-HEAD; invalidates on backend change
- [ ] **T8 (P3, human: ~2d / CC: ~1-2h)** — metadata strategy — per-device-family protocolInfo (TV DLNA.ORG_PN/OP/FLAGS) + video + subtitles; validate vs real Samsung
  - Surfaced by: Tension 1
  - Files: src/renfield_mcp_dlna/didl.py → metadata/strategy.py
  - Verify: real Samsung/LG accepts video; family unit tests with captured SOAP fixtures
- [ ] **T9 (P4, human: ~2-3d / CC: ~1-2h)** — mediaserver — DmsDevice list_servers/browse(paginated)/search/play_from_server
  - Surfaced by: Phase 4 scope
  - Files: src/renfield_mcp_dlna/mediaserver.py, server.py
  - Verify: browse/search parse, res-URL resolution, server→renderer play wiring
- [ ] **T10 (P5, human: ~3-4d / CC: ~2h)** — backends/openhome — full OpenHomeBackend (Playlist + Volume); retire Linn RC workaround to fallback-only
  - Surfaced by: Issue 1 + volume note
  - Files: backends/openhome.py, backends/avtransport.py
  - Verify: Linn native gapless queue + OpenHome Volume; RC workaround no longer on Linn path
- [ ] **T11 (P6, human: ~2-3d / CC: ~1-2h)** — backends/sonos — SonosBackend wrapping soco (queue + zone awareness)
  - Surfaced by: Issue 4
  - Files: backends/sonos.py, pyproject.toml (soco dep)
  - Verify: Sonos queue add/clear + zone group reported; UDN reconciled with registry
- [ ] **T12 (P7, human: ~2d / CC: ~1-2h)** — hardening — per-UDN lock + GENA auto-renew + NOTIFY-gap watchdog/re-subscribe
  - Surfaced by: Tension 3 (session hardening) + critical failure mode
  - Files: control_point.py, queue_manager.py
  - Verify: survives device reboot; concurrent calls serialize; multi-NIC status not permanently stale
- [ ] **T13 (P1+, ongoing, human: ~1d / CC: ~1h)** — tests — opt-in env-gated real-device harness per backend + captured SOAP fixtures
  - Surfaced by: Issue 6
  - Files: tests/integration/, tests/fixtures/
  - Verify: RENFIELD_TEST_RENDERER suite skipped without hardware; fixtures replay real protocolInfo

## Notes
- Bump `async-upnp-client` floor to `>=0.47`; add `soco` (P6 only).
- Each phase is an independent PR matching the repo's one-feature-per-branch history.

## Review
(filled in post-implementation)

## GSTACK REVIEW REPORT

| Review | Trigger | Why | Runs | Status | Findings |
|--------|---------|-----|------|--------|----------|
| CEO Review | `/plan-ceo-review` | Scope & strategy | 0 | — | not run |
| Codex Review | `/codex review` | Independent 2nd opinion | 0 | — | codex not installed |
| Eng Review | `/plan-eng-review` | Architecture & tests (required) | 1 | CLEAR | 7 issues + scope reduction; 1 critical failure mode (plan-addressed in P7) |
| Design Review | `/plan-design-review` | UI/UX gaps | 0 | — | n/a (no UI surface) |
| DX Review | `/plan-devex-review` | Developer experience gaps | 0 | — | not run |

- **OUTSIDE VOICE:** ran (Claude subagent, codex unavailable) — 8 findings. 2 reversals applied
  (metadata → hybrid library+per-family strategy; OpenHome spike pulled into P1 + ABC kept
  provisional) and 4 refinements applied (single discovery semantic, combo-device by separate
  UDN, session hardening as its own phase, metadata memoize by (url,UDN)).
- **CROSS-MODEL:** review favored full library adoption; outside voice showed it wouldn't fix
  strict-TV interop → reconciled to hybrid (user chose). Outside voice's "stdio TTL cache is
  useless" rested on a wrong premise (MCP stdio persists within a session) — corrected, but its
  "don't run two discovery systems" point was adopted.
- **UNRESOLVED:** 0 decisions left open.
- **VERDICT:** ENG CLEARED — ready to implement. Start with P1 (foundation + frozen ABC).
