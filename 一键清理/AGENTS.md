# Project working rules

- This is a Windows-first, local-only desktop file organizer. Do not add cloud services, analytics, telemetry, or network calls.
- Never scan outside the folder pair explicitly selected by the user. Never follow symlinks, junctions, or other reparse points.
- Never delete user files in the MVP. File moves must have a preview, explicit confirmation, collision-safe destinations, and a recorded undo path.
- Do not overwrite an existing destination. A failed or unverified cross-volume copy must leave the source intact.
- Keep the SQLite database, settings, backups, test fixtures, and user files outside Git. Do not commit credentials or personal sample content.
- Before implementing filesystem behavior, add or update focused tests. Run the test suite and a launch/import check before each milestone commit.
- Explain changes in plain language and include a short manual verification step for the user at each milestone.
