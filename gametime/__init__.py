import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    current_app,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema.sql"
STATUSES = {
    "confirmed": "Подтверждена",
    "running": "Идёт",
    "completed": "Завершена",
    "cancelled": "Отменена",
    "no_show": "Неявка",
}

ACTIVE_STATUSES = ("confirmed", "running")


def create_app(test_config=None):
    app = Flask(
        __name__,
        instance_relative_config=True,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("GAMETIME_DATABASE", str(Path(app.instance_path) / "gametime.sqlite3")),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)

    @app.before_request
    def protect_forms():
        if request.method == "POST":
            submitted = request.form.get("csrf_token", "")
            expected = session.get("_csrf_token", "")
            import hmac

            if not expected or not hmac.compare_digest(submitted, expected):
                abort(400, description="Форма устарела. Обновите страницу и попробуйте снова.")

    @app.context_processor
    def template_context():
        return {
            "current_user": current_user(),
            "csrf_token": csrf_token,
            "status_labels": STATUSES,
        }

    @app.template_filter("money")
    def format_money(value):
        amount = float(value)
        if amount.is_integer():
            return f"{int(amount):,}".replace(",", " ")
        return f"{amount:,.2f}".replace(",", " ").replace(".", ",")

    @app.teardown_appcontext
    def close_db(_error=None):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @app.errorhandler(403)
    def forbidden(error):
        return render_template("error.html", code=403, message=error.description), 403

    @app.errorhandler(404)
    def not_found(error):
        return render_template("error.html", code=404, message="Страница или объект не найдены."), 404

    @app.errorhandler(400)
    def bad_request(error):
        return render_template(
            "error.html",
            code=400,
            message=getattr(error, "description", "Некорректный запрос."),
        ), 400

    @app.get("/")
    def index():
        settings = get_settings()
        equipment_count = get_db().execute(
            "SELECT count(*) FROM equipment WHERE is_active = 1"
        ).fetchone()[0]
        return render_template(
            "index.html",
            settings=settings,
            equipment_count=equipment_count,
            club_now=club_now(settings["timezone"]),
        )

    @app.get("/equipment")
    def equipment_list():
        page = page_number()
        per_page = 9
        conditions = ["e.is_active = 1"]
        params = []
        equipment_type = request.args.get("type", "").strip()
        zone_id = request.args.get("zone", "").strip()
        settings = get_settings()
        date_value = request.args.get("date", club_now(settings["timezone"]).date().isoformat())
        try:
            selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            abort(400, description="Укажите дату в формате ГГГГ-ММ-ДД.")
        if equipment_type in ("PC", "console"):
            conditions.append("e.equipment_type = ?")
            params.append(equipment_type)
        if zone_id.isdigit():
            conditions.append("e.zone_id = ?")
            params.append(int(zone_id))
        where = " WHERE " + " AND ".join(conditions)
        db = get_db()
        total = db.execute(
            f"SELECT count(*) FROM equipment e{where}", params
        ).fetchone()[0]
        items = db.execute(
            "SELECT e.*, z.name AS zone_name FROM equipment e "
            "JOIN zones z ON z.id = e.zone_id"
            + where
            + " ORDER BY e.name LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
        zones = db.execute("SELECT id, name FROM zones ORDER BY name").fetchall()
        availability = {
            item["id"]: daily_slots(item["id"], selected_date, settings)
            for item in items
        }
        return render_template(
            "equipment.html",
            equipment=items,
            zones=zones,
            selected_type=equipment_type,
            selected_zone=zone_id,
            selected_date=date_value,
            availability=availability,
            page=page,
            pages=max(1, (total + per_page - 1) // per_page),
        )

    @app.get("/equipment/<int:equipment_id>")
    def equipment_detail(equipment_id):
        equipment = get_equipment(equipment_id)
        date_value = request.args.get("date", club_now(get_settings()["timezone"]).date().isoformat())
        try:
            selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            abort(400, description="Укажите дату в формате ГГГГ-ММ-ДД.")
        settings = get_settings()
        slots = daily_slots(equipment_id, selected_date, settings)
        time_options = []
        cursor = datetime.combine(selected_date, datetime.strptime(settings["opening_time"], "%H:%M").time())
        closing = datetime.combine(selected_date, datetime.strptime(settings["closing_time"], "%H:%M").time())
        while cursor <= closing:
            time_options.append(cursor.strftime("%H:%M"))
            cursor += timedelta(minutes=30)
        current_time = club_now(settings["timezone"])
        return render_template(
            "equipment_detail.html",
            equipment=equipment,
            selected_date=selected_date.isoformat(),
            slots=slots,
            time_options=time_options,
            today_value=current_time.date().isoformat(),
            current_minute=current_time.hour * 60 + current_time.minute,
            settings=settings,
        )

    @app.get("/equipment/<int:equipment_id>/slots")
    def equipment_slots(equipment_id):
        get_equipment(equipment_id)
        date_value = request.args.get("date", "")
        try:
            selected_date = datetime.strptime(date_value, "%Y-%m-%d").date()
        except ValueError:
            abort(400, description="Укажите дату в формате ГГГГ-ММ-ДД.")
        settings = get_settings()
        return jsonify(
            date=selected_date.isoformat(),
            slots=daily_slots(equipment_id, selected_date, settings),
        )

    @app.route("/register", methods=("GET", "POST"))
    def register():
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            errors = {}
            if not name or len(name) > 120:
                errors["name"] = "Укажите имя (до 120 символов)."
            if "@" not in email or len(email) > 254:
                errors["email"] = "Введите корректный email."
            if len(password) < 8:
                errors["password"] = "Пароль должен содержать не менее 8 символов."
            if errors:
                return render_template("register.html", errors=errors), 400
            db = get_db()
            try:
                cursor = db.execute(
                    "INSERT INTO users(name, email, password_hash, role, created_at) "
                    "VALUES (?, ?, ?, 'customer', ?)",
                    (name, email, generate_password_hash(password), utc_now()),
                )
                db.commit()
            except sqlite3.IntegrityError:
                db.rollback()
                return render_template(
                    "register.html", errors={"email": "Этот email уже зарегистрирован."}
                ), 400
            session.clear()
            session["user_id"] = cursor.lastrowid
            flash("Аккаунт создан. Добро пожаловать!", "success")
            return redirect(url_for("equipment_list"))
        return render_template("register.html", errors={})

    @app.route("/login", methods=("GET", "POST"))
    def login():
        errors = {}
        if request.method == "POST":
            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            user = get_db().execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email,)
            ).fetchone()
            if user is None or not check_password_hash(user["password_hash"], password):
                errors["email"] = "Неверный email или пароль."
                return render_template("login.html", errors=errors), 401
            session.clear()
            session["user_id"] = user["id"]
            flash("Вы вошли в аккаунт.", "success")
            return redirect(url_for("index"))
        return render_template("login.html", errors=errors)

    @app.post("/logout")
    def logout():
        session.clear()
        flash("Вы вышли из аккаунта.", "success")
        return redirect(url_for("index"))

    @app.get("/profile")
    @login_required
    def profile():
        return render_template("profile.html", user=current_user())

    @app.get("/my-bookings")
    @login_required
    def my_bookings():
        user = current_user()
        page = page_number()
        per_page = 10
        db = get_db()
        total = db.execute(
            "SELECT count(*) FROM bookings WHERE user_id = ?", (user["id"],)
        ).fetchone()[0]
        bookings = db.execute(
            "SELECT b.*, e.name AS equipment_name, e.equipment_type "
            "FROM bookings b JOIN equipment e ON e.id = b.equipment_id "
            "WHERE b.user_id = ? ORDER BY b.start_at DESC LIMIT ? OFFSET ?",
            (user["id"], per_page, (page - 1) * per_page),
        ).fetchall()
        return render_template(
            "my_bookings.html",
            bookings=bookings,
            page=page,
            pages=max(1, (total + per_page - 1) // per_page),
        )

    @app.post("/bookings")
    @login_required
    def create_booking():
        try:
            equipment_id = int(request.form.get("equipment_id", ""))
            selected_date = datetime.strptime(request.form.get("date", ""), "%Y-%m-%d").date()
            start_time = datetime.strptime(request.form.get("start_time", ""), "%H:%M").time()
            end_time = datetime.strptime(request.form.get("end_time", ""), "%H:%M").time()
            start_at = datetime.combine(selected_date, start_time)
            end_at = datetime.combine(selected_date, end_time)
        except (ValueError, TypeError):
            flash("Выберите устройство и корректный интервал.", "error")
            return redirect(url_for("equipment_list"))
        result, booking_id = create_booking_atomic(
            user_id=current_user()["id"],
            equipment_id=equipment_id,
            start_at=start_at,
            end_at=end_at,
        )
        if result != "ok":
            messages = {
                "not_found": "Оборудование не найдено.",
                "inactive": "Это оборудование временно недоступно для бронирования.",
                "past": "Начало брони должно быть в будущем.",
                "duration": "Минимальная длительность — 30 минут; время выбирается шагом 30 минут.",
                "hours": "Выбранный интервал находится вне часов работы клуба.",
                "occupied": "Это время уже занято. Выберите другой интервал.",
            }
            flash(messages.get(result, "Не удалось создать бронь."), "error")
            return redirect(url_for("equipment_detail", equipment_id=equipment_id))
        flash("Бронь подтверждена.", "success")
        return redirect(url_for("my_bookings"))

    @app.post("/bookings/<int:booking_id>/cancel")
    @login_required
    def cancel_booking(booking_id):
        booking = get_booking(booking_id)
        user = current_user()
        if booking["user_id"] != user["id"] and user["role"] != "admin":
            abort(403, description="Вы не можете отменить чужую бронь.")
        if booking["status"] != "confirmed":
            flash("Отменить можно только подтверждённую бронь.", "error")
            return redirect(url_for("my_bookings"))
        settings = get_settings()
        now = club_now(settings["timezone"]).replace(tzinfo=None).isoformat(timespec="minutes")
        if booking["start_at"] <= now:
            flash("Бронь можно отменить только до её начала.", "error")
            return redirect(url_for("my_bookings"))
        db = get_db()
        changed = db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE id = ? AND status = 'confirmed'",
            (booking_id,),
        ).rowcount
        db.commit()
        if changed != 1:
            flash("Статус брони уже изменился. Обновите страницу.", "error")
        else:
            flash("Бронь отменена; время снова доступно.", "success")
        return redirect(url_for("my_bookings"))

    @app.get("/admin")
    @admin_required
    def admin_dashboard():
        settings = get_settings()
        day = request.args.get("date", club_now(settings["timezone"]).date().isoformat())
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            abort(400, description="Укажите дату в формате ГГГГ-ММ-ДД.")
        status_filter = request.args.get("status", "")
        conditions = ["date(b.start_at) = ?"]
        params = [day]
        if status_filter in STATUSES:
            conditions.append("b.status = ?")
            params.append(status_filter)
        where = " WHERE " + " AND ".join(conditions)
        page = page_number()
        per_page = 12
        db = get_db()
        total = db.execute(
            "SELECT count(*) FROM bookings b" + where, params
        ).fetchone()[0]
        bookings = db.execute(
            "SELECT b.*, u.name AS customer_name, e.name AS equipment_name "
            "FROM bookings b JOIN users u ON u.id = b.user_id "
            "JOIN equipment e ON e.id = b.equipment_id"
            + where
            + " ORDER BY b.start_at LIMIT ? OFFSET ?",
            [*params, per_page, (page - 1) * per_page],
        ).fetchall()
        occupied = db.execute(
            "SELECT coalesce(sum((julianday(end_at) - julianday(start_at)) * 24), 0) "
            "FROM bookings WHERE date(start_at) = ? AND status IN ('confirmed', 'running')",
            (day,),
        ).fetchone()[0]
        users = db.execute(
            "SELECT id, name, email, role FROM users ORDER BY name"
        ).fetchall()
        all_equipment = db.execute(
            "SELECT e.*, z.name AS zone_name FROM equipment e "
            "JOIN zones z ON z.id = e.zone_id ORDER BY e.name"
        ).fetchall()
        return render_template(
            "admin.html",
            bookings=bookings,
            date_value=day,
            status_filter=status_filter,
            occupied_hours=round(occupied, 2),
            users=users,
            all_equipment=all_equipment,
            page=page,
            pages=max(1, (total + per_page - 1) // per_page),
        )

    @app.post("/bookings/<int:booking_id>/start")
    @admin_required
    def start_booking(booking_id):
        return change_booking_status(booking_id, "confirmed", "running", "Сеанс запущен.")

    @app.post("/bookings/<int:booking_id>/finish")
    @admin_required
    def finish_booking(booking_id):
        return change_booking_status(booking_id, "running", "completed", "Сеанс завершён.")

    @app.post("/bookings/<int:booking_id>/no-show")
    @admin_required
    def mark_no_show(booking_id):
        booking = get_booking(booking_id)
        settings = get_settings()
        now = club_now(settings["timezone"]).replace(tzinfo=None).isoformat(timespec="minutes")
        if booking["status"] != "confirmed" or booking["start_at"] > now:
            flash("Неявку можно отметить только после начала подтверждённой брони.", "error")
            return redirect(url_for("admin_dashboard"))
        return change_booking_status(booking_id, "confirmed", "no_show", "Отмечена неявка.")

    @app.get("/reports/utilization")
    @admin_required
    def utilization_report():
        settings = get_settings()
        today = club_now(settings["timezone"]).date()
        start_value = request.args.get("from", today.replace(day=1).isoformat())
        end_value = request.args.get("to", today.isoformat())
        try:
            start_date = datetime.strptime(start_value, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_value, "%Y-%m-%d").date()
        except ValueError:
            abort(400, description="Укажите даты отчёта в формате ГГГГ-ММ-ДД.")
        if start_date > end_date:
            abort(400, description="Начало периода не может быть позже его окончания.")
        db = get_db()
        completed = db.execute(
            "SELECT count(*) AS count, coalesce(sum(total_price), 0) AS revenue, "
            "coalesce(sum((julianday(end_at) - julianday(start_at)) * 24), 0) AS hours "
            "FROM bookings WHERE status = 'completed' "
            "AND date(start_at) >= ? AND date(start_at) <= ?",
            (start_value, end_value),
        ).fetchone()
        occupied = db.execute(
            "SELECT coalesce(sum((julianday(end_at) - julianday(start_at)) * 24), 0) "
            "FROM bookings WHERE status IN ('confirmed', 'running') "
            "AND date(start_at) >= ? AND date(start_at) <= ?",
            (start_value, end_value),
        ).fetchone()[0]
        return render_template(
            "report.html",
            start_value=start_value,
            end_value=end_value,
            completed=completed,
            occupied_hours=round(occupied, 2),
        )

    @app.route("/admin/equipment/new", methods=("GET", "POST"))
    @admin_required
    def add_equipment():
        return equipment_form(None)

    @app.route("/admin/equipment/<int:equipment_id>/edit", methods=("GET", "POST"))
    @admin_required
    def edit_equipment(equipment_id):
        return equipment_form(get_equipment(equipment_id))

    @app.post("/admin/users/<int:user_id>/role")
    @admin_required
    def set_user_role(user_id):
        role = request.form.get("role", "")
        if role not in ("customer", "admin"):
            abort(400, description="Недопустимая роль.")
        if user_id == current_user()["id"] and role != "admin":
            flash("Нельзя снять роль администратора со своего аккаунта.", "error")
            return redirect(url_for("admin_dashboard"))
        cursor = get_db().execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        get_db().commit()
        if cursor.rowcount == 0:
            abort(404)
        flash("Роль пользователя обновлена.", "success")
        return redirect(url_for("admin_dashboard"))

    @app.route("/admin/settings", methods=("GET", "POST"))
    @admin_required
    def edit_settings():
        settings = get_settings()
        errors = {}
        if request.method == "POST":
            address = request.form.get("address", "").strip()
            opening = request.form.get("opening_time", "")
            closing = request.form.get("closing_time", "")
            timezone = request.form.get("timezone", "").strip()
            try:
                opening_time = datetime.strptime(opening, "%H:%M").time()
                closing_time = datetime.strptime(closing, "%H:%M").time()
                if opening_time >= closing_time:
                    errors["closing_time"] = "Закрытие должно быть позже открытия; переход через полночь не поддерживается."
                if opening_time.minute not in (0, 30) or closing_time.minute not in (0, 30):
                    errors["opening_time"] = "Часы работы должны совпадать с 30-минутным шагом бронирования."
            except ValueError:
                errors["opening_time"] = "Введите время в формате ЧЧ:ММ."
            if not address or len(address) > 240:
                errors["address"] = "Укажите адрес клуба (до 240 символов)."
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError):
                errors["timezone"] = "Укажите корректную IANA timezone, например Asia/Almaty."
            if not errors:
                get_db().execute(
                    "UPDATE club_settings SET address = ?, opening_time = ?, closing_time = ?, timezone = ? WHERE id = 1",
                    (address, opening, closing, timezone),
                )
                get_db().commit()
                flash("Настройки клуба сохранены.", "success")
                return redirect(url_for("edit_settings"))
        return render_template("settings.html", settings=settings, errors=errors)

    @app.cli.command("init-db")
    def init_db_command():
        initialize_database(app)
        print("Database initialized.")

    @app.cli.command("seed")
    def seed_command():
        initialize_database(app)
        seed_database(app)
        print("Demo data created. Admin: admin@gametime.test / Admin123!")

    with app.app_context():
        initialize_database(app)

    return app


