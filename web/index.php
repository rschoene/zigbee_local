<?php
session_start();
if (isset($_SESSION['user'])) {
    header('Location: chart.php');
} else {
    header('Location: login.php');
}
exit;
