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
  let retry = null;

  async function connect() {
    clearTimeout(retry);
    if (pc) pc.close();
    pc = new RTCPeerConnection();
    const current = pc;
    try {
      pc.addTransceiver('video', { direction: 'recvonly' });
      pc.ontrack = e => { video.srcObject = e.streams[0]; };
      pc.onconnectionstatechange = () => {
        if (pc === current && ['failed', 'disconnected'].includes(pc.connectionState)) {
          retry = setTimeout(connect, 3000);
        }
      };
      await pc.setLocalDescription(await pc.createOffer());
      await waitForIce(pc);
      const res = await fetch(`http://${location.hostname}:8889/${encodeURIComponent(name)}/whep`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/sdp' },
        body: pc.localDescription.sdp,
      });
      if (!res.ok) throw new Error(`WHEP ${res.status}`);
      await pc.setRemoteDescription({ type: 'answer', sdp: await res.text() });
    } catch {
      current.close();
      retry = setTimeout(connect, 3000);
    }
  }
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