def get_db():
    if "db" not in g:
        connection = sqlite3.connect(current_app.config["DATABASE"], timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        g.db = connection
    return g.db


def initialize_database(app):
    db_path = app.config["DATABASE"]
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    connection.execute(
        "INSERT OR IGNORE INTO club_settings(id, opening_time, closing_time, timezone) "
        "VALUES (1, '10:00', '23:00', 'Asia/Almaty')"
    )
    connection.commit()
    connection.close()


def seed_database(app):
    with app.app_context():
        db = get_db()
        if db.execute("SELECT count(*) FROM users").fetchone()[0]:
            return
        now = utc_now()
        accounts = [
            ("Администратор", "admin@gametime.test", "Admin123!", "admin"),
            ("Алия Демидова", "aliya@gametime.test", "Client123!", "customer"),
            ("Тимур Орлов", "timur@gametime.test", "Client123!", "customer"),
            ("Марк Соколов", "mark@gametime.test", "Client123!", "customer"),
        ]
        user_ids = {}
        for name, email, password, role in accounts:
            cursor = db.execute(
                "INSERT INTO users(name, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, email, generate_password_hash(password), role, now),
            )
            user_ids[email] = cursor.lastrowid
        zone_ids = []
        for name in ("Арена", "Лаунж"):
            zone_ids.append(
                db.execute("INSERT INTO zones(name) VALUES (?)", (name,)).lastrowid
            )
        devices = [
            ("PC Alpha", "PC", "Ryzen 5 5600 · RTX 4060 · 16 ГБ RAM", 1200, 0),
            ("PC Bravo", "PC", "Ryzen 5 5600 · RTX 4060 · 16 ГБ RAM", 1200, 0),
            ("PC Charlie", "PC", "Core i5 · RTX 3060 · 16 ГБ RAM", 1000, 0),
            ("PC Delta", "PC", "Core i7 · RTX 4070 · 32 ГБ RAM", 1600, 0),
            ("PC Echo", "PC", "Core i5 · RTX 3060 · 16 ГБ RAM", 1000, 1),
            ("PlayStation 5", "console", "PS5 · 2 контроллера · 4K TV", 1500, 1),
            ("Xbox Series X", "console", "Xbox Series X · 2 контроллера", 1400, 1),
            ("Nintendo Switch", "console", "Switch OLED · 2 Joy-Con", 900, 1),
        ]
        equipment_ids = []
        for index, (name, kind, specs, rate, zone_index) in enumerate(devices):
            cursor = db.execute(
                "INSERT INTO equipment(zone_id, name, equipment_type, specifications, hourly_rate, is_active) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (zone_ids[zone_index], name, kind, specs, rate),
            )
            equipment_ids.append(cursor.lastrowid)

        zone = ZoneInfo("Asia/Almaty")
        today = datetime.now(zone).date()
        demo = [
            (0, 1, 10, 11, "completed"),
            (1, 2, 11, 12, "completed"),
            (2, 3, 12, 14, "completed"),
            (3, 1, 14, 15, "completed"),
            (4, 2, 15, 16, "completed"),
            (5, 3, 16, 17, "completed"),
            (6, 1, 17, 18, "completed"),
            (7, 2, 18, 19, "completed"),
            (0, 3, 10, 11, "cancelled"),
            (1, 1, 12, 13, "no_show"),
            (2, 2, 13, 14, "confirmed"),
            (3, 3, 14, 15, "confirmed"),
            (4, 1, 15, 16, "confirmed"),
            (5, 2, 16, 17, "running"),
            (6, 3, 17, 18, "confirmed"),
        ]
        customers = [user_ids["aliya@gametime.test"], user_ids["timur@gametime.test"], user_ids["mark@gametime.test"]]
        rates = {equipment_ids[i]: devices[i][3] for i in range(len(devices))}
        for index, (device_idx, customer_idx, start_hour, end_hour, status) in enumerate(demo):
            day = today - timedelta(days=2) if index < 8 else today
            start_dt = datetime(day.year, day.month, day.day, start_hour, 0, tzinfo=zone)
            end_dt = datetime(day.year, day.month, day.day, end_hour, 0, tzinfo=zone)
            start_text = start_dt.replace(tzinfo=None).isoformat(timespec="minutes")
            end_text = end_dt.replace(tzinfo=None).isoformat(timespec="minutes")
            rate = rates[equipment_ids[device_idx]]
            duration = end_hour - start_hour
            db.execute(
                "INSERT INTO bookings(user_id, equipment_id, start_at, end_at, status, rate_snapshot, total_price, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    customers[customer_idx - 1],
                    equipment_ids[device_idx],
                    start_text,
                    end_text,
                    status,
                    rate,
                    rate * duration,
                    now,
                ),
            )
        db.commit()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return get_db().execute(
        "SELECT id, name, email, role FROM users WHERE id = ?", (user_id,)
    ).fetchone()


