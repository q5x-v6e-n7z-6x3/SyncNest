import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';
import { SourceTextModule, SyntheticModule, createContext } from 'node:vm';

const source = await readFile(new URL('../web/app.js', import.meta.url), 'utf8');
const tick = () => new Promise(resolve => setImmediate(resolve));

async function player({ waitForReady = false, waitForAuthentication = false, failPlayer = false, live = false, play } = {}) {
  const elements = {
    video: { pause() {}, load() {}, removeAttribute() {}, play: async () => {} },
    status: {}, resume: {},
  };
  const timers = [], instances = [], requests = [];
  if (play) elements.video.play = play;
  let authorize, authenticate, rejectAuthentication, ready, playerLoaded = false;
  class Hls {
    static Events = { MANIFEST_PARSED: 'manifest', ERROR: 'error' };
    static isSupported() { return true; }
    constructor(config) { this.config = config; this.handlers = {}; instances.push(this); }
    on(event, handler) { this.handlers[event] = handler; }
    loadSource() {}
    attachMedia() {}
    destroy() { this.destroyed = true; }
  }
  class DiscordSDK {
    instanceId = 'instance';
    ready = () => waitForReady ? new Promise(resolve => { ready = resolve; }) : Promise.resolve();
    commands = {
      authorize: () => new Promise(resolve => { authorize = resolve; }),
      authenticate: () => waitForAuthentication ? new Promise((resolve, reject) => {
        authenticate = resolve; rejectAuthentication = reject;
      }) : Promise.resolve(),
    };
  }
  const context = createContext({
    document: { getElementById: id => elements[id], querySelector: () => ({ content: '100' }) },
    location: { search: '?frame_id=test' }, URLSearchParams, AbortController,
    setTimeout: (fn, delay) => { timers.push({ fn, delay }); }, setInterval() {},
    fetch: async (url, options) => {
      requests.push({ url, options });
      if (url.endsWith('/login')) return {
        ok: true, json: async () => ({ session: 'session', access_token: 'token', revision: 1,
                                      room: { id: 'room', status: 'playing', buffer_seconds: live ? 5 : 3 } }),
      };
      return new Promise((resolve, reject) => {
        options.signal.addEventListener('abort', () => reject(options.signal.reason), { once: true });
        requests.at(-1).resolve = (room, status = 200, invalidJson = false) => resolve({
          ok: status === 200, status,
          json: async () => {
            if (invalidJson) throw new SyntaxError('Unexpected token <');
            return { revision: 1, room, error: status === 200 ? undefined : 'Unavailable' };
          },
        });
      });
    },
  });
  const sdk = new SyntheticModule(['DiscordSDK'], function () {
    this.setExport('DiscordSDK', DiscordSDK);
  }, { context });
  const hls = new SyntheticModule(['default'], function () {
    playerLoaded = true;
    this.setExport('default', Hls);
  }, { context });
  await hls.link(() => {});
  const module = new SourceTextModule(source, {
    context,
    importModuleDynamically: async specifier => {
      assert.equal(specifier, 'hls.js/light');
      if (failPlayer) throw new Error('Player download failed.');
      await hls.evaluate();
      return hls;
    },
  });
  await module.link(specifier => {
    assert.equal(specifier, '@discord/embedded-app-sdk');
    return sdk;
  });
  await module.evaluate();
  await tick();
  return { elements, timers, instances, requests,
    playerLoaded: () => playerLoaded, ready: () => ready(),
    authenticate: () => authenticate(),
    rejectAuthentication: () => rejectAuthentication(new Error('Discord authentication failed.')),
    authorizationStarted: () => Boolean(authorize), authorize: () => authorize({ code: 'code' }) };
}

test('player downloads during the Discord handshake without authorizing early', async () => {
  const app = await player({ waitForReady: true });
  assert.equal(app.playerLoaded(), true);
  assert.equal(app.authorizationStarted(), false);
  assert.equal(app.requests.length, 0);
  app.ready();
  await tick();
  assert.equal(app.authorizationStarted(), true);
  app.authorize();
  await tick();
  assert.equal(app.instances.length, 1);
});

test('failed player download is handled while the Discord handshake waits', async () => {
  const app = await player({ waitForReady: true, failPlayer: true });
  assert.equal(app.elements.status.textContent, 'Player download failed.');
  assert.equal(app.authorizationStarted(), false);
  assert.equal(app.requests.length, 0);
});

test('player loads alongside authorization and sends one waiting state request', async () => {
  const app = await player();
  assert.equal(app.playerLoaded(), true);
  assert.equal(app.requests.length, 0); // Authorization is still waiting.
  app.authorize();
  await tick();
  assert.equal(app.instances.length, 1);
  assert.equal(app.requests.length, 2); // Login and one state request.
  assert.equal(app.requests[1].url, '/.proxy/api/state?revision=1');
  assert.equal(app.requests[1].options.headers['X-SyncNest-Session'], 'session');
  const headers = [];
  app.instances[0].config.xhrSetup({ setRequestHeader: (name, value) => headers.push([name, value]) });
  assert.deepEqual(headers, [['X-SyncNest-Session', 'session']]);
  assert.equal(app.timers.length, 0); // No overlapping polling while waiting.
});

