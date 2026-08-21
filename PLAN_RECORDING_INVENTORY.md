# Phase 3.6 - Read-only Recording Inventory

## Goal

Expose bounded metadata for local MCAP capture sessions from each Processing
Agent to the Central admin UI without transferring raw recordings.

## Scope

- Scan only immediate session directories under the configured capture root.
- Read at most 1,000 small `session-manifest.json` files and report at most the
  20 most recent session summaries.
- Report complete, failed, partial, and invalid session counts, declared MCAP
  size, topic/message counts, transport loss, and Task Run references.
- Persist the inventory only as part of the latest Agent heartbeat snapshot.
- Display fleet totals and per-Agent/per-robot recent sessions in Admin.

## Non-Goals

- No MCAP, image, video, checksum, recorder log, or local path transfer.
- No remote start, stop, retry, upload, download, retention, or delete action.
- No claim that a `.partial` directory proves a recorder process is active.
- No LeRobot conversion or central Object Storage catalog.

## Safety And Compatibility

1. The scanner never follows capture-root, session-directory, or manifest
   symlinks.
2. Malformed, oversized, cross-Agent, or mismatched manifests are isolated as
   invalid inventory entries and cannot stop the Robot Agent.
3. Existing heartbeat payloads remain valid through an additive default.
4. Central must be upgraded before an Edge begins sending the new field because
   older Central models reject unknown fields.
5. Admin remains read-only and receives metadata only.

## Acceptance Criteria

- Unit tests cover complete, failed, partial, malformed, symlink, cross-Agent,
  bounded-recent, missing-root, and cache behavior.
- Central validates and round-trips the additive inventory while accepting old
  heartbeat fixtures.
- Fleet summary and Admin show recording counts and recent session details.
- `processing-lab1` reports the existing accepted and failed RB5 sessions with
  no raw recording transfer and no restart of camera or robot observation
  services.

## Validation Result

**COMPLETE (2026-08-06).** Lab1 reports one complete and one failed session,
73,216,896 declared bytes, and no raw recording fields. The one-time Fable
review initially rejected identifier-length, timestamp-order, and duplicate
session collision handling. Those three contract gaps were fixed and covered
by scanner-to-Central round-trip tests; the focused Opus follow-up returned
`PASS` with no critical findings. The Agent keeps `/var/lib/sang` writable for
ROS logs while mounting only `/var/lib/sang/captures` read-only. Edge
provisioning now creates that directory with the configured UID/GID before
Compose and refuses implicit root-owned bind-directory creation.
