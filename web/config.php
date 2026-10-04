<?php
/**
 * Website configuration.
 *
 * - `database`: path to the SQLite file (relative to this file or absolute).
 * - `users`:    map of username => [password, metrics].
 *
 * Each user's `metrics` is a map:
 *   "source_id:device_id:metric" => "Display Name"
 *
 * The display name is what the user sees in the UI. If you want the default
 * metric name, set the value to null.
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
                'zigpy:0x22BA:temperature' => 'Temperature',
                'zigpy:0x22BA:humidity'    => 'Humidity',
                'zigpy:0x22BA:battery_voltage' => 'Battery',
            ],
        ],
        // 'viewer' => [
        //     'password' => 'view123',
        //     'metrics' => [
        //         'zigpy:0x22BA:temperature' => 'Room Temp',
        //     ],
        // ],
    ],
];
