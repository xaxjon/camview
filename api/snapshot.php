<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Save one JPEG frame from a configured camera into snapshots/.
// Source is read server-side; clients can only snapshot configured cameras.

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
         . ' -hide_banner -loglevel error -timeout 8000000 -rtsp_transport tcp -i ' . escapeshellarg($source)
         . ' -frames:v 1 -f mjpeg - 2>/dev/null';
    $jpeg = shell_exec($cmd);
    return ($jpeg && strlen($jpeg) > 1000) ? $jpeg : null;
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
