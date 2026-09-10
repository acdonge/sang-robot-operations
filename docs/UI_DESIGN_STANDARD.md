# SANG Web UI Design Standard

Status: mandatory for every first-party browser surface in this repository.

## Purpose

SANG uses one React and Ant Design operations-console language across Central Admin and the
Processing/Operator surface. New screens must extend this system instead of creating a parallel
HTML, CSS, or component style. The UI must remain useful for mixed robot fleets while making each
screen's safety and write boundary visible.

## Source and build boundary

- Source lives in `frontend/admin/` and uses React, TypeScript, Ant Design, and Vite.
- `ui-admin/` and `ui-operator/` are generated deployment artifacts. Do not hand-edit them.
- One Vite multi-page build produces all routes and shared chunks under `/antd-assets/`.
- Run `cd frontend/admin && npm run build` after every UI change.
- Register a new browser entry in `frontend/admin/vite.config.ts`; do not add standalone scripts,
  inline style blocks, a CDN build of Ant Design, or another frontend toolchain.

| Surface | Entry source | Served route | Allowed writes |
| --- | --- | --- | --- |
| Fleet and robot detail | `src/main.tsx` | `/` | None; GET and WebSocket observation only |
| Operations setup | `src/config-main.tsx` | `/config.html` | Versioned configuration metadata only |
| Data catalog | `src/catalog-main.tsx` | `/catalog.html` | None; immutable recording and derivation lookup only |
| Login | `src/login-main.tsx` | `/login.html` | Authentication request only |
| Operator station | `src/operator-main.tsx` | `/operator/` | Bounded capture start/stop and run completion only |

No surface may add a robot, vendor SDK, policy, welder, feeder, ROS publish, service, or action
command path without a separate safety architecture and review.

## Visual language

- Use `UiProvider` and the tokens in `src/theme.tsx` as the single theme source.
- Use a bright neutral workspace (`#f4f6f8`), navy navigation (`#001529`), blue primary actions
  (`#1677ff`), and Ant semantic colors for success, warning, and error.
- Typography uses the configured Ubuntu/Avenir/Segoe stack. Do not introduce a second page-specific
  font system.
- Use restrained 8 px radii, white information surfaces, compact tables, and generous page spacing.
- Status color must carry one meaning: green for healthy/pass, amber for degraded/in progress, red
  for failed/unsafe, blue for selected/active, and neutral gray for unavailable or historical.
- Interface copy is English. Identifiers, ROS topics, hashes, and timestamps use code styling.

## Page anatomy

1. Wrap the entry in `UiProvider`.
2. Use `AppShell` for navigation, connection status, and the explicit safety boundary.
3. Use `PageHeader` for the uppercase context label, page title, one-sentence purpose, and primary
   page actions.
4. Place content in `page-stack`; use Ant `Card`, `Table`, `Descriptions`, `Alert`, `Tag`, `Modal`,
   and responsive `Row`/`Col` primitives before creating custom equivalents.
5. Put shared behavior in `src/components/`, shared transport in `src/api.ts`, and shared contracts
   in typed modules. Do not duplicate camera or request implementations between pages.

## Interaction rules

- Reconcile heartbeat, WebSocket, and polling updates in place. Never reload or replace the whole
  page for live data, and do not animate every refresh.
- Preserve stable row keys and selection so status updates do not flash or reset context.
- Camera transport is on demand. Fetch bytes only after an explicit action, stop cleanly, and keep
  live preview to one stream per browser unless a reviewed requirement changes the limit.
- Recorded playback may show at most the backend-declared camera count and must revoke temporary
  object URLs when frames, sessions, or views change.
- Destructive metadata actions require confirmation and a reason where the backend contract expects
  one. Execution state changes must show their exact transition.
- Every loading, empty, error, stale, disabled, and disconnected state must be legible without
  relying on color alone.
- Keep keyboard focus, accessible names, table labels, and meaningful image alt text. Avoid clickable
  containers when a Button or link communicates the action better.

## Responsive behavior

- Desktop uses the persistent 248 px navigation rail; narrow layouts use the shared navigation
  drawer.
- Tables may scroll inside their own container, but the document must not create horizontal page
  overflow.
- Summary grids, filters, camera views, forms, and detail columns must collapse deliberately at the
  existing breakpoints in `src/styles.css`.
- Verify at least one desktop and one mobile viewport before considering a screen complete.

## Definition of done

For every UI change:

1. Run `npm run typecheck` and `npm run build` in `frontend/admin/`.
2. Run `src/fleet_control_plane/test/test_admin_ui.py` in the canonical container.
3. Open every affected route through the existing FastAPI server and verify content, navigation,
   responsive overflow, and browser console errors.
4. Exercise only actions allowed by the table above. Hardware or actuator commands are never part
   of UI visual verification.
5. Update this document when a deliberate, reviewed design-system decision changes.
