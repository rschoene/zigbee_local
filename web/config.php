<?php
/**
 * Website configuration.
 *
 * - `database`: path to the SQLite file (relative to this file or absolute).
 * - `users`:    map of username => [password, metrics].
 *
 * Each user's `metrics` is a map:
 *   "source_id|device_id|metric" => "Display Name"
 *
 * The delimiter is `|` (pipe) because device IDs (IEEE addresses) contain
 * colons. The display name is what the user sees in the UI.
 *
 * Passwords: use a bcrypt hash (run `php -r "echo password_hash('secret', PASSWORD_DEFAULT);"`).
 * Plain-text passwords are also accepted (compared with hash_equals) for
 * convenience on a trusted local network.
 */

return [
    'database' => '/opt/zigbee_local/data/zigbee.db',
    'debug' => true,  // set to false in production

    'users' => [
        'admin' => [
            'password' => 'admin',  // TODO: replace with password_hash('...', PASSWORD_DEFAULT)
            'metrics' => [
                'dongle1|a4:c1:38:17:64:0a:ff:ff|temperature_measured_value' => 'Temperature',
                'dongle1|a4:c1:38:17:64:0a:ff:ff|relative_measured_value'    => 'Humidity',
            ],
        ],
        // 'viewer' => [
        //     'password' => 'view123',
        //     'metrics' => [
        //         'dongle1|a4:c1:38:17:64:0a:ff:ff|temperature' => 'Room Temp',
        //     ],
        // ],
    ],
];
