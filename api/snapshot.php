<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// One JPEG frame from a configured camera. Source is read server-side;
// clients can only snapshot configured cameras, never arbitrary URLs.
require_login();

$name = (string) ($_GET['name'] ?? '');
$cam = null;
foreach (load_cameras() as $c) {
    if ($c['name'] === $name && $c['enabled']) { $cam = $c; break; }
}
if (!$cam) json_err('camera not found', 404);

$ffmpeg = CAMVIEW_ROOT . '/bin/ffmpeg';
if (!is_executable($ffmpeg)) json_err('bin/ffmpeg not found — run ./setup.sh', 500);

$cmd = 'timeout 12 ' . escapeshellarg($ffmpeg)
     . ' -hide_banner -loglevel error -rtsp_transport tcp -i ' . escapeshellarg($cam['source'])
     . ' -frames:v 1 -f mjpeg - 2>/dev/null';
$jpeg = shell_exec($cmd);

if (!$jpeg || strlen($jpeg) < 1000) json_err('could not grab a frame (camera offline?)', 502);

header('Content-Type: image/jpeg');
header('Content-Length: ' . strlen($jpeg));
echo $jpeg;
