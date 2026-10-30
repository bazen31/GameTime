import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gametime import create_app, create_booking_atomic, get_db, seed_database


class GameTimeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key",
                "DATABASE": str(Path(self.temp_dir.name) / "test.sqlite3"),
            }
        )
        seed_database(self.app)
        with self.app.app_context():
            db = get_db()
            zone_id = db.execute("SELECT id FROM zones LIMIT 1").fetchone()["id"]
            user_id = db.execute(
                "SELECT id FROM users WHERE email = 'aliya@gametime.test'"
            ).fetchone()["id"]
            self.equipment_id = db.execute(
                "INSERT INTO equipment(zone_id, name, equipment_type, specifications, hourly_rate, is_active) "
                "VALUES (?, 'Test PC', 'PC', 'Test hardware', 1000, 1)",
                (zone_id,),
            ).lastrowid
            db.execute(
                "UPDATE club_settings SET opening_time = '00:00', closing_time = '23:30', timezone = 'UTC' WHERE id = 1"
            )
            db.commit()
            self.user_id = user_id
            self.other_user_id = db.execute(
                "SELECT id FROM users WHERE email = 'timur@gametime.test'"
            ).fetchone()["id"]
            self.admin_id = db.execute(
                "SELECT id FROM users WHERE role = 'admin'"
            ).fetchone()["id"]
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def future_interval(self, hours=48, duration=60):
        start = (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)).replace(
            minute=0, second=0, microsecond=0
        )
        end = start + timedelta(minutes=duration)
        return start, end

    def make_booking(self, start, end, user_id=None, equipment_id=None):
        with self.app.app_context():
            return create_booking_atomic(
                user_id or self.user_id,
                equipment_id or self.equipment_id,
                start,
                end,
            )

    def login_as(self, user_id):
        with self.client.session_transaction() as browser_session:
            browser_session["user_id"] = user_id
            browser_session["_csrf_token"] = "test-token"

    def test_overlap_is_rejected_but_adjacent_booking_is_allowed(self):
        start, end = self.future_interval()
        self.assertEqual(self.make_booking(start, end)[0], "ok")
        self.assertEqual(self.make_booking(start + timedelta(minutes=30), end + timedelta(minutes=30))[0], "occupied")
        self.assertEqual(self.make_booking(end, end + timedelta(hours=1))[0], "ok")

    def test_booking_keeps_rate_snapshot_and_cancel_releases_interval(self):
        start, end = self.future_interval()
        result, booking_id = self.make_booking(start, end)
        self.assertEqual(result, "ok")
        with self.app.app_context():
            db = get_db()
            db.execute("UPDATE equipment SET hourly_rate = 1800 WHERE id = ?", (self.equipment_id,))
            booking = db.execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
            self.assertEqual(booking["rate_snapshot"], 1000)
            self.assertEqual(booking["total_price"], 1000)
            db.execute("UPDATE bookings SET status = 'cancelled' WHERE id = ?", (booking_id,))
            db.commit()
        self.assertEqual(self.make_booking(start, end)[0], "ok")

    def test_concurrent_requests_create_only_one_booking(self):
        start, end = self.future_interval(hours=72)
        barrier = threading.Barrier(2)

        def attempt():
            barrier.wait()
            return self.make_booking(start, end)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: attempt(), range(2)))
        self.assertCountEqual(results, ["ok", "occupied"])

    def test_customer_cannot_finish_or_cancel_another_users_booking(self):
        start, end = self.future_interval()
        result, booking_id = self.make_booking(start, end)
        self.assertEqual(result, "ok")
        self.login_as(self.other_user_id)
        response = self.client.post(
            f"/bookings/{booking_id}/cancel", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 403)
        self.login_as(self.user_id)
        response = self.client.post(
            f"/bookings/{booking_id}/finish", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 403)

    def test_slots_endpoint_and_auth_pages_render(self):
        response = self.client.get("/equipment")
        self.assertEqual(response.status_code, 200)
        self.assertIn("В этот день пока свободно".encode(), response.data)
        response = self.client.get(f"/equipment/{self.equipment_id}")
        self.assertEqual(response.status_code, 200)
        response = self.client.get(
            f"/equipment/{self.equipment_id}/slots?date=2030-01-01"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["slots"], [])

    def test_booking_form_creates_server_priced_booking_and_admin_pages_render(self):
        start, end = self.future_interval(duration=30)
        self.login_as(self.user_id)
        response = self.client.post(
            "/bookings",
            data={
                "csrf_token": "test-token",
                "equipment_id": self.equipment_id,
                "date": start.date().isoformat(),
                "start_time": start.strftime("%H:%M"),
                "end_time": end.strftime("%H:%M"),
            },
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            booking = get_db().execute(
                "SELECT rate_snapshot, total_price, status FROM bookings "
                "WHERE equipment_id = ? ORDER BY id DESC LIMIT 1",
                (self.equipment_id,),
            ).fetchone()
            self.assertEqual(booking["rate_snapshot"], 1000)
            self.assertEqual(booking["total_price"], 500)
            self.assertEqual(booking["status"], "confirmed")
        self.login_as(self.admin_id)
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.assertEqual(self.client.get("/admin/settings").status_code, 200)
        self.assertEqual(self.client.get("/reports/utilization").status_code, 200)

    def test_booking_status_transitions_are_admin_only_and_one_way(self):
        start, end = self.future_interval()
        result, booking_id = self.make_booking(start, end)
        self.assertEqual(result, "ok")
        self.login_as(self.admin_id)
        response = self.client.post(
            f"/bookings/{booking_id}/start", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.post(
            f"/bookings/{booking_id}/finish", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            status = get_db().execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "completed")
        response = self.client.post(
            f"/bookings/{booking_id}/finish", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            status = get_db().execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "completed")

    def test_seed_contains_required_demo_data(self):
        with self.app.app_context():
            db = get_db()
            counts = (
                db.execute("SELECT count(*) FROM equipment WHERE name != 'Test PC'").fetchone()[0],
                db.execute("SELECT count(*) FROM zones").fetchone()[0],
                db.execute("SELECT count(*) FROM users WHERE role = 'customer'").fetchone()[0],
                db.execute("SELECT count(*) FROM bookings").fetchone()[0],
            )
        self.assertEqual(counts, (8, 2, 3, 15))

    def test_no_show_is_available_after_booking_start(self):
        start, end = self.future_interval()
        result, booking_id = self.make_booking(start, end)
        self.assertEqual(result, "ok")
        self.login_as(self.admin_id)
        with self.app.app_context():
            get_db().execute(
                "UPDATE bookings SET start_at = '2000-01-01T10:00' WHERE id = ?",
                (booking_id,),
            )
            get_db().commit()
        response = self.client.post(
            f"/bookings/{booking_id}/no-show", data={"csrf_token": "test-token"}
        )
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            status = get_db().execute(
                "SELECT status FROM bookings WHERE id = ?", (booking_id,)
            ).fetchone()["status"]
        self.assertEqual(status, "no_show")


if __name__ == "__main__":
    unittest.main()
