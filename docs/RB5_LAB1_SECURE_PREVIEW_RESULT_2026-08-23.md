# RB5 Lab1 Secure Selected-Camera Preview Result

Date: 2026-08-23
Verdict: **PASS**

## Scope

Connect the physical Lab1 D455, D405, and OTOS WGC200 streams to the Central Admin UI through the existing on-demand one-camera relay. This gate was read-only: it did not send RB5 commands or operate any welding equipment.

## Runtime topology

- Central Admin: `http://127.0.0.1:18091`, loopback-only.
- Agent heartbeat ingress: `https://10.0.0.8:8443`, existing mTLS heartbeat v2.
- Edge preview: `https://10.0.0.9:8101`, TLS 1.2+ mutual TLS.
- Lab1 UFW permits `8101/tcp` only from Central `10.0.0.8`.
- Edge server certificate SANs: `lab1.local`, `10.0.0.9`, and `10.0.0.4`.
- Edge pins the exact Central preview-client SHA-256 fingerprint. The private keys remain mode `0600` in role-separated secret directories.

## Registered assets

- Processing Node: `processing-lab1`
- Robot: `rb5-lab1` bound to `processing-lab1/rb5_lab1_eval_01`
- OTOS: `rb5-lab1-otos-wgc200` bound to `welding_view`
- D455: `rb5-lab1-d455` bound to `d455_color`
- D405: `rb5-lab1-d405` bound to `d405_color`
- Workcell: `workcell-rb5-lab1`

The Central database was backed up before these registry writes as `fleet.before-lab1-preview-assets-20260823.db`.

## Security evidence

- No client certificate: rejected during the TLS handshake with `certificate required`.
- Trusted lab HTTPS certificate with the wrong fingerprint: rejected with HTTP `403`.
- Exact pinned Central preview-client certificate: accepted with HTTP `200`.
- Central validates the Edge certificate and requires the advertised preview host to match the Agent's last observed mTLS peer address (`10.0.0.9`).
- A client cannot submit an arbitrary upstream preview URL; relay targets come only from active camera asset bindings and the latest authenticated Agent heartbeat.

## Relay and UI evidence

| Camera | Relay frame | Result |
|---|---:|---|
| OTOS WGC200 | 1920x1080 JPEG | PASS |
| RealSense D455 | 640x480 JPEG | PASS |
| RealSense D405 | 640x480 JPEG | PASS |

- All three Central relay endpoints returned `multipart/x-mixed-replace` and a complete JPEG frame.
- While one preview lease was active, a second camera request returned HTTP `429`.
- Closing the first stream released the lease and the next camera opened with HTTP `200`.
- The Admin robot detail page listed all three fresh sources and opened OTOS at its natural 1920x1080 resolution.
- No application console errors were observed. The Codex in-app Electron shell emitted only its generic development-mode CSP warning.
- Focused preview/deployment tests passed: `27 passed`.
- Full robot/camera/security/OPC UA/MCAP/Isaac regression passed: `356 passed`, with 14 dependency deprecation warnings.

## Operational notes

- OTOS remains optional in the RB5 health contract, so disconnecting the welding camera does not make RB5 and the two RealSense sources unavailable.
- The current Phase 3 Admin remains auth-disabled but loopback-only. Remote Admin access still requires the Operations v1 authenticated HTTPS profile or a separately approved management tunnel.
- During deployment, the Lab1 repository copy was older than the mTLS preview implementation. Six `robot_agent` modules were synchronized after taking `robot-agent-before-secure-preview-20260823.tar.gz`; the Agent then recovered and remained online.
- MCAP capture/upload and the unattended two-hour soak are separate follow-up gates.
