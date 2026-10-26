'use strict';

const position = Number(document.body.dataset.camera) - 1;

function showMessage(message) {
  const text = document.createElement('p');
  text.id = 'empty';
  text.textContent = message;
  document.body.replaceChildren(text);
}

function waitForIce(pc) {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise(resolve => {
    pc.addEventListener('icegatheringstatechange', () => {
      if (pc.iceGatheringState === 'complete') resolve();
    });
    setTimeout(resolve, 2000);
  });
}

function watch(name) {
  const video = document.createElement('video');
  video.id = 'camera';
  video.muted = true;
  video.autoplay = true;
  video.playsInline = true;
  video.title = name;
  document.body.appendChild(video);
  let pc = null;
  let sessionUrl = null;
  let retry = null;
  let disconnectTimer = null;
  let stopped = false;

  function deleteSession(url, keepalive = false) {
    if (!url) return;
    fetch(url, { method: 'DELETE', keepalive }).catch(() => {});
  }

  function closeCurrent(keepalive = false) {
    clearTimeout(disconnectTimer);
    const oldPc = pc;
    const oldSession = sessionUrl;
    pc = null;
    sessionUrl = null;
    if (oldPc) {
      oldPc.onconnectionstatechange = null;
      oldPc.close();
    }
    video.srcObject = null;
    deleteSession(oldSession, keepalive);
  }

  function scheduleConnect(delay = 3000) {
    clearTimeout(retry);
    if (!stopped) retry = setTimeout(connect, delay);
  }

  async function connect() {
    if (stopped) return;
    clearTimeout(retry);
    closeCurrent();
    const current = new RTCPeerConnection();
    pc = current;
    try {
      current.addTransceiver('video', { direction: 'recvonly' });
      current.ontrack = e => {
        if (pc === current) video.srcObject = e.streams[0];
      };
      current.onconnectionstatechange = () => {
        if (pc !== current || stopped) return;
        if (current.connectionState === 'connected') {
          clearTimeout(disconnectTimer);
        } else if (current.connectionState === 'failed') {
          scheduleConnect();
        } else if (current.connectionState === 'disconnected') {
          // ICE disconnections are often transient; do not multiply sessions.
          clearTimeout(disconnectTimer);
          disconnectTimer = setTimeout(() => {
            if (pc === current && current.connectionState === 'disconnected') {
              scheduleConnect();
            }
          }, 10000);
        }
      };
      await current.setLocalDescription(await current.createOffer());
      await waitForIce(current);
      const res = await fetch(`http://${location.hostname}:8889/${encodeURIComponent(name)}/whep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: current.localDescription.sdp,
      });
      if (!res.ok) throw new Error(`WHEP ${res.status}`);
      const locationHeader = res.headers.get('Location');
      if (!locationHeader) throw new Error('WHEP response missing Location');
      const newSessionUrl = new URL(locationHeader, res.url).href;
      if (pc !== current || stopped) {
        current.close();
        deleteSession(newSessionUrl);
        return;
      }
      sessionUrl = newSessionUrl;
      await current.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    } catch {
      if (pc === current) closeCurrent();
      else current.close();
      scheduleConnect();
    }
  }

  window.addEventListener('pagehide', () => {
    stopped = true;
    clearTimeout(retry);
    closeCurrent(true);
  }, { once: true });
  connect();
}

async function init() {
  const res = await fetch('api/free.php', { cache: 'no-store' });
  const cameras = await res.json();
  if (!res.ok) { showMessage('Public camera view is not enabled'); return; }
  if (!cameras[position]) { showMessage('Camera not available'); return; }
  watch(cameras[position].name);
}
init().catch(() => showMessage('Camera service unavailable'));
