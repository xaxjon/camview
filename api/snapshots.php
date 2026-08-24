<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Snapshot file management: list, view, download (single or tar.gz), delete, purge.
$u = require_login();
$method = $_SERVER['REQUEST_METHOD'];

if ($method === 'GET' && isset($_GET['file'])) {
    $path = valid_snapshot_file((string) $_GET['file']);
    header('Content-Type: image/jpeg');
    header('Content-Length: ' . filesize($path));
    if (!empty($_GET['download'])) {
        header('Content-Disposition: attachment; filename="' . basename($path) . '"');
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
    // bulk download as tar.gz (built with system tar; names are whitelist-validated)
    $files = body()['files'] ?? [];
    if (!is_array($files) || !$files) json_err('no files selected');
    $names = [];
    foreach ($files as $f) { valid_snapshot_file((string) $f); $names[] = escapeshellarg((string) $f); }
    $cmd = 'cd ' . escapeshellarg(SNAPSHOTS_DIR) . ' && tar czf - ' . implode(' ', $names);
    header('Content-Type: application/gzip');
    header('Content-Disposition: attachment; filename="snapshots.tar.gz"');
    passthru($cmd);
    exit;
}

if ($method === 'DELETE') {
    require_admin();
    check_csrf();
    $b = body();
    if (!empty($b['all'])) {  // purge everything
        $n = 0;
        foreach (glob(SNAPSHOTS_DIR . '/*.jpg') ?: [] as $f) { unlink($f); $n++; }
        json_out(['ok' => true, 'deleted' => $n]);
    }
    $files = $b['files'] ?? [];
    if (!is_array($files) || !$files) json_err('no files selected');
    $n = 0;
    foreach ($files as $f) { unlink(valid_snapshot_file((string) $f)); $n++; }
    json_out(['ok' => true, 'deleted' => $n]);
}

json_err('method not allowed', 405);
