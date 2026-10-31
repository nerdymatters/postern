# Selfie enrollment and isolated preview

Enrollment is optional and disabled by default. It requires explicit activity, IP and selfie consent; use an isolated fixture environment for fault/camera tests. The helper `scripts.selfie_preview` can create dedicated private preview storage using an operator-authorized host credential. It performs cloud writes to that preview storage; read its help and isolation checks before running.

```sh
.venv/bin/python -m scripts.selfie_preview prepare --host-config /path/to/private/config.json
VITE_POSTERN_API='' bun run --cwd dashboard/web build --outDir ../../.context/selfie-preview/web
.venv/bin/python -m scripts.selfie_preview serve
```

The preview binds to loopback and uses synthetic reader/admin identities. Keep it isolated from production folders, catalog, data and ports. If a consenting person uses a phone, provide an operator-controlled HTTPS/password gate; never expose demo sign-in directly. Stop only the verified preview processes when finished. No real-phone acceptance is implied by included fixtures.

## Intellectual property agreement

Both entry checkboxes are required by the API, including when selfie enrollment is disabled. Acceptance records the activity notice version, IP agreement version and both explicit confirmations in the audit log, plus the session's IP acceptance timestamp. An existing session without the current IP acceptance returns to **Before you start**. Valid selfie enrollments are preserved; accepting the new agreement does not itself require another photo.

The IP text is `ip_notice` in private `branding.json`; `ip_notice_version` defaults to `ip-1`. Increment the version whenever that text changes so existing sessions must review the revision. Release the matching frontend/API together so the new acceptance fields are available. Refresh older tabs. Sessions created before browser binding was enabled must sign in with Google again before camera enrollment; content and progress are preserved. Administrators enroll before reading training materials or retrieving portrait images; admin management itself does not require enrollment in 0.5.0.

## Enrollment and retention

