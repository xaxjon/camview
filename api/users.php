<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

$me = require_admin();
$method = $_SERVER['REQUEST_METHOD'];

if ($method === 'GET') {
    json_out(array_map(fn($u) => ['username' => $u['username'], 'role' => $u['role']], load_users()));
}

check_csrf();
$users = load_users();

if ($method === 'POST') {
    $b = body();
    $username = trim((string) ($b['username'] ?? ''));
    $password = (string) ($b['password'] ?? '');
    $role = ($b['role'] ?? '') === 'admin' ? 'admin' : 'viewer';
    if (!preg_match('/^[a-zA-Z0-9_.-]{2,32}$/', $username)) json_err('invalid username');
    if (strlen($password) < 8) json_err('password must be at least 8 characters');
    foreach ($users as $u) if ($u['username'] === $username) json_err('user exists', 409);
    $users[] = ['username' => $username, 'hash' => password_hash($password, PASSWORD_BCRYPT), 'role' => $role];
    save_users($users);
    json_out(['ok' => true]);
}

if ($method === 'PUT') {
    $b = body();
    $username = (string) ($b['username'] ?? '');
    $found = false;
    foreach ($users as &$u) {
        if ($u['username'] !== $username) continue;
        $found = true;
        if (isset($b['role'])) {
            $role = $b['role'] === 'admin' ? 'admin' : 'viewer';
            if ($role !== 'admin' && $u['role'] === 'admin'
                && count(array_filter($users, fn($x) => $x['role'] === 'admin')) === 1) {
                json_err('cannot demote the last admin');
            }
            $u['role'] = $role;
        }
        if (!empty($b['password'])) {
            if (strlen((string) $b['password']) < 8) json_err('password must be at least 8 characters');
            $u['hash'] = password_hash((string) $b['password'], PASSWORD_BCRYPT);
        }
    }
    unset($u);
    if (!$found) json_err('user not found', 404);
    save_users($users);
    json_out(['ok' => true]);
}

if ($method === 'DELETE') {
    $username = (string) (body()['username'] ?? '');
    if ($username === $me['username']) json_err('cannot delete yourself');
    $target = null;
    foreach ($users as $u) if ($u['username'] === $username) { $target = $u; break; }
    if (!$target) json_err('user not found', 404);
    if ($target['role'] === 'admin'
        && count(array_filter($users, fn($x) => $x['role'] === 'admin')) === 1) {
        json_err('cannot delete the last admin');
    }
    save_users(array_values(array_filter($users, fn($u) => $u['username'] !== $username)));
    json_out(['ok' => true]);
}

json_err('method not allowed', 405);
