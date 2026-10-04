<?php
session_start();
if (!isset($_SESSION['user'])) {
    header('Location: login.php');
    exit;
}

$config = require __DIR__ . '/config.php';
$user = $_SESSION['user'];
$metrics = $config['users'][$user]['metrics']; // key => display name
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Zigbee Monitor</title>
    <link rel="stylesheet" href="assets/style.css">
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js"></script>
</head>
<body>
<header>
    <h1>Zigbee Monitor</h1>
    <div class="header-right">
        <span class="user-label"><?= htmlspecialchars($user) ?></span>
        <a href="logout.php" class="btn btn-sm">Log out</a>
    </div>
</header>

<main>
    <div class="controls">
        <label>
            From
            <input type="datetime-local" id="time-from">
        </label>
        <label>
            To
            <input type="datetime-local" id="time-to">
        </label>
        <button id="btn-refresh" class="btn">Refresh</button>
        <div class="presets">
            <button class="btn btn-sm preset" data-hours="1">1h</button>
            <button class="btn btn-sm preset" data-hours="6">6h</button>
            <button class="btn btn-sm preset" data-hours="24">24h</button>
            <button class="btn btn-sm preset" data-hours="168">7d</button>
        </div>
    </div>

    <div id="charts"></div>
</main>

<script>
const METRICS = <?= json_encode($metrics) ?>;
const charts = {};

// --- Time helpers ---
function toLocalInput(d) {
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function setDefaultRange() {
    const now = new Date();
    const from = new Date(now.getTime() - 24 * 3600 * 1000);
    document.getElementById('time-from').value = toLocalInput(from);
    document.getElementById('time-to').value = toLocalInput(now);
}

// --- Bucketing: aggregate raw points into min/max/avg per bucket ---
function bucketData(points, canvasWidth) {
    // points: [{t, v}] sorted by time
    const bucketCount = Math.max(50, Math.floor(canvasWidth / 3));
    if (points.length <= bucketCount) {
        return points.map(p => ({ t: p.t, min: p.v, max: p.v, avg: p.v }));
    }
    const t0 = new Date(points[0].t).getTime();
    const t1 = new Date(points[points.length - 1].t).getTime();
    const span = t1 - t0 || 1;
    const buckets = Array.from({ length: bucketCount }, (_, i) => ({
        t: new Date(t0 + (span * (i + 0.5)) / bucketCount).toISOString(),
        sum: 0, count: 0, min: Infinity, max: -Infinity
    }));
    for (const p of points) {
        const idx = Math.min(bucketCount - 1, Math.floor((new Date(p.t).getTime() - t0) / span * bucketCount));
        const b = buckets[idx];
        b.sum += p.v; b.count++;
        if (p.v < b.min) b.min = p.v;
        if (p.v > b.max) b.max = p.v;
    }
    return buckets.filter(b => b.count > 0).map(b => ({
        t: b.t, min: b.min, max: b.max, avg: b.sum / b.count
    }));
}

// --- Chart creation ---
function createChart(key, displayName) {
    const container = document.getElementById('charts');
    const div = document.createElement('div');
    div.className = 'chart-card';
    div.id = 'chart-' + key.replace(/:/g, '-');
    div.innerHTML = `
        <div class="chart-header">
            <h2>${displayName}</h2>
            <div class="chart-actions">
                <button class="btn btn-sm btn-download" data-action="png" data-key="${key}">PNG</button>
                <button class="btn btn-sm btn-download" data-action="csv" data-key="${key}">CSV</button>
            </div>
        </div>
        <canvas></canvas>`;
    container.appendChild(div);

    const ctx = div.querySelector('canvas').getContext('2d');
    charts[key] = new Chart(ctx, {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'max',
                    data: [],
                    borderColor: 'rgba(59,130,246,0.35)',
                    borderWidth: 1,
                    pointRadius: 0,
                    pointHitRadius: 0,
                    tension: 0.2,
                    fill: false,
                },
                {
                    label: 'min',
                    data: [],
                    borderColor: 'rgba(59,130,246,0.35)',
                    borderWidth: 1,
                    pointRadius: 0,
                    pointHitRadius: 0,
                    tension: 0.2,
                    fill: { target: 0, above: 'rgba(59,130,246,0.08)' },
                },
                {
                    label: displayName,
                    data: [],
                    borderColor: '#3b82f6',
                    borderWidth: 2,
                    pointRadius: 0,
                    pointHitRadius: 5,
                    tension: 0.2,
                    fill: false,
                },
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
                legend: { display: false },
                tooltip: {
                    callbacks: {
                        title: items => new Date(items[0].parsed.x).toLocaleString(),
                        label: item => {
                            const u = charts[key].unit || '';
                            const name = item.dataset.label;
                            if (name === 'max') return `Max: ${item.parsed.y} ${u}`;
                            if (name === 'min') return `Min: ${item.parsed.y} ${u}`;
                            return `Avg: ${item.parsed.y} ${u}`;
                        }
                    }
                }
            },
            scales: {
                x: { type: 'time', time: { tooltipFormat: 'yyyy-MM-dd HH:mm' } },
                y: { beginAtZero: false }
            }
        }
    });
}

