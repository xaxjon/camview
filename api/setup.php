<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// First-run only: creates the initial admin. Refuses once any user exists.
if (load_users()) json_err('setup already completed', 403);

$b = body();
$username = trim((string) ($b['username'] ?? ''));
$password = (string) ($b['password'] ?? '');

if (!preg_match('/^[a-zA-Z0-9_.-]{2,32}$/', $username)) json_err('invalid username');
if (strlen($password) < 8) json_err('password must be at least 8 characters');

save_users([[
    'username' => $username,
    'hash' => password_hash($password, PASSWORD_BCRYPT),
    'role' => 'admin',
]]);
json_out(['ok' => true]);
