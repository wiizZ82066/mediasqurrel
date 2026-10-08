'use strict';
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createUpdates } = require('../updates.cjs');

function fixture(overrides = {}) {
  const updater = new EventEmitter();
  const calls = [];
  updater.checkForUpdates = async () => { calls.push('check'); };
  updater.quitAndInstall = (silent, restart) => calls.push(['install', silent, restart]);
  const controller = createUpdates({ updater, app: { getVersion: () => '1.1.1' },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    backend: async action => { calls.push(action); return { active: 0, locked: action === 'acquire' }; },
    stopBackend: async () => { calls.push('stop'); }, resumeBackend: async () => { calls.push('resume'); },
    ...overrides,
  });
  return { updater, controller, calls };
}
const ready = f => { f.updater.emit('update-downloaded', { version: '1.2.0' }); };

test('downloading/ready never installs on exit or interrupts tasks', async () => {
  const f = fixture({ backend: async () => ({ active: 2, locked: false }) });
  assert.equal(f.updater.autoInstallOnAppQuit, false);
  assert.equal(f.updater.allowDowngrade, false);
  ready(f);
  await new Promise(setImmediate);
  assert.match(f.controller.getState().message, /2/);
  await f.controller.install();
  assert.deepEqual(f.calls, []);
});
test('later does not acquire a lock, stop backend or install', async () => {
  const f = fixture({ dialog: { showMessageBox: async () => ({ response: 0 }) } });
  ready(f);
  await f.controller.install();
  assert.deepEqual(f.calls, ['status']);
});
test('backend failure fails closed and keeps the app running', async () => {
  const f = fixture({ backend: async () => { throw new Error('timeout'); } });
  ready(f);
  await f.controller.install();
  assert.equal(f.controller.getState().status, 'error');
  assert.deepEqual(f.calls, []);
});
test('idle installation waits for process-tree shutdown and rejects double clicks', async () => {
  let unblock;
  const f = fixture({ stopBackend: () => new Promise(resolve => { unblock = resolve; }) });
  ready(f);
  const install = f.controller.install();
  await new Promise(setImmediate);
  await f.controller.install();
  assert.equal(f.calls.some(Array.isArray), false);
  unblock();
  await install;
  assert.deepEqual(f.calls, ['status', 'acquire', ['install', true, true]]);
});
test('shutdown failure unlocks queue and does not install', async () => {
  const f = fixture({ stopBackend: async () => { throw new Error('cannot stop'); } });
  ready(f);
  await f.controller.install();
  assert.equal(f.controller.getState().status, 'error');
  assert.deepEqual(f.calls, ['status', 'acquire', 'release']);
});
test('installer error recovers backend instead of stranding the app', async () => {
  const f = fixture();
  f.updater.quitAndInstall = () => f.updater.emit('error', new Error('installer failed'));
  ready(f);
  await f.controller.install();
  await new Promise(setImmediate);
  assert.deepEqual(f.calls, ['status', 'acquire', 'stop', 'resume', 'release']);
  assert.equal(f.controller.getState().status, 'error');
});
test('provider/download failures stay visible and permit retry', async () => {
  const f = fixture();
  f.updater.checkForUpdates = async () => { throw new Error('missing latest.yml'); };
  await f.controller.check();
  assert.match(f.controller.getState().message, /missing latest.yml/);
  f.updater.checkForUpdates = async () => { f.calls.push('retry'); };
  await f.controller.check();
  assert.deepEqual(f.calls, ['retry']);
});
