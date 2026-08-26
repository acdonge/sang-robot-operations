# Automatic Edge Recording Upload Result - 2026-08-24

## Status

**PASS.** Finalized Processing PC sessions can now reach the Central immutable Object Store and SQLite catalog automatically without exposing the Admin API.

## Implemented Boundary

- Heartbeat remains on the dedicated Agent mTLS ingress at `8443`.
- Recording traffic uses a separate mTLS data ingress at `8444`, so multi-gigabyte uploads cannot occupy the heartbeat server process.
- Both paths authorize the same enrolled Agent certificate, exact `urn:sang:agent:<agent_id>` URI identity, enabled HTTPS profile, validity window, and revocation state.
- The recording ingress exposes only manifest registration, declared artifact upload, finalize, and mTLS health routes. It has no robot command or Admin configuration route.
- Processing captures are mounted read-only. Durable retry state is stored separately in `recording-upload.db`.
- Uploads resume Central-complete artifacts, stream pending files in bounded blocks, use exponential retry backoff, and stop revisiting sessions after a durable `COMPLETE` checkpoint.

## Verification

- Full source regression: **350 passed**.
- Isaac vendor profile/scene regression: **11 passed**.
- Central and Lab1 Compose graphs both rendered successfully.
- Temporary-PKI integration covered valid Agent upload/finalize, another-Agent rejection, credential revocation, exact file integrity, idempotent completion, profile filtering, retry, and restart persistence.
- Live Central endpoint `https://10.0.0.8:8444/healthz` returned `recording-mtls-ingress / ok` using the real `processing-lab1` client certificate.

## Live Lab1 Acceptance

- Agent: `processing-lab1`, online after deployment.
- Robot: `rb5_lab1_eval_01`, `READY`, sources fresh, rates healthy.
- Existing selected session: `rb5-lab1-observation-v2-20260823T192546Z-site10m`.
- Central catalog: `COMPLETE`, 3/3 artifacts, 5,394,131,765 bytes.
- Manifest SHA-256: `c29167c5f3b89b189596ec1a52dd799a7fedac74a2095fadd7d77f6e6e4159be`.
- Edge checkpoint after two deliberate uploader restarts: `COMPLETE`, `attempts=1`, no error.
- The session was already complete in Central, so acceptance intentionally proved idempotent discovery without re-hashing or retransmitting 5.39 GB.

## Safety and Remaining Acceptance

No robot command, vendor command channel, motion, welder, feeder, or arc interface was used. No new immutable live recording was created because that would add permanent data without explicit operator approval.

The next site acceptance should record one explicitly approved short observation segment and observe automatic first-time transfer while monitoring heartbeat continuity and bandwidth. Retention/deletion policy, abandoned `STAGING` cleanup, cloud Object Store support, LeRobot conversion, and Task Run-driven capture remain later work.
