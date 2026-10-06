<?php
/**
 * JSON API endpoint for chart data.
 *
 * Params:
 *   metric  – "source_id:device_id:metric" (required)
 *   from    – ISO 8601 start time (optional, default: 24h ago)
 *   to      – ISO 8601 end time (optional, default: now)
 *
 * Returns:
 *   { "metric": "Display Name", "unit": "°C", "data": [{"t":"...","v":...}, ...] }
 */

session_start();
if (!isset($_SESSION['user'])) {
    http_response_code(401);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'unauthorized']);
    exit;
}

$config = require __DIR__ . '/config.php';
$user = $_SESSION['user'];
$userConfig = $config['users'][$user];
$allowedMetrics = $userConfig['metrics']; // key => display name

// The client only ever sends an opaque index into the user's metric list,
// never the real key (which contains the device UID + source id).
$metricIdx = (int)($_GET['metric'] ?? -1);
$metricKeys = array_keys($allowedMetrics);
if ($metricIdx < 0 || $metricIdx >= count($metricKeys)) {
    http_response_code(403);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'forbidden']);
    exit;
}
$metricKey = $metricKeys[$metricIdx];

// Parse time range
$to = $_GET['to'] ?? date('c');
$from = $_GET['from'] ?? date('c', time() - 86400);

// Validate / normalize. Convert to UTC since the daemon stores UTC timestamps.
try {
    $fromDt = new DateTime($from);
    $toDt = new DateTime($to);
    $utc = new DateTimeZone('UTC');
    $fromDt->setTimezone($utc);
    $toDt->setTimezone($utc);
} catch (Exception $e) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'invalid time format']);
    exit;
}

if ($fromDt >= $toDt) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'from must be before to']);
    exit;
}

// Query the database
$dbPath = $config['database'];
$debug = $config['debug'] ?? false;

if (!file_exists($dbPath)) {
    http_response_code(500);
    header('Content-Type: application/json');
    echo json_encode(['error' => $debug ? "database not found: $dbPath" : 'database not found']);
    exit;
}

try {
    $db = new PDO('sqlite:' . $dbPath);
    $db->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
} catch (Exception $e) {
    http_response_code(500);
    header('Content-Type: application/json');
    echo json_encode(['error' => $debug ? 'database error: ' . $e->getMessage() : 'database error']);
    exit;
}

// Parse metric key: "source_id|device_id|metric"
$parts = explode('|', $metricKey, 3);
if (count($parts) !== 3) {
    http_response_code(400);
    header('Content-Type: application/json');
    echo json_encode(['error' => 'invalid metric key']);
    exit;
}
[$sourceId, $deviceId, $metric] = $parts;

// Stored format: "2026-10-04T12:34:56.789012+00:00"
// Use >= for from, < for to+1s to handle the microsecond/offset suffix.
$toDt->modify('+1 second');
$stmt = $db->prepare(
    "SELECT r.ts, r.value
     FROM readings r
     JOIN metrics m ON m.id = r.metric_id
     WHERE m.source_id = ? AND m.device_id = ? AND m.metric = ?
       AND r.ts >= ? AND r.ts < ?
     ORDER BY r.ts ASC"
);
$stmt->execute([$sourceId, $deviceId, $metric, $fromDt->format('Y-m-d\TH:i:s'), $toDt->format('Y-m-d\TH:i:s')]);
$rows = $stmt->fetchAll(PDO::FETCH_ASSOC);

// Get unit from metrics table
$unitStmt = $db->prepare(
    "SELECT unit FROM metrics WHERE source_id = ? AND device_id = ? AND metric = ?"
);
$unitStmt->execute([$sourceId, $deviceId, $metric]);
$unitRow = $unitStmt->fetch(PDO::FETCH_ASSOC);

$data = array_map(fn($r) => ['t' => $r['ts'], 'v' => $r['value']], $rows);

header('Content-Type: application/json');
echo json_encode([
    'metric' => $allowedMetrics[$metricKey] ?? $metric,
    'unit'   => $unitRow['unit'] ?? '',
    'data'   => $data,
]);
