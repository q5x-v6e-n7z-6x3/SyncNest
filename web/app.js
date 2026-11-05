import { DiscordSDK } from '@discord/embedded-app-sdk';

const video = document.getElementById('video');
const status = document.getElementById('status');
const resume = document.getElementById('resume');
const base = '/.proxy';
let session;
let hls;
let attachedRoom;
let lastStatus;
let revision;
let Hls;
let stateRequest;
let authenticated = false;
let manifestReady = false;

function message(text = '') {
  status.textContent = text;
  status.hidden = !text;
}

async function api(path, body, signal) {
  const response = await fetch(`${base}/api/${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json', 'X-SyncNest-Session': session || '' },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: 'no-store',
    signal,
  });
  const revoked = response.status === 401 || response.status === 403;
  // A proxy may return HTML, so revoke access before parsing its response.
  if (revoked) {
    session = null;
    detach();
  }
  let data;
  try {
    data = await response.json();
  } catch {
    throw new Error(revoked ? 'Your access expired. Reopen the Activity.'
      : 'Could not reach the video server.');
  }
  if (!response.ok) {
    throw new Error(data?.error || (revoked ? 'Your access expired. Reopen the Activity.'
      : 'Could not reach the video server.'));
  }
  return data;
}

function detach() {
  hls?.destroy();
  hls = null;
  attachedRoom = null;
  manifestReady = false;
  video.pause();
  video.removeAttribute('src');
  video.load();
  resume.hidden = true;
}

async function watch() {
  if (!authenticated || !session || !hls || !manifestReady) return;
  if (hls?.liveSyncPosition != null) video.currentTime = hls.liveSyncPosition;
  try {
    await video.play();
    resume.hidden = true;
    message();
  } catch (error) {
    // Some clients require a gesture before allowing automatic sound.
    if (error.name === 'NotAllowedError') {
      message();
      resume.hidden = false;
    } else if (error.name !== 'AbortError') {
      message('Could not play the video. Use /stop and try /play again.');
    }
  }
}

function attach(room) {
  detach();
  if (!Hls.isSupported()) {
    throw new Error('Use Discord desktop or Discord in Chrome/Edge to watch.');
  }
  attachedRoom = room.id;
  message('Loading video…');
  const buffer = room.buffer_seconds || 3;
  hls = new Hls({
    enableWorker: false,
    liveSyncDuration: buffer, liveMaxLatencyDuration: buffer + 4,
    maxLiveSyncPlaybackRate: 1.1, liveSyncOnStallIncrease: 1,
    backBufferLength: 8, maxBufferLength: 12,
    xhrSetup(xhr) { xhr.setRequestHeader('X-SyncNest-Session', session); },
  });
  hls.on(Hls.Events.MANIFEST_PARSED, () => { manifestReady = true; return watch(); });
  hls.on(Hls.Events.ERROR, (_, data) => {
    if (data.fatal) {
      detach();
      message('Video connection interrupted. Reconnecting…');
      // Reconnect even if the room's status has not changed.
      revision = undefined;
      stateRequest?.abort();
    }
  });
  hls.loadSource(`${base}/media/${room.id}/index.m3u8`);
  hls.attachMedia(video);
}

function render(room) {
  lastStatus = room?.status;
  if (['playing', 'ending'].includes(lastStatus)) {
    if (attachedRoom !== room.id) attach(room);
    else if (!video.paused && !video.ended) message();
  } else {
    if (attachedRoom) detach();
    message(room?.error || {
      loading: 'Loading video…', starting: 'Loading video…',
      stopped: 'Stopped.', finished: 'Video finished.', error: 'Could not play this video.',
    }[lastStatus] || 'Use /play in this voice channel’s chat.');
  }
}

async function poll() {
  if (!session) return;
  let delay = 0;
  stateRequest = new AbortController();
  try {
    const result = await api(`state?revision=${revision}`, undefined, stateRequest.signal);
    revision = result.revision;
    render(result.room);
  }
  catch (error) {
    // Retry with a fresh snapshot, with a short backoff for failed connections.
    revision = undefined;
    delay = 1000;
    if (error.name !== 'AbortError') message(error.message);
  }
  finally {
    stateRequest = null;
    // The server waits for changes and returns them immediately.
    if (session) setTimeout(poll, delay);
  }
}

resume.onclick = watch;
video.onplaying = () => { resume.hidden = true; message(); };
video.onended = () => message('Video finished.');

// Keep viewers near the same position after buffering or backgrounding.
setInterval(() => {
  const target = hls?.liveSyncPosition;
  if (lastStatus === 'playing' && !video.paused && target != null &&
      Math.abs(video.currentTime - target) > 2) video.currentTime = target;
}, 3000);

async function initialize() {
  if (!new URLSearchParams(location.search).has('frame_id')) {
    message('Open this Activity in Discord using /play.');
    return;
  }
  const client_id = document.querySelector('meta[name="discord-client-id"]').content;
  const sdk = new DiscordSDK(client_id);
  // Download the player during the SDK handshake as well as authorization.
  // Promise.all also handles a download failure while the handshake is pending.
  const [player, result] = await Promise.all([
    import('hls.js/light'),
    (async () => {
      await sdk.ready();
      const { code } = await sdk.commands.authorize({
        client_id, response_type: 'code', state: '', prompt: 'none', scope: ['identify'],
      });
      const login = await api('login', { code, instance_id: sdk.instanceId });
      return login;
    })(),
  ]);
  Hls = player.default;
  session = result.session;
  revision = result.revision;
  // Server-verified viewers can buffer video during Discord's final handshake.
  // Playback remains gated until the SDK confirms authentication.
  await Promise.all([
    sdk.commands.authenticate({ access_token: result.access_token }),
    (async () => { render(result.room); poll(); })(),
  ]);
  authenticated = Boolean(session);
  if (manifestReady) watch();
}
initialize().catch((error) => {
  session = null;
  authenticated = false;
  stateRequest?.abort();
  detach();
  message(error.message);
});
