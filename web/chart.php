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
            datasets: [{
                label: displayName,
                data: [],
                borderColor: '#3b82f6',
                backgroundColor: 'rgba(59,130,246,0.1)',
                borderWidth: 2,
                pointRadius: 0,
                pointHitRadius: 5,
                tension: 0.2,
                fill: true,
            }]
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
                        label: item => `${item.parsed.y} ${charts[key].unit || ''}`
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

// --- Data fetching ---
async function fetchMetric(key) {
    const from = document.getElementById('time-from').value;
    const to = document.getElementById('time-to').value;
    const url = `api.php?metric=${encodeURIComponent(key)}&from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`;
    const res = await fetch(url);
    if (!res.ok) return;
    const json = await res.json();
    if (json.error) return;

    const chart = charts[key];
    chart.unit = json.unit;
    chart.data.datasets[0].label = `${json.metric} (${json.unit})`;
    chart.data.labels = json.data.map(d => d.t);
    chart.data.datasets[0].data = json.data.map(d => ({ x: d.t, y: d.v }));
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
    const link = document.createElement('a');
    link.download = `${key.replace(/[^a-zA-Z0-9]/g, '_')}.png`;
    link.href = chart.toBase64Image('image/png', 1);
    link.click();
}

function downloadCSV(key) {
    const chart = charts[key];
    if (!chart) return;
    const unit = chart.unit || '';
    const rows = [['timestamp', 'value' + (unit ? ` (${unit})` : '')]];
    for (const pt of chart.data.datasets[0].data) {
        rows.push([pt.x, pt.y]);
    }
    const csv = rows.map(r => r.join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const link = document.createElement('a');
    link.download = `${key.replace(/[^a-zA-Z0-9]/g, '_')}.csv`;
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
