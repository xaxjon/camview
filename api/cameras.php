<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

$u = require_login();
$method = $_SERVER['REQUEST_METHOD'];

if ($method === 'GET') {
    $status = mtx_path_status();
    $isAdmin = ($u['role'] ?? '') === 'admin';
    $all = $isAdmin && !empty($_GET['all']);  // admin UI needs disabled cameras too
    $out = [];
    foreach (load_cameras() as $c) {
        if (!$c['enabled'] && !$all) continue;  // the grid only shows watchable cameras
        $row = [
            'name' => $c['name'],
            'enabled' => $c['enabled'],
            'status' => $c['enabled'] ? ($status[$c['name']] ?? 'standby') : 'disabled',
        ];
        if ($isAdmin) {
            $row['source'] = $c['source'];
            $row['transcode_audio'] = $c['transcode_audio'];
        }
        $out[] = $row;
    }
    json_out($out);
}

require_admin();
check_csrf();

$old = load_cameras();
$oldByName = array_column($old, null, 'name');

function validated_camera(array $b): array {
    $name = trim((string) ($b['name'] ?? ''));
    $source = trim((string) ($b['source'] ?? ''));
    if (!preg_match(NAME_RE, $name)) json_err('invalid name (letters, digits, - and _ only)');
    if (!str_starts_with($source, 'rtsp://')) json_err('source must be an rtsp:// URL');
    return [
        'name' => $name,
        'source' => $source,
        'transcode_audio' => !empty($b['transcode_audio']),
        'enabled' => ($b['enabled'] ?? true) !== false,
    ];
}

if ($method === 'POST') {
    $cam = validated_camera(body());
    if (isset($oldByName[$cam['name']])) json_err("{$cam['name']} already exists", 409);
    apply_camera_changes($old, [...$old, $cam]);
    json_out(['ok' => true]);
}

if ($method === 'PUT') {
    $b = body();
    $orig = (string) ($b['original'] ?? '');
    if (!isset($oldByName[$orig])) json_err('camera not found', 404);
    $cam = validated_camera($b);
    if ($cam['name'] !== $orig && isset($oldByName[$cam['name']])) json_err("{$cam['name']} already exists", 409);
    $new = [];
    foreach ($old as $c) $new[] = ($c['name'] === $orig) ? $cam : $c;
    apply_camera_changes($old, $new);
    json_out(['ok' => true]);
}

if ($method === 'DELETE') {
    $name = (string) (body()['name'] ?? '');
    if (!isset($oldByName[$name])) json_err('camera not found', 404);
    apply_camera_changes($old, array_values(array_filter($old, fn($c) => $c['name'] !== $name)));
    json_out(['ok' => true]);
}

json_err('method not allowed', 405);
