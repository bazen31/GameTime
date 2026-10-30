PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'customer'
        CHECK (role IN ('customer', 'admin')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) BETWEEN 1 AND 80)
);

CREATE TABLE IF NOT EXISTS equipment (
    id INTEGER PRIMARY KEY,
    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE RESTRICT,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 120),
    equipment_type TEXT NOT NULL CHECK (equipment_type IN ('PC', 'console')),
    specifications TEXT NOT NULL DEFAULT '',
    hourly_rate INTEGER NOT NULL CHECK (hourly_rate >= 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    equipment_id INTEGER NOT NULL REFERENCES equipment(id) ON DELETE RESTRICT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed'
        CHECK (status IN ('confirmed', 'running', 'completed', 'cancelled', 'no_show')),
    rate_snapshot INTEGER NOT NULL CHECK (rate_snapshot >= 0),
    total_price INTEGER NOT NULL CHECK (total_price >= 0),
    created_at TEXT NOT NULL,
    CHECK (end_at > start_at)
);

CREATE TABLE IF NOT EXISTS club_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    address TEXT NOT NULL DEFAULT 'ул. Игровая, 1',
    opening_time TEXT NOT NULL,
    closing_time TEXT NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'Asia/Almaty',
    CHECK (opening_time < closing_time)
);

CREATE INDEX IF NOT EXISTS idx_equipment_type_zone_active
    ON equipment(equipment_type, zone_id, is_active);
CREATE INDEX IF NOT EXISTS idx_bookings_equipment_time_status
    ON bookings(equipment_id, start_at, end_at, status);
CREATE INDEX IF NOT EXISTS idx_bookings_user_start
    ON bookings(user_id, start_at DESC);
CREATE INDEX IF NOT EXISTS idx_bookings_status_start
    ON bookings(status, start_at);
