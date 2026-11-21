# Portable scanner recognition — coordinated design (2026-11-21)

Goal: plug the M-10 into **any** laptop, open the site, press the scanner pill, and
that browser receives the scans. Today the path only works when the kiosk browser,
the server and the daemon share one host.

Five agents (`deepseek`, `mimo`, `glm`, `kimi`, `qwen`) planned this independently,
cross-reviewed each other, and revised. This is the coordinator's synthesis and the
binding decision record. (`deepseek` produced its plan and review but its provider
account ran out of credit before the final revision; the other four converge.)

Raw artifacts: `/tmp/scanner-portable/{brief.md,plans,reviews}` (scratch, not committed).

---

## 1. The two measurements that decided the architecture

**(a) The CSP forbids the obvious design.** `app/main.py:408`:

```
default-src 'self'; script-src 'self' 'unsafe-eval'; style-src 'self' 'unsafe-inline';
img-src 'self' data: https:; connect-src 'self'; object-src 'none';
base-uri 'self'; form-action 'self'; frame-ancestors 'none'
```

A page on the server cannot `fetch()` `http://127.0.0.1:PORT` (`connect-src 'self'`),
and cannot iframe it either — there is no `frame-src`, so `default-src 'self'`
applies. Three of the five plans were fetch-based and therefore dead on arrival;
CORS headers on the agent cannot relax the *page's* policy. Amending the CSP would
rescue the fetch shape but not DNS rebinding, Chrome's Private Network Access, or
Safari's loopback handling — so we do **not** touch the policy.

**Only navigation escapes the CSP.** A popup opened by the pill click is top-level
navigation: `window.open` is not governed by `connect-src`, `postMessage` is not
governed by CORS, and the agent's own page may fetch its own origin freely.

**(b) `/pair` is already ungated** (`app/routers/scanner.py:334`) — it is the
split-host fallback, and the 8-character code is the gate (constant-time, single-use,
atomic mint). So the browser half of pairing needs **no new server endpoint at all**;
it needs the code, delivered over loopback.

## 2. Decision: popup handshake + staff invite

```
pill click (user gesture)
  └─ window.open("http://127.0.0.1:<port>/pair?nonce=<128-bit>")   ← navigation, CSP-free
        agent page (own CSP, nonce'd script)
          ├─ verifies Host (127.0.0.1/localhost only)      ← DNS-rebinding defence
          ├─ verifies Origin == its configured site origin  ← else: no offer
          └─ postMessage({type, nonce, terminal_id, pairing_code, status}, siteOrigin)
                 └─ window.close()
  ├─ SPA verifies event.origin, event.source === popup, type, nonce
  ├─ POST /api/scanner/pair {terminal_id, code}   ← existing endpoint, same-origin
  └─ store token → existing poll loop → pill `live`
```

Why this and not the alternatives:

| Alternative | Verdict |
|---|---|
| SPA `fetch()` to the agent (deepseek, qwen, mimo) | Dies on `connect-src 'self'`, then on PNA/Safari. Rejected |
| Iframe + postMessage | Dies on `default-src 'self'`. Rejected |
| Amend the CSP | Widening for every page load to serve one feature; does not fix rebinding/PNA. Rejected |
| Custom URI scheme `hcrm-scanner://` (kimi) | Works, but needs a registered handler + a browser prompt (Firefox every time) and is a per-OS install surface. Deferred fast-path |
| LAN-wide `REGISTER_ANY` + typed code | Typed code violates the UX; register is the only gate we actually need |

The popup doubles as the status surface: it can say *why* it cannot offer (no device,
no permission) instead of failing silently.

**Admission control (registration).** A laptop that is not the server cannot register
without one of: loopback, the legacy `HCRM_SCANNER_REGISTER_ANY=1`, or a **staff-minted
single-use invite** (16 chars, TTL, hash-only, atomically consumed, max open). The
invite rides the one-time install and is deleted at first registration. The pill
itself never needs a staff session — pairing is terminal-auth.

## 3. Identity, and why NOTES.md §9 becomes impossible

* `terminal_id` stays the server's key and the human label (hostname default) — no
  endpoint changes.
* New nullable **`machine_key`** = `sha256(/etc/machine-id)[:16]`, unique. Stable
  across renames, fresh on OS reinstall. A register carrying a known key under a new
  `terminal_id` is a 409 naming the old one — renames are caught at the door.