def csrf_token():
    if "_csrf_token" not in session:
        import secrets

        session["_csrf_token"] = secrets.token_urlsafe(32)
    return session["_csrf_token"]


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_user() is None:
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = current_user()
        if user is None:
            return redirect(url_for("login", next=request.path))
        if user["role"] != "admin":
            abort(403, description="Эта функция доступна только администратору.")
        return view(*args, **kwargs)

    return wrapped


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def club_now(timezone):
    return datetime.now(ZoneInfo(timezone))


def get_settings():
    row = get_db().execute("SELECT * FROM club_settings WHERE id = 1").fetchone()
    if row is None:
        raise RuntimeError("Club settings are missing; run database initialization.")
    return row


def get_equipment(equipment_id):
    equipment = get_db().execute(
        "SELECT e.*, z.name AS zone_name FROM equipment e "
        "JOIN zones z ON z.id = e.zone_id WHERE e.id = ?",
        (equipment_id,),
    ).fetchone()
    if equipment is None:
        abort(404)
    return equipment


def get_booking(booking_id):
    booking = get_db().execute("SELECT * FROM bookings WHERE id = ?", (booking_id,)).fetchone()
    if booking is None:
        abort(404)
    return booking


def page_number():
    value = request.args.get("page", "1")
    return max(1, int(value)) if value.isdigit() else 1


