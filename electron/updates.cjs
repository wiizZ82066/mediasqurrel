'use strict';

// Unsigned Windows releases: GitHub HTTPS + updater SHA512 integrity checks.
// No claim of Authenticode or Ed25519 publisher authentication is made.
function createUpdates({ updater, app, dialog, backend, stopBackend, resumeBackend }) {
  let state = { status: 'idle', version: app.getVersion(), nextVersion: '', percent: 0, message: '' };
  let ready = false;
  let busy = false;
  let installing = false;
  const set = patch => { state = { ...state, ...patch }; };
  const failure = error => set({ status: 'error', message: /sha512|checksum|hash/i.test(String(error.message || error))
    ? '更新文件校验失败，应用可继续使用，请稍后重试。'
    : '暂时无法完成更新，应用可继续使用，请稍后重试。' });
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = false;
  updater.allowPrerelease = false;
  updater.allowDowngrade = false;
  updater.on('checking-for-update', () => set({ status: 'checking', message: '正在检查更新…' }));
  updater.on('update-available', info => set({ status: 'downloading', nextVersion: info.version, percent: 0, message: '发现新版，正在下载…' }));
  updater.on('download-progress', progress => set({ status: 'downloading', percent: Math.round(progress.percent), message: '正在下载更新，现有任务继续运行' }));
  updater.on('update-not-available', () => set({ status: 'idle', message: '当前已是最新版本' }));
  updater.on('update-downloaded', info => {
    ready = true;
    set({ status: 'ready', nextVersion: info.version, percent: 100, message: '更新已准备好，可稍后安装' });
    backend('status').then(result => {
      if (result.active) set({ message: `有 ${result.active} 个任务正在运行或排队，完成后可更新。` });
    }).catch(() => set({ message: '更新已准备好；暂时无法确认任务状态，稍后重试安装。' }));
  });
  updater.on('error', error => {
    console.warn('[updater]', error.message);
    failure(error);
    if (installing) {
      installing = false;
      void resumeBackend().then(() => backend('release')).catch(e => console.error('[updater] recovery', e.message));
    }
  });

  async function check() {
    if (busy || installing || ['checking', 'downloading'].includes(state.status) || ready) return getState();
    busy = true;
    try { await updater.checkForUpdates(); } catch (error) { failure(error); }
    finally { busy = false; }
    return getState();
  }

  async function install() {
    if (!ready || busy || installing) return getState();
    busy = true;
    let stopped = false;
    try {
      const { response } = await dialog.showMessageBox({ type: 'question', buttons: ['稍后', '安装并重启'], defaultId: 0, cancelId: 0,
        message: `v${state.nextVersion} 已准备好`, detail: '安装前将检查下载任务；有任务运行时不会退出。' });
      if (response !== 1) return getState();
      const result = await backend('acquire');
      if (result.active > 0 || !result.locked) {
        set({ status: 'ready', message: `有 ${result.active} 个任务正在运行或排队，完成后可更新。` });
        return getState();
      }
      set({ status: 'installing', message: '正在安装更新并重启…' });
      await stopBackend();
      stopped = true;
      installing = true;
      updater.quitAndInstall(true, true);
    } catch (error) {
      installing = false;
      failure(error);
      if (stopped) await resumeBackend().catch(e => console.error(e));
      await backend('release').catch(() => {});
    } finally { busy = false; }
    return getState();
  }

  function getState() { return { ...state, canInstall: ready && !installing }; }
  return { check, install, getState };
}

module.exports = { createUpdates };
