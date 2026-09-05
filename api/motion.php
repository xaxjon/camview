<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Motion timeline data + JPEG serving (read-only, any logged-in user).
// Data source: motion/<cam>/<YYYY-MM-DD>/<cam>-<Ymd-His>.jpg written by motion.py.

const MOTION_DIR = CAMVIEW_ROOT . '/motion';
const MOTION_FILE_RE = '/^([a-zA-Z0-9_-]+)\/(\d{4}-\d{2}-\d{2})\/([a-zA-Z0-9_-]+)-(\d{8})-(\d{6})\.jpg$/';

require_login();

if (isset($_GET['file'])) {
    $rel = (string) $_GET['file'];
    if (!preg_match(MOTION_FILE_RE, $rel)) json_err('invalid file', 400);
    $path = MOTION_DIR . '/' . $rel;
    if (!is_file($path)) json_err('not found', 404);
    header('Content-Type: image/jpeg');
    header('Content-Length: ' . filesize($path));
    if (!empty($_GET['download'])) {
        header('Content-Disposition: attachment; filename="' . basename($path) . '"');
    }
    readfile($path);
    exit;
}

$range = ($_GET['range'] ?? '24h') === '7d' ? '7d' : '24h';

// motion-enabled cameras from the config
$cams = [];
foreach (load_cameras() as $c) {
    if ($c['enabled'] && $c['motion']) $cams[] = $c['name'];
}

$days = [];
$today = new DateTime('today');
if ($range === '7d') {
    for ($i = 6; $i >= 0; $i--) $days[] = (clone $today)->modify("-$i days")->format('Y-m-d');
    $since = $today->getTimestamp() - 6 * 86400;
} else {
    $days[] = (clone $today)->modify('-1 day')->format('Y-m-d');
    $days[] = $today->format('Y-m-d');
    $since = time() - 86400;
}

$files = [];
foreach ($cams as $cam) {
    $files[$cam] = [];
    foreach ($days as $day) {
        foreach (glob(MOTION_DIR . "/$cam/$day/$cam-*.jpg") ?: [] as $f) {
            if (filemtime($f) < $since) continue;
            if (!preg_match('/-(\d{8})-(\d{6})\.jpg$/', basename($f), $m)) continue;
            $ts = DateTime::createFromFormat('Ymd-His', "$m[1]-$m[2]")->getTimestamp();
            $files[$cam][] = [$ts, "$cam/$day/" . basename($f)];
        }
    }
    usort($files[$cam], fn($a, $b) => $a[0] <=> $b[0]);
}

json_out(['range' => $range, 'cams' => $cams, 'files' => $files]);
