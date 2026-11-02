# Security

## Reporting a problem

Please report security problems privately through GitHub's **Report a vulnerability** button
(Security → Advisories) on this repository, not in a public issue.

## What Jolt Scan does and does not do

- No telemetry. The app makes only these network requests: YouTube downloads through yt-dlp when you paste a link,
  and the Tick Studio files and update check on GitLab.
- The server listens on 127.0.0.1 only. It refuses requests whose Host or Origin is not 127.0.0.1 or localhost, so
  other web pages cannot reach it.
- The Tick Studio files are pinned to one commit and checked by git blob hash before they are built.
- A scanner update is built and self-tested in a separate folder first. It replaces nothing if either step fails.
- Videos and scans stay in the `projects` folder on your PC.