def create_booking_atomic(user_id, equipment_id, start_at, end_at):
    db = get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        equipment = db.execute(
            "SELECT * FROM equipment WHERE id = ?", (equipment_id,)
        ).fetchone()
        if equipment is None:
            db.rollback()
            return "not_found", None
        if not equipment["is_active"]:
            db.rollback()
            return "inactive", None
        settings = db.execute("SELECT * FROM club_settings WHERE id = 1").fetchone()
        timezone = ZoneInfo(settings["timezone"])
        now = datetime.now(timezone).replace(tzinfo=None)
        if start_at <= now:
            db.rollback()
            return "past", None
        if end_at <= start_at or (end_at - start_at).total_seconds() < 1800:
            db.rollback()
            return "duration", None
        if (
            start_at.second
            or start_at.microsecond
            or start_at.minute not in (0, 30)
            or end_at.second
            or end_at.microsecond
            or end_at.minute not in (0, 30)
        ):
            db.rollback()
            return "duration", None
        opening = datetime.strptime(settings["opening_time"], "%H:%M").time()
        closing = datetime.strptime(settings["closing_time"], "%H:%M").time()
        if (
            start_at.date() != end_at.date()
            or start_at.time() < opening
            or end_at.time() > closing
        ):
            db.rollback()
            return "hours", None
        start_text = start_at.isoformat(timespec="minutes")
        end_text = end_at.isoformat(timespec="minutes")
        overlap = db.execute(
            "SELECT 1 FROM bookings WHERE equipment_id = ? "
            "AND status IN ('confirmed', 'running') "
            "AND start_at < ? AND end_at > ? LIMIT 1",
            (equipment_id, end_text, start_text),
        ).fetchone()
        if overlap:
            db.rollback()
            return "occupied", None
        duration_hours = (end_at - start_at).total_seconds() / 3600
        total_price = equipment["hourly_rate"] * duration_hours
        if total_price.is_integer():
            total_price = int(total_price)
        cursor = db.execute(
            "INSERT INTO bookings(user_id, equipment_id, start_at, end_at, status, rate_snapshot, total_price, created_at) "
            "VALUES (?, ?, ?, ?, 'confirmed', ?, ?, ?)",
            (
                user_id,
                equipment_id,
                start_text,
                end_text,
                equipment["hourly_rate"],
                total_price,
                utc_now(),
            ),
        )
        db.commit()
        return "ok", cursor.lastrowid
    except Exception:
        db.rollback()
        raise