- An enrollment belongs to an email and random browser identifier, with a 30-day validity period. The identifier is stored locally and bound to the session on sign-in. It is a convenience binding, not hardware attestation; clearing browser storage or changing browsers requires another photo.
- The camera opens only after explicit consent and a button press. Tracks stop after capture, cancellation or leaving. Upload occurs only after **Use this photo**. No face matching, liveness check or identity verification is performed.
- New and replacement selfies require one clear face. The server checks locally in the restricted converter with the bundled [YuNet model](../../postern/models/README.md), before creating a pending enrollment or allocating a cloud file. Blank/object images, multiple faces, small faces and faces outside the saved frame receive retake guidance. Rejected input and temporary derivatives are deleted; rejected replacements leave the current selfie active. Detector failures return a retryable 503 and cannot bypass enrollment. No landmarks, confidence scores or face templates are retained. A printed/screen face can pass: this is face presence detection, not liveness.
- Existing enrollments keep their original expiry and do not need to retake at rollout. Keep `selfie_notice_version` unchanged for this rollout; the revised capture/privacy wording describes the new check for new photos. A user can still cancel a replacement with **Keep current selfie**.
- The API limits input to 1 MiB, decodes in the existing restricted image converter, strips metadata, and saves a 256×256 JPEG. Only a checksum-confirmed private Drive upload becomes active. Failed cloud writes retry; sign-out/revocation cancels pending enrollment. Kept captures and photos that could not be prepared, including malformed images, are limited to ten per account in a rolling 24-hour window. When the release record is saved, a face the check did not accept counts only toward 30 attempts in that window, and a server fault (detector, converter host, private storage or disk) counts toward neither. A delayed release keeps the attempt counted until durable reconciliation succeeds; all reservations count toward 90 in that window. See the [reservation lifecycle](capacity.md#durable-selfie-reservations) for interrupted requests and restarts. Each account prepares one photo at a time; different people's photos prepare in parallel, up to the configured converter slots (two by default). Wait for a pending save to retry during a storage incident instead of repeatedly submitting. A dropped save response is reconciled against server state before another upload.
- Replacement retires the previous selfie for that email/browser. Expiry or confirmed offboarding also retires a photo. Old photos are retained for the configured retention period after retirement (90 days by default), then deleted locally and permanently from their dedicated Drive folder. Non-image audit references remain. Failed deletion retries with exponential backoff capped at one hour and an admin-visible warning; access ends at the deadline regardless of deletion availability.
- Readers can retrieve only their current enrollment portrait. Enrolled admins may view retained historical portraits. Every access uses the existing session, allowlist and audit guards, and rechecks authorization after slow work. Native video tickets also enforce selfie validity.
- The small portrait appears beside the existing email/time footer outside PDF/photo content and in the video player overlay. The video overlay is not baked into downloads. Audit events include the selfie ID; activity reports associate the first recorded content open with its retained photo.
- Recovery rebuilds confirmed enrollment metadata from private audit exports. Consent-only records without confirmation are quarantined and unreadable until deletion at their original expiry plus the configured retention period; missing confirmation is not proof that enrollment failed. Recovery does not restore sessions. Portraits are fetched privately from Drive only while retained. Cached portraits do not wait behind uploads. A missing portrait needed for document/photo rendering fails closed with a retry screen; optional report/video portraits may show a grey placeholder during a storage error while the video email/time overlay remains.

## Operator configuration and camera trouble

The [release guide](release.md) covers private configuration, backups and coordinated deployment. Keep the dedicated owner-only
`selfie_folder_id` stable for retained photos; changing folders requires an explicit
migration. Retention defaults to 90 days (`selfie_retention_days`), the selfie notice
to `selfie-1` (`selfie_notice_version`), and the IP notice to `ip-1` (`ip_notice_version`).

If the camera is unavailable, use HTTPS on a camera-equipped phone and grant camera
permission after accepting the notice. In 0.5.0, admins can instead choose
**Continue to admin without a selfie** after the two entry agreements to manage
posts and review staff progress. This does not create an enrollment or allow reading
training materials or portrait images; those remain gated when enrollment is enabled.
Readers have no skip option. Operators can temporarily set
`selfie_enabled` to `false` and restart the API during an incident; both entry
agreements remain required. This pauses photo cleanup, so restore the feature
promptly. Do not fabricate production readers or override an individual's consent.

Install the updated Python dependencies from `requirements-pilot.lock` before
deploying face checking, and include `postern/models/` in the installed package.
`.venv/bin/python -P -m postern.serve install --check-only --config <config>` verifies the model
hash, runs a CPU inference, and runs the detector once more inside the converter sandbox as part
of its preflight checks, so a profile or limit that only breaks it there fails preflight. Deploy
the matching frontend for retake guidance. The detector does not need a network
download, external API key or additional cloud service. Keep the existing private
selfie folder and configured retention settings. During a detector incident, existing
enrollments still work; new captures retry instead of accepting unchecked images.

## Verification

Run `.venv/bin/python -m pytest tests -q`, `bun run --cwd dashboard/web test`, and the preview build command above. Enrollment tests cover consent, cloud failure, browser/account isolation, expiry, private reads, revocation during writes/reads, metadata stripping, retention retries, recovery and video-ticket expiry. Face tests use a [public-domain NASA portrait](../../tests/fixtures/README.md) and constructed negatives: blank/object/group/tiny/cropped-out faces, rotated and mirrored portraits, unavailable models, and legacy enrollment preservation. Frontend tests cover camera denial, late permission cleanup, retake, explicit confirmation, pending storage, face-check failures, and the content gate. Real-phone checks should include a normal selfie, a nonface object, retaking after rejection and cancelling a replacement; the small fixture set does not establish accuracy across every face, skin tone or camera.

Screenshots and detailed test output are in `.context/selfie-preview/`. These artifacts contain only synthetic test identities/images unless a human explicitly captures a real selfie in the private preview.
