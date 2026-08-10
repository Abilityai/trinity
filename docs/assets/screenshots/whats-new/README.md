# Release-note screenshots — frozen, never refresh

Each `v<version>/` directory holds the screenshots embedded in that release's
`docs/user-docs/whats-new/<version>.md`.

**These are point-in-time snapshots and must never be updated.** A release note describes
what shipped in that version; a current screenshot inside it is simply wrong. They are
deliberately excluded from the canonical store at [`docs/screenshots/`](../../../screenshots/)
and from its manifest and freshness check.

When cutting a release, `/generate-user-docs` collects fresh images into a **new**
`v<version>/` directory. Never overwrite an existing one.
