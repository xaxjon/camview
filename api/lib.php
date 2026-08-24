<?php
declare(strict_types=1);

// Shared helpers for the camview API. No output when requested directly.

const CAMVIEW_ROOT = __DIR__ . '/..';
const STREAMS_FILE = CAMVIEW_ROOT . '/streams.json';
const USERS_FILE   = CAMVIEW_ROOT . '/users.json';

const NAME_RE = '/^[a-zA-Z0-9_-]+$/';

session_set_cookie_params([
    'httponly' => true,
    'samesite' => 'Lax',
    'secure' => !empty($_SERVER['HTTPS']) && $_SERVER['HTTPS'] !== 'off',
]);
session_start();

// ---------- responses ----------

function json_out($data, int $code = 200): never {
    http_response_code($code);
    header('Content-Type: application/json');
    echo json_encode($data);
    exit;
}

function json_err(string $msg, int $code = 400): never {
    json_out(['error' => $msg], $code);
}

function body(): array {
    $d = json_decode((string) file_get_contents('php://input'), true);
    return is_array($d) ? $d : [];
}

// ---------- auth ----------

function current_user(): ?array {
    return $_SESSION['user'] ?? null;
}

function require_login(): array {
    $u = current_user();
    if (!$u) json_err('login required', 401);
    return $u;
}

function require_admin(): array {
    $u = require_login();
    if (($u['role'] ?? '') !== 'admin') json_err('admin required', 403);
    return $u;
}

function csrf_token(): string {
    if (empty($_SESSION['csrf'])) $_SESSION['csrf'] = bin2hex(random_bytes(16));
    return $_SESSION['csrf'];
}

function check_csrf(): void {
    $t = $_SERVER['HTTP_X_CSRF_TOKEN'] ?? '';
    if (!hash_equals(csrf_token(), $t)) json_err('bad CSRF token', 403);
}

// ---------- users store ----------

function load_users(): array {
    if (!is_file(USERS_FILE)) return [];
    $d = json_decode((string) file_get_contents(USERS_FILE), true);
    return is_array($d) ? $d : [];
}

function save_users(array $users): void {
    atomic_write(USERS_FILE, json_encode(array_values($users), JSON_PRETTY_PRINT));
}

// ---------- cameras store ----------

// Returns list of camera dicts (string comment entries in streams.json are dropped).
function load_cameras(): array {
    if (!is_file(STREAMS_FILE)) return [];
    $d = json_decode((string) file_get_contents(STREAMS_FILE), true);
    if (!is_array($d)) return [];
    $out = [];
    foreach ($d as $s) {
        if (!is_array($s) || !isset($s['name'], $s['source'])) continue;
        $out[] = [
            'name' => $s['name'],
            'source' => $s['source'],
            'transcode_audio' => !empty($s['transcode_audio']),
            'enabled' => ($s['enabled'] ?? true) !== false,
        ];
    }
    return $out;
}

function save_cameras(array $cams): void {
    $doc = ["Managed via the admin UI (admin.html). Manual edits: run sudo python3 gen-config.py && sudo systemctl restart mediamtx-viewer"];
    foreach ($cams as $c) {
        $e = ['name' => $c['name'], 'source' => $c['source']];
        if (!empty($c['transcode_audio'])) $e['transcode_audio'] = true;
        if (isset($c['enabled']) && !$c['enabled']) $e['enabled'] = false;
        $doc[] = $e;
    }
    atomic_write(STREAMS_FILE, json_encode($doc, JSON_PRETTY_PRINT));
}

function atomic_write(string $path, string $data): void {
    $tmp = $path . '.tmp';
    if (file_put_contents($tmp, $data, LOCK_EX) === false || !rename($tmp, $path)) {
        json_err("cannot write $path — check web server permissions", 500);
    }
}

// ---------- MediaMTX ----------

function mtx_api(string $method, string $path, $payload = null): array {
    $base = rtrim(getenv('MTX_API') ?: 'http://127.0.0.1:9997', '/');
    $ch = curl_init($base . $path);
    curl_setopt_array($ch, [
        CURLOPT_CUSTOMREQUEST => $method,
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_TIMEOUT => 8,
        CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
    ]);
    if ($payload !== null) curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($payload));
    $res = curl_exec($ch);
    $code = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
    curl_close($ch);
    if ($res === false) return ['code' => 0, 'body' => null];
    return ['code' => $code, 'body' => json_decode((string) $res, true)];
}

// name => 'online' (streaming) | 'standby' (configured, not pulling) for all configured paths
function mtx_path_status(): array {
    $r = mtx_api('GET', '/v3/paths/list');
    $out = [];
    foreach (($r['body']['items'] ?? []) as $p) {
        if (isset($p['name'])) $out[$p['name']] = !empty($p['ready']) ? 'online' : 'standby';
    }
    return $out;
}

// Regenerate mediamtx.yml and live-apply path changes to the running MediaMTX.
function apply_camera_changes(array $old, array $new): void {
    save_cameras($new);

    $cmd = 'cd ' . escapeshellarg(CAMVIEW_ROOT) . ' && python3 gen-config.py --paths-json';
    $errFile = tempnam(sys_get_temp_dir(), 'gencfg');
    exec($cmd . ' 2> ' . escapeshellarg($errFile), $out, $rc);
    $stderr = trim((string) file_get_contents($errFile));
    unlink($errFile);
    $paths = json_decode(implode("\n", $out), true);
    if ($rc !== 0 || !is_array($paths)) {
        json_err('config regeneration failed: ' . ($stderr ?: implode("\n", $out)), 500);
    }

    $oldByName = [];
    foreach ($old as $c) $oldByName[$c['name']] = $c;
    $newByName = [];
    foreach ($new as $c) $newByName[$c['name']] = $c;

    $delete = $add = [];
    foreach ($old as $c) {
        // removed entirely, or disabled (no longer in generated paths)
        if (!isset($newByName[$c['name']]) || !isset($paths[$c['name']])) $delete[] = $c['name'];
    }
    foreach ($paths as $name => $conf) {
        $o = $oldByName[$name] ?? null;
        $n = $newByName[$name];
        if (!$o || $o['source'] !== $n['source'] || $o['transcode_audio'] !== $n['transcode_audio']
            || $o['enabled'] !== $n['enabled']) {
            $add[] = $name;  // new, or replaced via delete+add
        }
    }

    foreach (array_unique([...$delete, ...array_intersect($add, array_keys($oldByName))]) as $name) {
        $r = mtx_api('DELETE', '/v3/config/paths/delete/' . rawurlencode($name));
        if (!in_array($r['code'], [200, 404], true)) json_err("mediamtx delete failed for $name", 502);
    }
    foreach ($add as $name) {
        $r = mtx_api('POST', '/v3/config/paths/add/' . rawurlencode($name), $paths[$name]);
        if ($r['code'] !== 200) json_err("mediamtx add failed for $name", 502);
    }
}