def daily_slots(equipment_id, selected_date, settings):
    opening = datetime.strptime(settings["opening_time"], "%H:%M")
    closing = datetime.strptime(settings["closing_time"], "%H:%M")
    start_of_day = datetime.combine(selected_date, opening.time()).isoformat(timespec="minutes")
    end_of_day = datetime.combine(selected_date, closing.time()).isoformat(timespec="minutes")
    rows = get_db().execute(
        "SELECT start_at, end_at FROM bookings WHERE equipment_id = ? "
        "AND status IN ('confirmed', 'running') AND start_at < ? AND end_at > ? "
        "ORDER BY start_at",
        (equipment_id, end_of_day, start_of_day),
    ).fetchall()
    return [
        {"start": row["start_at"], "end": row["end_at"]}
        for row in rows
    ]


def change_booking_status(booking_id, expected, target, message):
    get_booking(booking_id)
    cursor = get_db().execute(
        "UPDATE bookings SET status = ? WHERE id = ? AND status = ?",
        (target, booking_id, expected),
    )
    get_db().commit()
    if cursor.rowcount != 1:
        flash("Переход статуса невозможен: бронь уже изменилась или имеет другой статус.", "error")
    else:
        flash(message, "success")
    return redirect(url_for("admin_dashboard"))


def equipment_form(equipment):
    db = get_db()
    zones = db.execute("SELECT id, name FROM zones ORDER BY name").fetchall()
    errors = {}
    values = dict(equipment) if equipment else {}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        equipment_type = request.form.get("equipment_type", "")
        specifications = request.form.get("specifications", "").strip()
        try:
            zone_id = int(request.form.get("zone_id", ""))
        except ValueError:
            zone_id = -1
        try:
            rate = int(request.form.get("hourly_rate", ""))
        except ValueError:
            rate = -1
        is_active = 1 if request.form.get("is_active") == "1" else 0
        if not name or len(name) > 120:
            errors["name"] = "Укажите название (до 120 символов)."
        if equipment_type not in ("PC", "console"):
            errors["equipment_type"] = "Выберите PC или консоль."
        if not any(zone["id"] == zone_id for zone in zones):
            errors["zone_id"] = "Выберите существующую зону."
        if rate < 0:
            errors["hourly_rate"] = "Тариф должен быть целым числом не меньше нуля."
        values.update(
            name=name,
            equipment_type=equipment_type,
            specifications=specifications,
            zone_id=zone_id,
            hourly_rate=rate,
            is_active=is_active,
        )
        if not errors:
            params = (zone_id, name, equipment_type, specifications, rate, is_active)
            if equipment:
                db.execute(
                    "UPDATE equipment SET zone_id = ?, name = ?, equipment_type = ?, "
                    "specifications = ?, hourly_rate = ?, is_active = ? WHERE id = ?",
                    (*params, equipment["id"]),
                )
                flash("Оборудование обновлено.", "success")
            else:
                db.execute(
                    "INSERT INTO equipment(zone_id, name, equipment_type, specifications, hourly_rate, is_active) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    params,
                )
                flash("Оборудование добавлено.", "success")
            db.commit()
            return redirect(url_for("admin_dashboard"))
    return render_template(
        "equipment_form.html", equipment=equipment, zones=zones, values=values, errors=errors
    )