* Portable config moves to `~/.config/hcrm/` (XDG), so copying a `data/` directory
  can no longer transplant an identity (the 2026-11-17 root cause).
* The portable path writes **no offer file**: the code travels by `postMessage`, so
  the three-way identity (DB row ↔ config ↔ offer file) collapses to two.
* That two-way self-heals: `409 already_paired` → agent `reclaim` → fresh code → pair.
* Heartbeats make a stale agent visible instead of silent.

## 4. Truth: `live` must mean something

New `POST /api/scanner/terminals/{id}/heartbeat` (secret-authed, JSON), sent by the
daemon at start, every 30 s and on hotplug, recording `last_seen_at` plus
`device_status` (`present|absent|no-permission`) and the personality. `GET /events`
gains an additive `agent: {online, device_status, personality?}`.

The pill's `live` requires **both** a fresh heartbeat and `device_status == present`.
Today a paired browser with the scanner unplugged shows `live` — a lie. That is fixed
for the same-host kiosk too.

Pill states stay `live | off | unpaired` (the CSS/`data-scanner-feed` contract is
untouched) and gain a `data-scanner-detail` reason slot:
`no-agent | no-device | no-permission | bound-elsewhere | register-denied | server-down`.

## 5. Backlog

Ordered so every item is independently verifiable **with no scanner attached**.

| # | Item | Deps | Hardware-free |
|---|---|---|---|
| **P1** | Server truth + admission: heartbeat endpoint, `device_status`/`machine_key`/`pairing_code_expires_at` columns + migrations, code TTL in `_pair_terminal`, `POST /terminals/{id}/reclaim`, additive `agent` in `GET /events`, `POST /invites`, invite-gated register, rate limit, coded errors | — | yes |
| **P2** | Agent: `--local-port` popup server (`/pair`, `/status`, nonce issue), Host/Origin/`Sec-Fetch-Site` guards, own CSP page, invite registration + `machine_key`, XDG config, heartbeat thread, 409→reclaim loop | P1 | yes (fake sysfs/reader) |
| **P3** | SPA: synchronous `window.open`, message validation, `/pair` call, detail states + i18n (all languages), degraded-old-server rule | P1, P2 | yes (node tests + mocked popup) |
| **P4** | Installer + systemd **user** unit + udev `uaccess`, docs (trust argument, §9 fix, the USB boundary) | P2 | script dry-run only |
| **P5** | Hardware-free end-to-end proof: scratch server + real agent on an ephemeral port + a scripted "browser" replaying the exact popup flow, synthetic scans, absent/denied/stale/replay paths | P1–P4 | yes |
| **P6** | Physical acceptance on two laptops (a001 and a002 personalities, replug, offline) | P1–P5 **and** a working USB link | no |

## 6. Security decisions (binding for implementation)

1. **Bind 127.0.0.1 only.** Never `0.0.0.0`, never a LAN interface.
2. **Host allowlist** on the agent: `127.0.0.1:<port>` and `localhost:<port>` only;
   anything else → 403 and close. This is the DNS-rebinding defence; an Origin check
   alone does not cover a rebinded same-origin GET (which sends no `Origin`).
3. **No `Access-Control-Allow-Origin: *` on the agent**, ever. The popup is a
   navigation, not a fetch, so the agent needs no CORS at all.
4. **Nonce**: 128-bit, single-use, ≤120 s, embedded in the popup URL, echoed in the
   `postMessage` and verified by the SPA. `postMessage` uses the **pinned** site
   origin, never `*`.
5. **The pairing code appears in exactly one place**: the `postMessage` to the
   allowlisted opener. Never in a fetchable URL, never in `/status`, never in a log.
6. **The agent page carries its own strict CSP** (`default-src 'none'; script-src
   'nonce-…'; connect-src 'self'; base-uri 'none'`), no external resources.
7. Registration stays closed by default; an invite is the only non-loopback admission.
   Codes are hash-only server-side, constant-time compared, atomically consumed.
8. No credential in a URL, a log line, an error body, or a redirect.

## 7. The honest boundary

None of this fixes the dead USB link. On `hertha-shop` today the device fails every
descriptor read (`-EPROTO`, `docs/m10-connection-debug-2026-11-20.md`), so the honest
pill is `off`/`no-device` — and `live` is structurally impossible without a fresh
heartbeat reporting `present`. The feature is correct and fully provable with the
device absent; physical acceptance (P6) is blocked on the hardware fault.
