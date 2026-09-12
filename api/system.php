<?php
declare(strict_types=1);
require __DIR__ . '/lib.php';

// System status (admin only): disk, memory, CPU, load, network — plus the
// "purge > 1 day" action for motion captures and snapshots.

require_admin();

function read_cpu(): array {  // [busy jiffies, total jiffies]
    $l = @file_get_contents('/proc/stat');
    if ($l && preg_match('/^cpu\s+((?:\d+\s+){7}\d+)/m', $l, $m)) {
        $v = array_map('intval', preg_split('/\s+/', trim($m[1])));
        $idle = $v[3] + $v[4];  // idle + iowait
        return [array_sum($v) - $idle, array_sum($v)];
    }
    return [0, 0];
}

function read_net(): array {  // [rx bytes, tx bytes] over all non-loopback interfaces
    $rx = $tx = 0;
    foreach (@file('/proc/net/dev') ?: [] as $line) {
        if (!preg_match('/^\s*([\w.-]+):\s*(.+)$/', $line, $m) || $m[1] === 'lo') continue;
        $f = preg_split('/\s+/', trim($m[2]));
        $rx += (int) $f[0];
        $tx += (int) $f[8];
    }
    return [$rx, $tx];
}

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    check_csrf();
    $files = 0;
    $bytes = 0;
    // motion day-dirs older than yesterday (keep today + yesterday)
    $keepAfter = strtotime('today') - 86400;
    foreach (glob(CAMVIEW_ROOT . '/motion/*', GLOB_ONLYDIR) ?: [] as $camDir) {
        foreach (glob($camDir . '/*', GLOB_ONLYDIR) ?: [] as $dayDir) {
            $day = basename($dayDir);
            if (!preg_match('/^\d{4}-\d{2}-\d{2}$/', $day)) continue;
            if (strtotime($day) >= $keepAfter) continue;
            foreach (glob($dayDir . '/*.jpg') ?: [] as $f) {
                $bytes += filesize($f);
                $files++;
                unlink($f);
            }
            @rmdir($dayDir);
        }
    }
    // snapshots older than 24h
    foreach (glob(SNAPSHOTS_DIR . '/*.jpg') ?: [] as $f) {
        if (filemtime($f) < time() - 86400) {
            $bytes += filesize($f);
            $files++;
            unlink($f);
        }
    }
    json_out(['ok' => true, 'files' => $files, 'bytes' => $bytes]);
}

// GET: disk + memory are point-in-time; CPU and network rates come from a
// 500ms sample window measured here
[$busy, $total] = read_cpu();
[$rx, $tx] = read_net();
usleep(500000);
[$busy2, $total2] = read_cpu();
[$rx2, $tx2] = read_net();
$cpu = $total2 > $total ? round(100 * ($busy2 - $busy) / ($total2 - $total), 1) : 0.0;

$diskTotal = (int) disk_total_space(CAMVIEW_ROOT);
$diskFree = (int) disk_free_space(CAMVIEW_ROOT);

$memTotal = $memAvail = 0;
foreach (@file('/proc/meminfo') ?: [] as $line) {
    if (preg_match('/^MemTotal:\s+(\d+) kB/', $line, $m)) $memTotal = (int) $m[1] * 1024;
    if (preg_match('/^MemAvailable:\s+(\d+) kB/', $line, $m)) $memAvail = (int) $m[1] * 1024;
}

$load = array_map('floatval', array_slice(explode(' ', (string) @file_get_contents('/proc/loadavg')), 0, 3));
$uptime = (int) explode(' ', (string) @file_get_contents('/proc/uptime'))[0];

json_out([
    'disk' => ['total' => $diskTotal, 'used' => $diskTotal - $diskFree, 'free' => $diskFree],
    'mem' => ['total' => $memTotal, 'used' => $memTotal - $memAvail, 'available' => $memAvail],
    'cpu' => $cpu,
    'cores' => max(1, (int) trim((string) shell_exec('nproc'))),
    'load' => $load,
    'net' => [
        'rx_rate' => ($rx2 - $rx) * 2,
        'tx_rate' => ($tx2 - $tx) * 2,
        'rx_total' => $rx2,
        'tx_total' => $tx2,
    ],
    'uptime' => $uptime,
]);
