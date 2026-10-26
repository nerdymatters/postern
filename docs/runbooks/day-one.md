# Synthetic feasibility tools

`scripts/day1/` preserves small synthetic experiments for page stamping, audit latency, host load, cache behavior and remote request measurements. They are development tools, not the production authentication, publication or recovery implementation.

Inspect each command's `--help` and use generated non-confidential fixtures on an isolated host. The remote runner reads a configured probe URL and private probe credentials; it must not target an ordinary production API or real staff catalog. Reports belong outside Git under ignored `reports/day1/`.

The optional manual GitHub workflow uses repository variable `POSTERN_PROBE_URL` and secret `POSTERN_PROBE_CREDENTIALS`; it is not configured by default. Configure it only for an explicitly isolated fixture server. Do not publish measured operator endpoints, identities or private reports.

For current application-path rehearsals use the [capacity guide](capacity.md). Passing a synthetic test does not establish external-network performance, camera support, accessibility or simultaneous-viewer capacity. Real-device acceptance remains a separate check.
