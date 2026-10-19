<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Snapshot file management: list, view, download (single or tar.gz), delete, purge.
$u = require_login();
$method = $_SERVER['REQUEST_METHOD'];

if ($method === 'GET' && isset($_GET['file'])) {
    $file = (string) $_GET['file'];
    $raw = valid_snapshot_file($file);
    $path = $raw;
    $dewarped = false;
    if (empty($_GET['raw'])) {
        $camName = preg_replace('/-\d{8}-\d{6}(-\d+)?\.jpg$/', '', $file);
        $cam = camera_config($camName);
        if ($cam) {
            $out = dewarped_capture($raw, $cam,
                !empty($_GET['thumb']) ? DEWARP_THUMB_WIDTH : DEWARP_FULL_WIDTH);
            $path = $out['path'];
            $dewarped = $out['dewarped'];
        }
    }
    header('Content-Type: image/jpeg');
    header('Content-Length: ' . filesize($path));
    header('X-Camview-Dewarp: ' . ($dewarped ? 'panorama' : 'raw'));
    if (!empty($_GET['download'])) {
        header('Content-Disposition: attachment; filename="' . capture_download_name($raw, $dewarped) . '"');
    }
    readfile($path);
    exit;
}

if ($method === 'GET') {
    $files = list_snapshots();
    json_out([
        'files' => $files,
        'total_size' => array_sum(array_column($files, 'size')),
    ]);
}

if ($method === 'POST' && ($_GET['action'] ?? '') === 'archive') {
    $b = body();
    $files = $b['files'] ?? [];
    $rawMode = !empty($b['raw']);
    if (!is_array($files) || !$files) json_err('no files selected');

    // Stage validated symlinks so tar sees friendly panorama filenames while
    // dereferencing them into actual JPEG data.
    $stage = sys_get_temp_dir() . '/camview-archive-' . bin2hex(random_bytes(8));
    if (!mkdir($stage, 0700, true)) json_err('cannot prepare archive', 500);
    $cleanup = function () use ($stage) {
        foreach (glob($stage . '/*') ?: [] as $f) @unlink($f);
        @rmdir($stage);
    };
    register_shutdown_function($cleanup);
    $seen = [];
    foreach ($files as $f) {
        $file = (string) $f;
        $raw = valid_snapshot_file($file);
        $out = ['path' => $raw, 'dewarped' => false];
        if (!$rawMode) {
            $camName = preg_replace('/-\d{8}-\d{6}(-\d+)?\.jpg$/', '', $file);
            $cam = camera_config($camName);
            if ($cam) $out = dewarped_capture($raw, $cam, DEWARP_FULL_WIDTH);
        }
        $name = capture_download_name($raw, $out['dewarped']);
        if (isset($seen[$name])) continue;
        $seen[$name] = true;
        if (!symlink($out['path'], $stage . '/' . $name)) {
            $cleanup();
            json_err('cannot prepare archive', 500);
        }
    }
    $cmd = 'tar czhf - -C ' . escapeshellarg($stage) . ' .';
    header('Content-Type: application/gzip');
    header('Content-Disposition: attachment; filename="snapshots.tar.gz"');
    passthru($cmd);
    $cleanup();
    exit;
}

if ($method === 'DELETE') {
    require_admin();
    check_csrf();
    $b = body();
    if (!empty($b['all'])) {  // purge everything
        $n = 0;
        foreach (glob(SNAPSHOTS_DIR . '/*.jpg') ?: [] as $f) {
            dewarp_forget($f);
            unlink($f);
            $n++;
        }
        json_out(['ok' => true, 'deleted' => $n]);
    }
    $files = $b['files'] ?? [];
    if (!is_array($files) || !$files) json_err('no files selected');
    $n = 0;
    foreach ($files as $f) {
        $path = valid_snapshot_file((string) $f);
        dewarp_forget($path);
        unlink($path);
        $n++;
    }
    json_out(['ok' => true, 'deleted' => $n]);
}

json_err('method not allowed', 405);
