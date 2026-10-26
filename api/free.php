<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

if ($_SERVER['REQUEST_METHOD'] !== 'GET') json_err('method not allowed', 405);

$enabled = false;
foreach (load_users() as $u) {
    if (($u['username'] ?? null) === 'free') {
        $enabled = true;
        break;
    }
}
if (!$enabled) json_err('public montage is not enabled', 404);

$status = mtx_path_status();
$out = [];
foreach (load_cameras() as $c) {
    if (!$c['enabled']) continue;
    $out[] = [
        'name' => $c['name'],
        'dewarp' => $c['dewarp'],
        'dewarp_offset' => $c['dewarp_offset'],
        'dewarp_circle' => $c['dewarp'] ? DEWARP_CIRCLE : null,
        'status' => $status[$c['name']] ?? 'standby',
    ];
}
json_out($out);
