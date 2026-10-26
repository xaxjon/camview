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
if (!$enabled) json_err('public mosaic is not enabled', 404);

$out = [];
foreach (load_cameras() as $c) {
    if ($c['enabled']) $out[] = ['name' => $c['name']];
}
json_out($out);