// Convert datetime-local value (no tz) to ISO 8601 UTC
function toISO(localVal) {
    return new Date(localVal).toISOString();
}

// --- Data fetching ---
async function fetchMetric(key) {
    const from = toISO(document.getElementById('time-from').value);
    const to = toISO(document.getElementById('time-to').value);
    const url = `api.php?metric=${encodeURIComponent(key)}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
    const res = await fetch(url);
    if (!res.ok) return;
    const json = await res.json();
    if (json.error) return;

    const chart = charts[key];
    chart.unit = json.unit;
    chart.rawData = json.data;

    const canvasWidth = chart.canvas.width || 600;
    const buckets = bucketData(json.data, canvasWidth);
    chart.data.labels = buckets.map(b => b.t);
    chart.data.datasets[0].data = buckets.map(b => ({ x: b.t, y: b.max }));
    chart.data.datasets[1].data = buckets.map(b => ({ x: b.t, y: b.min }));
    chart.data.datasets[2].data = buckets.map(b => ({ x: b.t, y: b.avg }));
    chart.data.datasets[2].label = `${json.metric} (${json.unit})`;
    chart.options.scales.y.title = {
        display: true,
        text: json.unit || '',
        color: '#94a3b8',
    };
    chart.update('none');
}

async function refreshAll() {
    for (const key of Object.keys(METRICS)) {
        await fetchMetric(key);
    }
}

// --- Download handlers ---
function downloadPNG(key) {
    const chart = charts[key];
    if (!chart) return;
    const name = (METRICS[key] || 'chart').replace(/[^a-zA-Z0-9 _-]/g, '').trim().replace(/\s+/g, '_');
    const link = document.createElement('a');
    link.download = `${name}.png`;
    link.href = chart.toBase64Image('image/png', 1);
    link.click();
}

function downloadCSV(key) {
    const chart = charts[key];
    if (!chart) return;
    const unit = chart.unit || '';
    const name = (METRICS[key] || 'chart').replace(/[^a-zA-Z0-9 _-]/g, '').trim().replace(/\s+/g, '_');
    const rows = [['timestamp', 'value' + (unit ? ` (${unit})` : '')]];
    const data = chart.rawData || chart.data.datasets[2].data.map(p => ({ t: p.x, v: p.y }));
    for (const pt of data) {
        rows.push([pt.t, pt.v]);
    }
    const csv = rows.map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const link = document.createElement('a');
    link.download = `${name}.csv`;
    link.href = URL.createObjectURL(blob);
    link.click();
    URL.revokeObjectURL(link.href);
}

// --- Init ---
document.addEventListener('DOMContentLoaded', () => {
    setDefaultRange();
    for (const [key, name] of Object.entries(METRICS)) {
        createChart(key, name);
    }
    refreshAll();

    document.getElementById('btn-refresh').addEventListener('click', refreshAll);

    document.querySelectorAll('.preset').forEach(btn => {
        btn.addEventListener('click', () => {
            const hours = parseInt(btn.dataset.hours);
            const now = new Date();
            const from = new Date(now.getTime() - hours * 3600 * 1000);
            document.getElementById('time-from').value = toLocalInput(from);
            document.getElementById('time-to').value = toLocalInput(now);
            refreshAll();
        });
    });

    document.querySelectorAll('.btn-download').forEach(btn => {
        btn.addEventListener('click', () => {
            const key = btn.dataset.key;
            if (btn.dataset.action === 'png') downloadPNG(key);
            else if (btn.dataset.action === 'csv') downloadCSV(key);
        });
    });
});
</script>
</body>
</html>
