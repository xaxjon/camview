<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

$u = current_user();
json_out([
    'user' => $u,
    'csrf' => $u ? csrf_token() : null,
    'setup_needed' => !load_users(),  // no users yet -> first-run setup
]);
