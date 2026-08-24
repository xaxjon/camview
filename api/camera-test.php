<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Probe an RTSP URL and report what the camera actually offers (admin only).
require_admin();
check_csrf();

$source = trim((string) (body()['source'] ?? ''));
if (!str_starts_with($source, 'rtsp://')) json_err('source must be an rtsp:// URL');

$ffmpeg = CAMVIEW_ROOT . '/bin/ffmpeg';
if (!is_executable($ffmpeg)) json_err('bin/ffmpeg not found — run ./setup.sh', 500);

$cmd = 'timeout 15 ' . escapeshellarg($ffmpeg)
     . ' -hide_banner -rtsp_transport tcp -i ' . escapeshellarg($source)
     . ' -t 3 -f null - 2>&1';
$out = shell_exec($cmd) ?? '';

if (!preg_match('/Stream #.*Video: ([a-z0-9_]+).*?(\d{2,5}x\d{2,5})/i', $out, $vm)) {
    json_out(['ok' => false, 'error' => 'no video stream found — check URL, credentials and that the camera is reachable']);
}

$res = [
    'ok' => true,
    'video' => strtoupper($vm[1]) . ' ' . $vm[2],
    'audio' => null,
    'hint' => null,
];
if (preg_match('/Stream #.*Audio: ([a-z0-9_]+)(, (\d+) Hz)?/i', $out, $am)) {
    $codec = strtolower($am[1]);
    $rate = $am[3] ?? '';
    $res['audio'] = $codec . ($rate ? " {$rate} Hz" : '');
    if ($codec === 'aac') {
        $res['hint'] = 'AAC audio cannot play over WebRTC — tick "transcode audio" for sound';
    } elseif (!in_array($codec, ['pcm_mulaw', 'pcm_alaw'], true)) {
        $res['hint'] = "audio codec $codec may not play over WebRTC — tick \"transcode audio\" if silent";
    }
} else {
    $res['hint'] = 'no audio track';
}
json_out($res);
