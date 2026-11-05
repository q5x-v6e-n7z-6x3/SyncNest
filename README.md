# SyncNest

SyncNest plays shared videos and music in a Discord voice channel. Video opens in a Discord Activity; music plays through the bot's voice connection. Use `/play` with an attachment or public link. Video sources include YouTube, X, Instagram, TikTok, Vimeo, Dailymotion, Twitch, Internet Archive, Odysee, BitChute, Rumble, and Kick. YouTube Music and Spotify track links select music mode automatically. See the platform limits below before choosing a link.

The bot runs on an always-on Linux server. Viewers only need Discord, with no software to install on their devices. The Activity displays the video alongside voice chat; bot cameras and Go Live screen sharing are unsupported.

Python runs the bot and HTTP server, and yt-dlp finds the video and audio streams. FFmpeg prepares a small rolling HLS buffer for video or Opus audio for voice playback. The JavaScript video player runs in Discord's Activity iframe. Each voice channel has a mixed video/music queue. SyncNest has no playlist imports, stored platform accounts, database, or search command.

## Server requirements

- A 64-bit Ubuntu or Debian server (x86-64 or ARM64) with SSH access and Docker Engine with the Compose plugin. Follow [Docker's installation guide](https://docs.docker.com/engine/install/) for the server's distribution, then enable Docker at boot with `sudo systemctl enable --now docker`.
- A domain or subdomain, such as `watch.example.com`, with an A record pointing to the server's public IPv4 address. Add an AAAA record only if the server also has working public IPv6.
- Inbound TCP ports **80 and 443** open in the server/provider firewall. Keep SSH restricted to administrators. Port 8080 stays inside Docker and must not be published separately.
- Allow outgoing UDP for Discord voice connections when using music mode. Docker handles return traffic; no extra published port is needed.
- Start with two CPU cores and 4 GB RAM for one 720p stream. Higher resolutions and multiple streams need more CPU and upload bandwidth. Leave several gigabytes of disk space for container images and temporary playback buffers.

Run the setup commands on the server over SSH. The container build includes Python, FFmpeg, Deno, Node.js, and the player build. Keep the server running between sessions; viewers can turn off their computers when they finish watching. SyncNest needs a running server and cannot use static website hosting alone.

## Configure Discord

1. Open the [Developer Portal](https://discord.com/developers/applications) and create an application named **SyncNest**.
2. Under **Bot**, obtain the bot token. Under **OAuth2**, obtain the client secret and add `https://127.0.0.1` under **Redirects**. The SDK handles authorization inside Discord; this placeholder redirect is required. Keep both credentials private for the server configuration below.
3. Under **Activities → Settings**, enable Activities and desktop support.
4. Under **Installation**, enable **Guild Install** with the `bot` and `applications.commands` scopes. Install the application in your server.
5. Give the bot **View Channels**, **Send Messages**, and **Create Invite** permissions. Music also needs **Connect** and **Speak** in the voice channel. Check voice-channel and text-channel overrides. Viewers need **Use Activities**, **Use Application Commands**, and permission to join the voice channel.

The bot uses only the non-privileged **Guilds** and **Voice States** intents. Leave the privileged intents disabled. Video sound comes from the Activity player; music uses the bot's voice connection.

An unpublished Activity may be limited to its owner and development team. To let other people use it, configure the application's distribution and access settings as well as installing the bot. See Discord's [Activity setup guide](https://docs.discord.com/developers/activities/building-an-activity) and [distribution guidance](https://docs.discord.com/developers/activities/overview).

## Start on the server

Clone or upload this repository to the server. In the project directory:

```sh
cp -n .env.example .env
chmod 600 .env
nano .env
```

Set the three values:

```dotenv
DISCORD_TOKEN=your_discord_bot_token
DISCORD_CLIENT_SECRET=your_discord_client_secret
DOMAIN=watch.example.com
```

Replace the credential placeholders. Set `DOMAIN` to the server's hostname, without `https://`, a port, or a path. Keep `.env` private on the server; Git ignores it, and the image build excludes it. The production image contains the application and built player. Tests and development dependencies stay out of that image.

Two optional settings also go in `.env`:

- `DISCORD_PROXY_AUTH=true` accepts Activity logins only when Discord's proxy signed the request for your application. Requests sent straight to your domain are then refused before they use the shared login limit. Turn it on after the Activity works, restart, and reopen the Activity. If logins then fail with "Open SyncNest from Discord", Discord is not signing your app's proxy requests; set it back to `false`.
- `SYNCNEST_MEMORY_LIMIT` caps the bot container's memory (default `4g`). Four 4K streams use about 2.2 GB.

Start the services:

```sh
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
sudo docker compose logs --tail=100 syncnest caddy
```

Caddy obtains and renews the HTTPS certificate once DNS points to the server and ports 80/443 are reachable. It stores certificates in Docker volumes. The bot runs as a non-root user without Linux capabilities, on a read-only filesystem, with process and memory limits, and keeps its HTTP port inside the container network. Playback buffers and tool caches use a memory-backed `/tmp` that is cleared on restart. Rebuild with `docker compose build --pull` regularly so FFmpeg and other system packages receive security updates. Both services restart after a server reboot unless you explicitly stop them, and logs rotate to limit disk usage. See [Caddy's Docker guidance](https://caddyserver.com/docs/running#docker-compose).

Check the HTTPS endpoint, replacing the example hostname:

```sh
curl --fail https://watch.example.com/ --output /dev/null
```

Then open **Activities → URL Mappings** in the Developer Portal. Add prefix `/` with target `watch.example.com`, using the same hostname as `DOMAIN`. Omit the scheme and trailing slash. Enable **User Settings → Advanced → Developer Mode** in Discord while testing an unpublished Activity.

The `syncnest` health check confirms that the HTTP server responds. Check the logs for the Discord connection too, then test playback in Discord. This setup needs no tunnel or bot running on your computer.

## Watch in Discord

Run `/help` in any server text channel for a private guide to playback, supported platforms, joining the Activity, and stopping. Help works before you join voice. Restart the bot after updating to register its commands with Discord.

1. Join a server voice channel using Discord desktop or Discord in Chrome/Edge.
2. Open that voice channel's text chat and run `/play url:https://www.youtube.com/watch?v=aqz-KE-bpKQ` (or a video link from another supported platform). For a video on your computer, use `/play file:<video>` instead and select the file.
3. Discord opens the Activity and starts the video and audio after loading. Friends join the same Activity through Discord. There is no player form or Start button.
4. If the client blocks automatic sound, click the video overlay once to allow playback.
5. Use `/play` again to queue another item, `/skip` to advance, or `/stop` to stop playback and pause the remaining queue.

If you send `/play` in a separate text channel, follow the **Join video player** link to open the Activity in your voice channel. People who already have it open receive new videos automatically. Each voice channel has one active item. Other viewers must open the Activity themselves, and Discord may ask them to authorize or join it.

### Video and music queue

Videos and music share one queue per voice channel and play in the order added. `/play` starts immediately when idle; while something is playing or the queue is paused, it adds the link or attachment and reports its position. Mode and video resolution are saved with each item. YouTube Music, Spotify tracks, and audio attachments still select music automatically.

| Command | Effect |
| --- | --- |
| `/queue` | Show the current item and the first 10 waiting items. Use `/queue page:2` or `page:3` for later entries. |
| `/queue delete:2` | Delete waiting item 2. Use `delete:1,3,5` for several items or `delete:2-4` for a range. Lists and ranges can be combined, such as `delete:1,3-5`. |
| `/skip` | Stop the current item and advance to the next one. |
| `/stop` | End the current item and disconnect music. Keep the remaining items paused. The stopped item is removed. |
| `/resume` | Start the next waiting item in a paused queue. It does not resume the stopped item's playback position. |
| `/queue clear:true` | Remove waiting items without interrupting current playback. |

Deletion uses the waiting positions shown by `/queue`, including their numbers on later pages. The current item is separate; use `/skip` to remove it. Positions update as playback advances or items are deleted, so check `/queue` before deleting. Duplicate or overlapping selections delete each item once. If any position is invalid, the whole deletion is rejected. Deleting from a paused queue keeps it paused; use `/resume` to continue. Use either `delete` or `clear` in one command.

Anyone in the voice channel can add items and use `/resume`. Only the person who added the current item can `/skip`, `/stop`, or change its `/resolution` while they are in the channel; members with **Move Members** can always do so, and anyone can once that person leaves. `/queue delete` follows the same rule per item, and `/queue clear` keeps items added by people who are still present. Other channels have independent queues. Each channel holds up to 25 waiting items, with up to 100 waiting items per server; an item still loading reserves one of those places. Queues stay in memory and are lost on restart. Deleting a channel or removing the bot from a server discards the affected queues. Playlist and album links are not expanded into queue entries; add individual items.

When an item finishes, the next one starts after cleanup. Videos leave time for their final buffered segments to play. Live streams keep their turn until they end or someone skips them. Unavailable or expired sources report an error in the voice channel's text chat and are skipped. A queued video's start message includes a link to open its Activity; people who already have the Activity open receive it automatically. A queued Spotify track's start message labels the actual YouTube source.

If a queued item cannot start because the bot is reconnecting, media workers are full, voice permissions are missing, the channel is empty, or another channel is using the bot's music connection, the queue pauses and keeps that item at the front. Use `/resume` after resolving the issue. Leaving the channel empty or moving/removing the music bot also pauses the remaining queue. Rejoining does not resume it automatically.

Only the current item resolves its platform media URLs and starts an encoder. Waiting items do not download or consume media workers. Discord attachment links can expire while waiting; attach the file again if that happens.

### Music in voice

Use `/play mode:music url:<link>` to play only the audio from a supported source. The same bot joins your voice channel and plays the track without opening an Activity. YouTube Music track links and Spotify track links select music mode automatically. Use `/play mode:video url:<link>` when you want a video Activity instead; Spotify links require music mode.

There is one active item per channel. Video and music can follow each other in the queue. The bot can play music in one voice channel per server at a time. An idle second channel gets a busy message; if its queue reaches a music item while another channel is using voice, it pauses until someone uses `/resume` after that connection is free. Music ends and the bot disconnects when the track finishes, someone skips or stops it, or the last human leaves. Moving or removing the bot also stops its music session and pauses the queue.

Spotify links identify a track and search YouTube for a close match using the artist, title, and duration. The reply labels the Spotify track and links to the YouTube source actually playing. Audio comes from YouTube, without a Spotify login or account cookies. Albums, playlists, episodes, and `spotify.link` short links are unsupported; use an `open.spotify.com/track/ID` link. If public metadata or a close match is unavailable, supply a YouTube Music link yourself. A match may differ from Spotify's recording.

`/resolution` applies only to video. Music uses stereo Opus audio at 128 kbps. Restart after updating to register the mode option, and rebuild the server image to install voice support.

### Video resolution

Every new `/play url:<video link>` starts at 720p unless you choose a resolution. Use `/resolution quality:1080p` to switch to 1080p or `/resolution quality:720p` to switch back. Pick the quality from Discord's dropdown. Anyone in the voice channel can change it for everyone; only the person running the command sees its confirmation.

The current video keeps playing while the new quality loads. Once the replacement has buffered three seconds of a recording or five seconds of a live stream, everyone's player briefly reloads. If loading fails, the current stream keeps playing and the command reports the error. A quality change keeps the same queue item; it does not advance the queue.

Recordings resume near the shared playback position, estimated from the rolling HLS buffer. Different amounts of buffering make this approximate. Live streams reconnect at the live edge. Selecting the current quality leaves playback running. Using `/stop` during a quality change cancels the replacement too.

Both `/resolution`'s quality dropdown and `/play`'s optional resolution dropdown list 144p, 280p, 360p, 480p, 720p, 1080p, 1440p, 4K (2160p), and Highest possible, from lowest to highest. A numbered choice sets the maximum height. SyncNest selects the best available rendition within that limit, or asks FFmpeg to scale down a larger or unknown-size stream. It never enlarges a lower-resolution source.

Highest possible keeps the highest source resolution, including 4K or 8K when available, with no fixed ceiling. It cannot add detail to the original video. Some platforms provide only one rendition.

Choose a lower resolution if the server cannot encode fast enough or its connection cannot keep up. Higher resolutions use more CPU and upload bandwidth. All output is H.264 at 30 fps, without lossless playback or HDR preservation. Restart the bot after updating so Discord registers `/resolution` and its dropdown.

### Local files

Use `/play file:<video>` to attach an MP4, MOV, M4V, MKV, WebM, or AVI from your computer. Choose one source, either `file` or `url`, and optionally set `resolution`. Uploaded videos support `/resolution` and `/stop` too. Restart SyncNest after updating so Discord registers the attachment option.

Audio attachments in MP3, M4A, AAC, WAV, FLAC, OGG, or Opus select music mode automatically. You can also use `/play mode:music file:<video>` to play a video's audio without showing its picture. Both kinds of attachment support `/stop`; audio playback requires an audio track.

Discord hosts the attachment, and [Discord's upload allowance](https://docs.discord.com/developers/reference#uploading-files) applies. SyncNest streams the file directly and deletes its temporary playback buffer when stopped. [Attachment URLs can expire](https://docs.discord.com/developers/reference#signed-attachment-cdn-urls); attach the file again if it expires or is removed. Local filesystem paths and playlists are not accepted.

## Supported links

| Platform | Accepted links |
| --- | --- |
| YouTube | Individual watch, youtu.be, Shorts, `/live/VIDEO_ID`, and embed links. Live-channel links at `/@HANDLE/live`, `/channel/CHANNEL_ID/live`, `/c/NAME/live`, or `/user/NAME/live` select the current broadcast. |
| YouTube Music | Individual `music.youtube.com/watch?v=VIDEO_ID` track links. These play the public YouTube source in music mode by default; login-only tracks are unavailable. |
| Spotify | Individual `open.spotify.com/track/ID` links, including region-prefixed links. Music mode finds a public YouTube match and labels that source. This does not stream Spotify audio. |
| X / Twitter | Native video posts at `/USER/status/ID` (also `/i/status/ID` and `/i/web/status/ID`) and public live broadcasts at `/i/broadcasts/ID`. Posts containing an X broadcast also work. For posts with multiple videos, append `/video/N` to choose one. Both `x.com` and `twitter.com` work. External link cards, Spaces, event pages, and profiles are unsupported; use an external video's original supported-platform URL. |
| Instagram | Public video posts at `/p/CODE/`, reels at `/reel/CODE/` or `/reels/CODE/`, and older `/tv/CODE/` links. Stories, profiles, and posts containing multiple videos are unsupported. |
| TikTok | Public videos at `/@USER/video/ID`, `/share/video/ID`, or `/embed/ID`; live broadcasts at `/@USER/live` or `m.tiktok.com/share/live/ID`. Open short share links (`vm.tiktok.com`, `vt.tiktok.com`, or `/t/CODE`) in a browser and use the full video/live URL. Profiles, collections, photo posts, and Douyin are unsupported. |
| Vimeo | Individual video links, unlisted links with a hash, player embeds (including `?h=`), and individual videos within channels, groups, or showcases. |
| Dailymotion | Individual `/video/` links, `/embed/video/` links, and `dai.ly` short links. |
| Twitch | Individual `/videos/` VODs, `clips.twitch.tv` or channel `/clip/` links, and channel URLs for live streams. |
| Internet Archive | `/details/` or `/embed/` video items, `/details/ITEM/FILENAME` for a specific entry, or `/download/ITEM/FILENAME.mp4`. Files must be MP4, MOV, M4V, MKV, WebM, AVI, OGV, MPG/MPEG, or TS; playlists and other containers are rejected. |
| Odysee | Individual `/@CHANNEL:CLAIM/VIDEO:CLAIM` or `/VIDEO:CLAIM` links, plus `/$/embed/VIDEO/CLAIM` and `/$/download/VIDEO/CLAIM` links. Claim hashes and signed-link query parameters are preserved. |
| BitChute | Individual `/video/ID` and `/embed/ID` links, including links from `old.bitchute.com`. |
| Rumble | Individual `/vID-title.html` or `/vID` links and `/embed/ID` links (including publisher-prefixed embed IDs). |
| Kick | Live `/CHANNEL` links, saved streams at `/CHANNEL/videos/UUID`, and individual `/CHANNEL/clips/clip_ID` or `/CHANNEL?clip=clip_ID` links. |

SyncNest uses public links without account cookies, browser logins, or platform credentials. Some links still require login, especially on Instagram and TikTok, and cannot play here. Facebook videos/live and Instagram Live are unsupported. TikTok video/live routing and live API handling have offline regression tests, but a particular link may still be unavailable without login.

Use a link to one video. For an Internet Archive item with several videos, choose one file; SyncNest will not queue the rest. Silent videos can play without an audio track.

YouTube and TikTok live-channel links and Twitch or Kick channels need an active broadcast. Otherwise, use a recording or clip. Odysee and Rumble live streams use individual video links when yt-dlp can extract them.

Channel lists, playlists, searches, platform homepages, arbitrary websites, and private, password-protected, subscriber-only, region-blocked, or DRM-protected content are unsupported. Audio-only items require music mode. Platform extraction changes over time. Keep yt-dlp current and test the links you plan to watch.

## How playback works

### Finding the source

`/play` checks that you are in voice and have supplied one supported URL or attachment. For attachments, it checks the type and size metadata, Discord CDN URL, and file extension. An idle video launches the Activity when run in the voice channel's chat, or supplies an invite when run elsewhere. Activity links are server invites, so the bot only creates one for people who have **Create Invite** in that voice channel; others open the Activity from the voice channel. Busy or paused channels retain the validated source in their queue.

yt-dlp finds fresh video and audio URLs in a background thread without downloading the whole video. It prefers resolution over codec: a higher-resolution VP9 stream can beat a lower-resolution AVC stream. At the same resolution it prefers SDR H.264 video and stereo AAC audio, which need no tone mapping, decode cheaply, and let the audio be copied. Only supported-platform extractors and generic CDN redirects are loaded.

yt-dlp, FFmpeg, and the Archive reader reach the internet through a loopback proxy inside the bot. It resolves each destination itself and connects only to public addresses, so a redirect, playlist entry, or DNS answer cannot make the server contact loopback services, private networks, or cloud metadata endpoints. Discord's own connections bypass it. yt-dlp's built-in cache can reuse extraction helper data between requests.

Discord attachments skip site extraction. Direct Archive file links go straight to FFmpeg, with the selected resolution still applied. Attachments, Archive files, and Odysee originals use fixed media containers and restricted input protocols to block disguised playlists from making other requests or reading host files. Odysee originals share Archive's container allowlist, and Odysee extraction uses the public metadata endpoint used by its web player.

During quality changes, a room can reuse fresh recording formats for up to two minutes. Live and expired sources always refresh. If a cached URL fails, SyncNest tries one fresh extraction.

### Encoding and buffering

FFmpeg starts as soon as extraction finishes. Known video/audio codecs and Archive MP4/MOV/MKV/WebM files use a one-second, 1 MB inspection window to shorten startup; unknown raw media keeps full probing.

Recordings fill the first five seconds of buffer quickly, then read at playback speed. Builds with `readrate_catchup` can refill after a stall at 1.5 times real time; older FFmpeg builds keep regular pacing. Live streams follow their source timestamps without an extra input throttle. Kick clip timestamp jumps are corrected to avoid artificial pauses.

Large Archive MP4/MOV/MKV/WebM files use four parallel HTTP range reads of up to 4 MiB each. A private loopback reader delivers them in order and refills its four-request window before waiting for FFmpeg to consume a block. Downloads can therefore continue while decoding or playback slows down. Prefetch stays bounded, seeking cancels obsolete reads, and the full video is never stored. Small files and servers without usable byte ranges keep direct/background FFmpeg reads.

Transient HTTP 500/502/503/504/429 errors and connection failures get retries with a capped exponential delay. HTTP 403/404 errors and normal EOF do not.

FFmpeg keeps the source dimensions in Highest possible mode or scales down to the chosen height. It never upscales. High-frame-rate video drops to 30 fps before resizing, so discarded frames need no scaling work. Encoding uses four threads at 1080p and above, or two for lower caps, and produces one-second H.264 HLS segments. Compatible AAC-LC audio is copied directly. Unknown audio, other codecs, and audio outside the channel/rate/bitrate limits convert to stereo AAC. Old segments are deleted, leaving roughly 20 seconds of media.

### Player and access

The Activity uses Discord's SDK for authorization. Python exchanges the authorization code before making a bot-authenticated Activity lookup, then checks the viewer's identity, Activity membership, and voice channel. The server allows 20 login attempts per 10 seconds across all viewers; with `DISCORD_PROXY_AUTH=true`, unsigned requests are refused before they count. Each viewer session may make about eight player requests per second with short bursts, hold two status polls at once, and each person keeps at most three sessions, so one viewer cannot monopolize the server. Requests from a closed connection stop immediately. If Discord rate-limits authentication, further calls pause for its requested delay, and retry responses keep the `Retry-After` header.

The hls.js player fetches the authenticated stream through Discord's proxy. It follows a shared position about three seconds behind recorded output or five seconds behind live output. Buffering can temporarily put viewers out of sync. After a stall, the player can add a second of safety buffer to avoid stalling again at the same margin.

The browser downloads the player library during the SDK handshake and authorization. It can then buffer server-authorized video during the SDK's final authentication request, but playback waits for authentication to succeed. Player assets and hashed chunks are cached and precompressed with Brotli and gzip. Authentication and playback state are never cached.

Status requests wait up to 20 seconds for a change. Loading, readiness, stopping, and errors wake viewers immediately; unchanged playback needs about three status requests per minute per viewer. While a request waits, the server checks voice membership and session expiry every second. Commands and media requests pause during a Discord gateway outage until voice membership has synchronized again.

When a video ends, its last segments stay briefly so viewers can finish. Stopping terminates FFmpeg immediately. Leaving voice revokes access, and an empty channel stops playback. Finished or stopped rooms release their buffer, cached formats, and media URLs, keeping only a small status snapshot for the player.

Video audio comes from the Activity, without a separate bot voice connection. Mobile and Safari video playback are unsupported. Music uses Discord voice directly.

### Native music

Music shares the extraction-job and overall media-worker limits with video. It selects an audio source and sends FFmpeg-encoded Opus packets through Discord's voice client. Attachment and original-file decoder restrictions apply to music too. The bot joins self-deafened and does not listen to other participants. Playback starts only once audio packets are available. Stopping cancels loading, terminates the encoder, and disconnects voice; cancelling a command does not interrupt cleanup.

The playback coordinator owns queue advancement separately from the video and music workers. It waits for cleanup before starting another item and follows replacement encoders during video quality changes. Stop and skip share a protected cleanup task so a cancelled Discord command cannot interrupt disconnection or start another item early.

## Managing the server

Run these commands from the project directory:

```sh
sudo docker compose logs --tail=100 -f syncnest
sudo docker compose restart syncnest
sudo docker compose down
```

`restart` interrupts current playback. `down` stops the services and removes their containers; it keeps the certificate volumes. Do not add `--volumes` unless you intend to delete those certificates. After changing `.env`, use `sudo docker compose up -d` so the containers receive the new settings.

To update the code and dependencies, first retain the current bot image:

```sh
sudo docker image tag syncnest:local syncnest:previous
git pull --ff-only
sudo docker compose build --pull --no-cache syncnest
sudo docker compose pull caddy
sudo docker compose up -d
sudo docker compose ps
```

Rebuilding installs current compatible Python dependencies, including yt-dlp. The JavaScript dependencies follow `package-lock.json`. To restore the previous bot image with its original dependencies:

```sh
sudo docker image tag syncnest:previous syncnest:local
sudo docker compose up -d --no-build syncnest
```

If an update changed deployment configuration, restore its previous Git revision too. The saved image restores the bot; manage Caddy separately and keep `.env` local. Updates and restarts stop playback and discard in-memory queues. Add sources with `/play` again afterward.

Run one copy of the bot. Each process allows four starting or active media workers across video and music, including replacements during quality changes, and two outstanding extraction jobs. When those slots are full, it rejects new work. A rejected quality change leaves the current video playing.

Stopping or timing out a load cannot terminate its extraction thread, which keeps its slot until it finishes. The limits are set by `MAX_STREAMS` and `MAX_EXTRACTIONS` in `src/syncnest/stream.py`.

Budget for FFmpeg's CPU use and the server's upload bandwidth. A platform may also block your hosting provider's IP even when the same link works locally. A free VM may not provide uninterrupted playback.

## Troubleshooting and checks

- Could not open the Activity: confirm Activities are enabled and the bot can create invites in that voice channel.
- Blank player or unavailable app: check `sudo docker compose ps`, the URL mapping, HTTPS, desktop support, Developer Mode, and access to the unpublished Activity.
- Authorization failure: check that both credentials belong to the same application and the OAuth2 redirect is saved. Join the voice channel, then reopen the Activity.
- Login rate limit: wait for the reported retry delay, then reopen the Activity. All viewers share the login allowance.
- HTTPS certificate failure: check DNS (including any AAAA record), inbound TCP 80/443, and `sudo docker compose logs --tail=100 caddy`. The hostname must be publicly reachable.
- Platform 403 or extraction failure: rebuild and start using the update commands above, then retry `/play` with the original platform link. For YouTube, check the bundled runtime with `sudo docker compose exec syncnest deno --version`. Do not reuse a temporary `googlevideo.com` link. Private, age-restricted, region-blocked, or otherwise unavailable videos may not work.
- Kick browser impersonation unavailable: rebuild the server image. Check that `sudo docker compose exec syncnest python -m yt_dlp --list-impersonate-targets` lists browser targets from `curl_cffi`. Kick may still reject requests or restrict specific content.
- Internet Archive item contains multiple videos: choose one file from the item's download options and use its `/download/ITEM/FILENAME` link.
- FFmpeg failure: read `sudo docker compose logs --tail=100 syncnest`. Check the bundled binary with `sudo docker compose exec syncnest ffmpeg -version`.
- Slow startup: check the logged extraction time and time to the first segment. Platform extraction, HTTPS, and Discord authorization can all add delay. Status changes return immediately; they do not wait for a polling interval. Player downloads overlap the SDK handshake and authorization, and authorized video buffers during final authentication. Brotli reduces asset downloads, with gzip available for other clients.
- No sound: click **Click to play with sound** and check that Discord's Activity audio is not muted.
- Music cannot connect or is silent: check the bot's Connect/Speak permissions, outgoing UDP, and whether it is server-muted. Rebuild the image after updating so the voice dependencies are installed. Only one channel per server can use music at a time.
- No Spotify match: use a direct YouTube Music link. Spotify metadata can be unavailable, and the matcher rejects candidates with incompatible titles, artists, versions, or durations.

Run the offline regression suite in a separate build stage without starting the bot or using Discord credentials:

```sh
sudo docker build --target tests .
```

Frontend tests run during the player build. The optional `tests` stage also runs the Python suite with the bundled FFmpeg.

The offline suite checks platform URLs, Activity authentication and voice access, channel isolation, attachment and Archive input restrictions, HTTP recovery, process cleanup, quality changes, encoded dimensions, and player reconnects. Music tests cover Spotify metadata and matching, native voice lifecycle, permissions, stop races, shared limits, and real HTTP-to-Opus encoding. Queue tests cover mixed ordering, pause/resume, skip, clear, capacity, channel isolation, failed sources, cancellation, and quality replacement. Frontend tests simulate the browser and Discord SDK, and platform extraction tests use fixtures. The component benchmark generates temporary media and runs a localhost HTTP server.

For a live check, have two people join the same voice channel and Activity. Play an anonymously available link from each platform. Check sound and approximate synchronization, stop and restart playback, then leave voice to check that access is cut off. Test recordings and live streams separately on YouTube, X, TikTok, Twitch, and Kick, including a quality change during each. Offline tests cannot confirm which links your network can reach or whether Discord will grant participants access.

Test music separately in Discord voice: try a YouTube Music link, a Spotify match, and an audio attachment. Confirm the bot joins, plays sound, rejects another channel's request, and disconnects after `/stop` and when everyone leaves. Source extraction and Opus encoding alone do not verify Discord voice transport.

Queue a video, an audio track, and another video in that order. Check automatic mode changes and the Activity join message. Stop midway, confirm the remaining items stay paused, then resume. Also test skip, deleting one or several waiting items, clearing waiting items, a failed link, and a quality change with a non-empty queue. Restart once to confirm the queue is cleared.

## Repository layout

```text
src/syncnest/
  bot.py                 Discord commands and server startup
  activity.py            Activity authentication, status, and media delivery
  stream.py              URL validation, extraction, encoding, and cleanup
  archive_reader.py      Parallel Archive reads and seeking
  egress.py              Public-only proxy for media and platform requests
  music.py               Native voice playback and connection cleanup
  music_sources.py       Public Spotify metadata and YouTube matching
  playback.py            Per-channel mixed queues and media lifecycle coordination
  queue_commands.py      Queue controls, Activity links, and start announcements
tests/                   Python regressions and player tests
benchmarks/              Offline startup and optional public-platform checks
scripts/build.mjs        Frontend build and asset compression
web/                     Player HTML, CSS, JavaScript, and generated bundle
deploy/Caddyfile         HTTPS proxy configuration
Dockerfile               Player build, runtime, and optional test stage
compose.yaml             Server services, health check, and certificate storage
requirements.txt         Python dependencies
package.json             Frontend dependencies and build/test commands
.env.example             Hostname and credential placeholders
.dockerignore            Files allowed into the image build
.github/workflows/       Offline test suite on every push and pull request
SECURITY.md              Vulnerability reporting
```

The container starts the Python package with `python -m syncnest.bot` and sets its import path to `/app/src`. The production image excludes tests and benchmarks.

For development checks with Python dependencies installed and player assets built, run these commands from the repository root:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m unittest discover -v
npm test
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python -m benchmarks.benchmark_speed
```

The offline benchmark needs FFmpeg. To check public links, run `PYTHONPATH=src python -m benchmarks.benchmark_platforms`; reports default to `benchmarks/results/`, which Git ignores. Both benchmark scripts use temporary playback buffers. The public-link benchmark makes real network requests and never connects the bot to Discord.

## Contributing

Issues and pull requests are welcome. Before opening a pull request, run the offline suite with `docker build --target tests .`, or with the development commands above. GitHub Actions runs the same suite on every push and pull request. Add a regression test for any bug you fix.

Report security problems privately, as described in [SECURITY.md](SECURITY.md).

## License

SyncNest is available under the [MIT License](LICENSE).
