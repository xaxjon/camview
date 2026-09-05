<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Snapshots from configured cameras. Sources are read server-side; clients
// can only use configured cameras, never arbitrary URLs.
//   GET  ?preview=<name>  -> one JPEG frame, NOT saved (hover previews)
//   POST {name}           -> save one frame to snapshots/ (tile 📷 button)

require_login();

function find_camera(string $name): ?array {
    foreach (load_cameras() as $c) {
        if ($c['name'] === $name && $c['enabled']) return $c;
    }
    return null;
}

function grab_frame(string $source): ?string {
    $ffmpeg = CAMVIEW_ROOT . '/bin/ffmpeg';
    if (!is_executable($ffmpeg)) json_err('bin/ffmpeg not found — run ./setup.sh', 500);
    $cmd = 'timeout 12 ' . escapeshellarg($ffmpeg)
         . ' -hide_banner -loglevel error -rtsp_transport tcp -i ' . escapeshellarg($source)
         . ' -frames:v 1 -f mjpeg - 2>/dev/null';
    $jpeg = shell_exec($cmd);
    return ($jpeg && strlen($jpeg) > 1000) ? $jpeg : null;
}

if ($_SERVER['REQUEST_METHOD'] === 'GET') {
    $cam = find_camera((string) ($_GET['preview'] ?? ''));
    if (!$cam) json_err('camera not found', 404);
    $jpeg = grab_frame($cam['source']);
    if (!$jpeg) json_err('could not grab a frame (camera offline?)', 502);
    header('Content-Type: image/jpeg');
    header('Content-Length: ' . strlen($jpeg));
    header('Cache-Control: no-store');
    echo $jpeg;
    exit;
}

check_csrf();

$cam = find_camera((string) (body()['name'] ?? ''));
if (!$cam) json_err('camera not found', 404);
$jpeg = grab_frame($cam['source']);
if (!$jpeg) json_err('could not grab a frame (camera offline?)', 502);

$path = snapshot_path($cam['name']);
if (file_put_contents($path, $jpeg) === false) {
    json_err('cannot write snapshot — check web server permissions', 500);
}
prune_snapshots();

json_out(['ok' => true, 'file' => basename($path)]);
