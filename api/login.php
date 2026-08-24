<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

$b = body();
$username = trim((string) ($b['username'] ?? ''));
$password = (string) ($b['password'] ?? '');

foreach (load_users() as $u) {
    if ($u['username'] === $username && password_verify($password, $u['hash'])) {
        session_regenerate_id(true);
        $_SESSION['user'] = ['username' => $u['username'], 'role' => $u['role']];
        unset($_SESSION['csrf']);
        json_out(['username' => $u['username'], 'role' => $u['role'], 'csrf' => csrf_token()]);
    }
}

sleep(1);  // slow down credential guessing
json_err('invalid username or password', 401);
