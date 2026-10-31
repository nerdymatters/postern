# Architecture

The React web app calls a FastAPI service on one macOS host. The API binds to loopback; an operator-managed HTTPS ingress and local Caddy proxy expose it. Exact browser origins are configured explicitly. The server's trust in proxy-derived addresses must match the documented local proxy boundary.

Google Identity supplies sign-in tokens. A separately shared read-only roster Sheet supplies membership and roles. Google Drive holds app-owned originals, portraits and sealed audit batches; an app-owned Google Sheet holds the confirmed catalog. OAuth access and roster credentials belong only on the API host.

SQLite owns local state and the durable audit queue. A single application process owns background jobs, in-memory admission controls and the writer. Running additional Uvicorn workers is not a supported scaling shortcut.

Documents and images are converted by a separate child process under the checked-in macOS sandbox profile and resource limits. Video preparation uses supported H.264/AAC inputs; it is not a general transcoding service. The selfie detector runs locally inside that same boundary.

Reader responses require a current authorized session, applicable agreements, optional enrollment and durable audit admission. Publication is visible only after cloud confirmation. Recovery hides content until an operator verifies the restored catalog. A failed cloud or permission refresh never enables demo mode.

`<data_dir>/training/` contains the installation's banks and captions; `<data_dir>/branding.json` contains its public operator/certificate settings. Neither is reconstructed from audit events. Configuration, credential files, databases, training and private backups never belong in a public checkout.

## Optional account service

An account-enabled frontend connects to a separate account/profile API as well
as the private resource API. The account API owns durable identities, profiles
and account sessions; the resource API retains its roster, protected sessions,
agreements, enrollment and training records. Account membership does not grant
private-resource access. Public profiles contain only explicitly published
presentation fields.

The browser matches the verified provider/subject of both sessions before
entering the private space. The resource API returns that identity only to its
authenticated session holder; it remains an identity description, not an access
grant. Demo sessions use a distinct issuer. Existing email-keyed activity and
certificate attribution are preserved. A missing identity on an older resource
API prevents account-enabled entry until the API is upgraded.

The hosted account implementation and database are maintained separately from
this open-source repository. Leaving the account API setting unset retains the
standalone reading-room flow. See [Accounts and profiles](accounts.md) for the
interface, configuration and rollout checks.

## Private Postern foundation

The [M2 design contract](private-spaces.md) defines responsible owners,
space-scoped roles, memberships, audience groups and resource grants. Native
metadata/session, onboarding, membership and ownership APIs are implemented and
verified in isolation; see the [service integration guide](runbooks/service-integration.md).
They are not enabled in production, and full content integration remains pending.
Restaurant/location groups remain reporting metadata. The
[implementation plan](designs/private-spaces.md) preserves the account/resource
boundary and legacy attribution, selects storage before catalog migration, and
requires direct API/media/cache/restore isolation checks before a second space
can share a backend.

## Limits

Watermarks cannot prevent screenshots. Playback events cannot prove attention. A selfie face check cannot prove identity or liveness. Cloud exports have a loss window for unexported activity. Certificates depend on the first passing exam event retained by those exports. The [backlog](../TODOS.md) and [capacity guide](runbooks/capacity.md) record known limits; no simultaneous-viewer capacity is guaranteed.