test('fatal HLS error interrupts the wait and reconnects without a room change', async () => {
  const app = await player();
  app.authorize();
  await tick();
  app.instances[0].handlers.error(null, { fatal: true });
  await tick();
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.requests[1].options.signal.aborted, true);
  const retry = app.timers.shift();
  assert.equal(retry.delay, 1000);
  retry.fn();
  await tick();
  assert.equal(app.requests[2].url, '/.proxy/api/state?revision=undefined');
  app.requests[2].resolve({ id: 'room', status: 'playing' });
  await tick();
  assert.equal(app.instances.length, 2);
});

test('failed state request retries a fresh snapshot after a backoff', async () => {
  const app = await player();
  app.authorize();
  await tick();
  app.requests[1].resolve(null, 503);
  await tick();
  const retry = app.timers.shift();
  assert.equal(retry.delay, 1000);
  retry.fn();
  await tick();
  assert.equal(app.requests[2].url, '/.proxy/api/state?revision=undefined');
});

test('revoked access clears playback and stops state requests', async () => {
  const app = await player();
  app.authorize();
  await tick();
  app.requests[1].resolve(null, 403);
  await tick();
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.timers.length, 0);
});

for (const status of [401, 403]) {
  test(`HTML ${status} response revokes playback without retrying`, async () => {
    const app = await player();
    app.authorize();
    await tick();
    app.requests[1].resolve(null, status, true);
    await tick();
    assert.equal(app.instances[0].destroyed, true);
    assert.equal(app.timers.length, 0);
    assert.match(app.elements.status.textContent, /Reopen the Activity/);
  });
}

test('HTML proxy failure shows a useful message and retries after backoff', async () => {
  const app = await player();
  app.authorize();
  await tick();
  app.requests[1].resolve(null, 502, true);
  await tick();
  assert.equal(app.elements.status.textContent, 'Could not reach the video server.');
  assert.equal(app.timers[0].delay, 1000);
});

test('blocked autoplay offers a gesture and resumes sound on click', async () => {
  let attempts = 0;
  const app = await player({ play: async () => {
    if (++attempts === 1) throw Object.assign(new Error(), { name: 'NotAllowedError' });
  } });
  app.authorize();
  await tick();
  await app.instances[0].handlers.manifest();
  assert.equal(app.elements.resume.hidden, false);
  await app.elements.resume.onclick();
  assert.equal(attempts, 2);
  assert.equal(app.elements.resume.hidden, true);
});

test('a stopped room destroys playback and shows its status', async () => {
  const app = await player();
  app.authorize();
  await tick();
  app.requests[1].resolve({ id: 'room', status: 'stopped' });
  await tick();
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.elements.status.textContent, 'Stopped.');
});


test('video buffers during final authentication and plays only after it succeeds', async () => {
  let attempts = 0;
  const app = await player({ waitForAuthentication: true, play: async () => { attempts++; } });
  app.authorize();
  await tick();
  assert.equal(app.instances.length, 1);
  assert.equal(app.requests[1].url, '/.proxy/api/state?revision=1');
  await app.instances[0].handlers.manifest();
  await app.elements.resume.onclick();
  assert.equal(attempts, 0);
  app.authenticate();
  await tick();
  assert.equal(attempts, 1);
});

test('failed final authentication discards buffered video and cancels waiting state', async () => {
  let attempts = 0;
  const app = await player({ waitForAuthentication: true, play: async () => { attempts++; } });
  app.authorize();
  await tick();
  await app.instances[0].handlers.manifest();
  app.rejectAuthentication();
  await tick();
  assert.equal(attempts, 0);
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.requests[1].options.signal.aborted, true);
  assert.equal(app.timers.length, 0);
  assert.equal(app.elements.status.textContent, 'Discord authentication failed.');
});

test('revoked access while authenticating prevents playback after the handshake', async () => {
  let attempts = 0;
  const app = await player({ waitForAuthentication: true, play: async () => { attempts++; } });
  app.authorize();
  await tick();
  await app.instances[0].handlers.manifest();
  app.requests[1].resolve(null, 403);
  await tick();
  app.authenticate();
  await tick();
  assert.equal(attempts, 0);
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.timers.length, 0);
});

test('a stopped room while authenticating does not autoplay stale video', async () => {
  let attempts = 0;
  const app = await player({ waitForAuthentication: true, play: async () => { attempts++; } });
  app.authorize();
  await tick();
  await app.instances[0].handlers.manifest();
  app.requests[1].resolve({ id: 'room', status: 'stopped' });
  await tick();
  app.authenticate();
  await tick();
  assert.equal(attempts, 0);
  assert.equal(app.instances[0].destroyed, true);
  assert.equal(app.elements.status.textContent, 'Stopped.');
});


test('stall recovery can grow the safety buffer without delaying initial playback', async () => {
  const app = await player();
  app.authorize();
  await tick();
  const config = app.instances[0].config;
  assert.equal(config.liveSyncDuration, 3);
  assert.equal(config.liveSyncOnStallIncrease, 1);
  assert.ok(config.liveMaxLatencyDuration > config.liveSyncDuration);
});


test('live playback has enough safety buffer for multi-second source batches', async () => {
  const app = await player({ live: true });
  app.authorize();
  await tick();
  const config = app.instances[0].config;
  assert.equal(config.liveSyncDuration, 5);
  assert.equal(config.liveMaxLatencyDuration, 9);
});
