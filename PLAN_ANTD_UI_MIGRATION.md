# PLAN - Ant Design UI migration

Goal: Replace every first-party browser surface with one React + Ant Design system while preserving each backend, authentication, and safety contract.

Done when:
- The root admin route is built from React and Ant Design, not hand-styled DOM code.
- Fleet summary, filtering, agent selection, robot detail routing, telemetry, events, recordings, and on-demand camera preview still work.
- Config, Catalog, Login, and Operator routes remain available.
- No robot, policy, welding, or vendor command endpoint is introduced.
- The production static build is served by the existing FastAPI process and passes browser verification.
- Config, Catalog, Login, and Operator use the same component tokens, navigation language, and responsive behavior.
- Repository instructions and a durable UI standard guide future screen development.

Out of scope:
- Changing ROS 2, OPC UA, recording, database, or authentication contracts.
- Adding any actuator command path.

## Visual thesis

An Ant Design operations console: bright neutral working surface, navy navigation, blue primary actions, and restrained semantic status color.

## Content plan

1. Persistent application navigation and connection state.
2. Fleet summary and compact storage/sync facts.
3. Searchable agent table with a selected-agent context panel.
4. Robot dossier with camera, telemetry, host, recordings, and retained events.

## Interaction thesis

- Live data reconciles in place without page-level flashes.
- Row selection reveals context without navigating away; robot links retain hash routes.
- Camera bytes are fetched only after an explicit preview action and only one stream runs at a time.

## Milestones

- [x] M1: Create the Vite + React + Ant Design build boundary - verified with `npm run build`.
- [x] M2: Port Fleet and selected-agent monitoring - verified against live summary/agent APIs and Agent row selection.
- [x] M3: Port robot details and on-demand MJPEG preview - verified on the RB5 route with a live frame followed by a clean stop.
- [x] M4: Update safety/UI tests and deploy static assets - verified with 11 focused tests, live camera start/stop, responsive browser checks, and a clean browser console.
- [x] M5: Extract the shared application shell, API client, tokens, and multi-page build - verified all five HTML entries and shared generated assets.
- [x] M6: Migrate Config and Login without changing metadata or authentication contracts - verified registry data, a non-submitted Node form, and the login form in-browser.
- [x] M7: Migrate Catalog and synchronized MCAP playback - verified session detail, three simultaneous cameras, timeline play/pause, and original JPEG frame requests.
- [x] M8: Migrate Operator while preserving idempotent capture recovery and observation-only camera preview - verified a completed run, disabled capture controls, one-stream preview behavior, and the absence of actuator endpoints.
- [x] M9: Record the durable UI standard and run all static, safety, and browser checks - verified desktop and 390 px layouts, English copy, navigation, TypeScript, build, and 11 UI tests.

## Decisions log

- 2026-08-27: Use the official React implementation of Ant Design instead of copying its CSS appearance.
- 2026-08-27: Build into `ui-admin/` with `emptyOutDir: false` so legacy Config, Catalog, and Login pages remain available during staged migration.
- 2026-08-27: Preserve the existing GET/WebSocket-only Fleet boundary and on-demand camera transport.
- 2026-08-27: Use one Vite multi-page build so all surfaces share Ant Design, while retaining `/operator/` as a distinct server mount.
- 2026-08-27: Treat `docs/UI_DESIGN_STANDARD.md` as the mandatory source for future browser design, interaction, responsive, and safety rules.
- 2026-08-27: Remove the superseded hand-authored page scripts and styles after all routes pass the shared React build.

## Current state

M1-M9 are complete. Fleet, Robot Detail, Operations Setup, Data Catalog, Login, and Operator Station now use the same React + Ant Design system without changing backend or actuator safety contracts.
