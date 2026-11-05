# Security policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub: open the repository's **Security** tab and choose **Report a vulnerability**. Do not open a public issue, pull request, or discussion for a security problem.

Include the affected commit, the steps or link that reproduce the problem, and what an attacker gains. A minimal proof of concept helps. Do not test against servers you do not run.

Only the latest commit on `main` receives fixes.

## Scope

In scope:

- Bypassing Activity authorization, or watching or controlling a voice channel you are not in.
- Making the bot, yt-dlp, or FFmpeg reach a private, loopback, or link-local address, despite the egress proxy.
- Reading files, credentials, or environment variables from the server.
- Bypassing the queue control rules or the per-session request limits.
- Escaping the container restrictions in `compose.yaml`.

Out of scope:

- Vulnerabilities in yt-dlp, FFmpeg, discord.py, Deno, or Discord itself. Report them to those projects. If SyncNest needs a dependency update or a workaround, report that here.
- Platforms refusing or rate-limiting extraction.
- Load from members who are legitimately in the voice channel, within the documented limits.
