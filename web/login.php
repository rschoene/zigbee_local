<?php
session_start();
if (isset($_SESSION['user'])) {
    header('Location: chart.php');
    exit;
}

$config = require __DIR__ . '/config.php';
$error = '';

if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $user = $_POST['username'] ?? '';
    $pass = $_POST['password'] ?? '';

    if (isset($config['users'][$user])) {
        $stored = $config['users'][$user]['password'];
        $ok = password_verify($pass, $stored) || hash_equals($stored, $pass);
        if ($ok) {
            session_regenerate_id(true);
            $_SESSION['user'] = $user;
            header('Location: chart.php');
            exit;
        }
    }
    $error = 'Invalid username or password.';
}
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login – Zigbee Monitor</title>
    <link rel="stylesheet" href="assets/style.css">
</head>
<body class="login-body">
<div class="login-box">
    <h1>Zigbee Monitor</h1>
    <?php if ($error): ?>
        <p class="error"><?= htmlspecialchars($error) ?></p>
    <?php endif; ?>
    <form method="POST" action="login.php">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required autofocus>
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required>
        <button type="submit">Log in</button>
    </form>
</div>
</body>
</html>
