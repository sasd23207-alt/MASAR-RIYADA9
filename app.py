import os, sqlite3, secrets, uuid, time, csv, io
from urllib.parse import quote
from datetime import datetime, timedelta, date
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, abort, send_from_directory, Response, send_file)
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", os.path.join(BASE, "data"))
UPLOADS = os.path.join(DATA, "uploads")
os.makedirs(UPLOADS, exist_ok=True)
DB = os.path.join(DATA, "office.db")

STATUSES = ["جديد", "قيد الدراسة", "عرض سعر مرسل", "بانتظار المستندات",
            "قيد التنفيذ", "جاهز للتسليم", "مكتمل", "ملغي"]
VISA_STATUSES = ["لم تُقدَّم", "مقدَّمة للسفارة", "صادرة", "مرفوضة"]
FAILS = {}
TYPES = {"flight": "حجز طيران", "hotel": "حجز فندق", "visa": "تأشيرة",
         "package": "باقة سياحية", "umrah": "عمرة / حج",
         "transfer": "نقل واستقبال", "other": "خدمة أخرى"}
ITEM_KINDS = {"flight": "تذكرة طيران", "hotel": "فندق", "visa": "رسوم تأشيرة",
              "transport": "نقل", "insurance": "تأمين", "other": "أخرى"}
VISA_CHECKLIST = ["جواز سفر ساري المفعول", "صورة شخصية بخلفية بيضاء",
                  "نموذج طلب التأشيرة معبأ", "حجز الفندق", "حجز الطيران",
                  "إثبات مالي"]
PAY_METHODS = ["نقدًا", "تحويل مصرفي", "بطاقة", "صك", "أخرى"]
ALLOWED = {"pdf", "jpg", "jpeg", "png", "doc", "docx"}

SCHEMA = """
create table if not exists settings(key text primary key, value text);
create table if not exists currencies(code text primary key, rate real not null);
create table if not exists users(
  id integer primary key, name text not null, phone text unique not null,
  email text, password_hash text not null, role text not null default 'client',
  created text);
create table if not exists requests(
  id integer primary key, ref text, client_id integer not null, type text not null,
  destination text, depart_date text, return_date text, pax integer default 1,
  notes text, status text not null default 'جديد', assigned_to integer,
  price real default 0, currency text, internal_note text, created text);
create table if not exists travelers(
  id integer primary key, request_id integer not null, full_name text not null,
  passport_no text, passport_expiry text, birth_date text, nationality text);
create table if not exists documents(
  id integer primary key, request_id integer not null, label text,
  original text, stored text, uploaded_by integer, created text);
create table if not exists messages(
  id integer primary key, request_id integer not null, sender_id integer,
  body text, created text);
create table if not exists items(
  id integer primary key, request_id integer not null, kind text,
  details text, supplier_id integer, cost real default 0, currency text,
  ref_code text);
create table if not exists checklist(
  id integer primary key, request_id integer not null, title text,
  done integer default 0);
create table if not exists payments(
  id integer primary key, request_id integer not null, amount real,
  currency text, method text, kind text default 'received', note text,
  created text);
create table if not exists suppliers(
  id integer primary key, name text not null, kind text, phone text, notes text);
create table if not exists itinerary(
  id integer primary key, request_id integer not null, day text, title text, details text);
create table if not exists tasks(
  id integer primary key, request_id integer, title text not null, due text,
  done integer default 0, assigned_to integer, created text);
create table if not exists log(
  id integer primary key, request_id integer not null, user_id integer, text text, created text);
create table if not exists packages(
  id integer primary key, name text not null, destination text, duration text,
  price real, currency text, description text, active integer default 1);
"""

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
_kf = os.path.join(DATA, "secret.key")
if not os.path.exists(_kf):
    open(_kf, "w").write(secrets.token_hex(32))
app.secret_key = os.environ.get("SECRET_KEY") or open(_kf).read()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SECURE_COOKIES") == "1"
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)


# ---------- قاعدة البيانات ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d:
        d.close()


def q(sql, a=()):
    return db().execute(sql, a).fetchall()


def q1(sql, a=()):
    return db().execute(sql, a).fetchone()


def ex(sql, a=()):
    c = db().execute(sql, a)
    db().commit()
    return c.lastrowid


def setting(k, d=""):
    r = q1("select value from settings where key=?", (k,))
    return r["value"] if r else d


def set_setting(k, v):
    ex("insert into settings(key,value) values(?,?) on conflict(key) "
       "do update set value=excluded.value", (k, v))


def migrate():
    c = sqlite3.connect(DB)
    c.executescript(SCHEMA)
    adds = [("requests", "payment_due", "text"), ("requests", "quote_note", "text"),
            ("messages", "seen", "integer default 0"), ("items", "paid", "integer default 0"),
            ("travelers", "visa_status", "text"), ("travelers", "visa_no", "text"),
            ("travelers", "visa_expiry", "text")]
    for t, col, typ in adds:
        have = {r[1] for r in c.execute(f"pragma table_info({t})")}
        if col not in have:
            c.execute(f"alter table {t} add column {col} {typ}")
    c.commit()
    c.close()


migrate()


def log(rid, text):
    ex("insert into log(request_id,user_id,text,created) values(?,?,?,?)",
       (rid, g.user["id"] if g.get("user") else None, text, now()))


def unread():
    if not g.get("user"):
        return 0
    if g.user["role"] in ("admin", "staff"):
        return q1("select count(*) c from messages m join users u on u.id=m.sender_id "
                  "where m.seen=0 and u.role='client'")["c"]
    return q1("select count(*) c from messages m join requests r on r.id=m.request_id "
              "join users u on u.id=m.sender_id where r.client_id=? and m.seen=0 and u.role!='client'",
              (g.user["id"],))["c"]


@app.template_filter("wa")
def wa(phone, text=""):
    d = "".join(ch for ch in (phone or "") if ch.isdigit())
    cc = setting("country_code", "218")
    if d.startswith("00"):
        d = d[2:]
    elif d.startswith("0"):
        d = cc + d[1:]
    elif not d.startswith(cc) and len(d) <= 10:
        d = cc + d
    return "https://wa.me/" + d + ("?text=" + quote(text) if text else "")


def safe_next(n):
    return n if n and n.startswith("/") and not n.startswith("//") else url_for("home")


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def num(x):
    try:
        return float(str(x).replace(",", ".").strip() or 0)
    except ValueError:
        return 0.0


# ---------- العملات والحسابات ----------
def rates():
    base = setting("base_currency", "LYD")
    r = {c["code"]: c["rate"] for c in q("select * from currencies")}
    r[base] = 1.0
    return r


def currency_codes():
    base = setting("base_currency", "LYD")
    return [base] + [c["code"] for c in q("select code from currencies order by code")
                     if c["code"] != base]


def conv(a, c, R):
    return (a or 0) * R.get(c, 1.0)


def totals(r, R=None):
    R = R or rates()
    rid = r["id"]
    price = conv(r["price"], r["currency"], R)
    paid = sum(conv(p["amount"], p["currency"], R) * (-1 if p["kind"] == "refund" else 1)
               for p in q("select * from payments where request_id=?", (rid,)))
    cost = sum(conv(i["cost"], i["currency"], R)
               for i in q("select * from items where request_id=?", (rid,)))
    return dict(price=price, paid=paid, cost=cost, due=price - paid, profit=price - cost)


def missing_rates():
    R = rates()
    used = set()
    for t in ("requests", "items", "payments"):
        for row in q(f"select distinct currency c from {t} where currency is not null and currency!=''"):
            used.add(row["c"])
    return sorted(c for c in used if c not in R)


# ---------- الدخول والصلاحيات ----------
def csrf():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


@app.before_request
def pre():
    g.user = q1("select * from users where id=?", (session["uid"],)) if "uid" in session else None
    if request.endpoint in ("static", None):
        return
    if request.endpoint != "setup" and not q1("select 1 from users where role='admin'"):
        return redirect(url_for("setup"))
    if request.method == "POST":
        if not secrets.compare_digest(request.form.get("csrf", ""), session.get("csrf", "-")):
            abort(400)


@app.context_processor
def inject():
    return dict(csrf=csrf, office_name=setting("office_name", "مكتب السفر"),
                STATUSES=STATUSES, TYPES=TYPES, ITEM_KINDS=ITEM_KINDS,
                PAY_METHODS=PAY_METHODS, codes=currency_codes(), VISA_STATUSES=VISA_STATUSES,
                unread=unread(),
                base_cur=setting("base_currency", "LYD"))


def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            session["next"] = request.full_path.rstrip("?")
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def staff_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login"))
        if g.user["role"] not in ("admin", "staff"):
            abort(403)
        return f(*a, **k)
    return w


def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not g.user:
            return redirect(url_for("login"))
        if g.user["role"] != "admin":
            abort(403)
        return f(*a, **k)
    return w


def is_staff():
    return g.user and g.user["role"] in ("admin", "staff")


def get_req(rid):
    r = q1("select r.*, u.name client_name, u.phone client_phone, u.email client_email "
           "from requests r join users u on u.id=r.client_id where r.id=?", (rid,))
    if not r:
        abort(404)
    if not is_staff() and r["client_id"] != g.user["id"]:
        abort(403)
    return r


# ---------- الصفحات العامة ----------
@app.route("/")
def home():
    if g.user:
        return redirect(url_for("admin" if is_staff() else "client"))
    return render_template("landing.html", phone=setting("office_phone"),
                           address=setting("office_address"),
                           packages=q("select * from packages where active=1 order by id desc"))


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if q1("select 1 from users where role='admin'"):
        return redirect(url_for("home"))
    if request.method == "POST":
        f = request.form
        if not (f["name"].strip() and f["phone"].strip() and len(f["password"]) >= 6):
            flash("أكمل الحقول، وكلمة المرور 6 خانات على الأقل")
        else:
            set_setting("office_name", f["office_name"].strip() or "مكتب السفر")
            cur = f["currency"].strip().upper() or "LYD"
            set_setting("base_currency", cur)
            ex("insert into users(name,phone,password_hash,role,created) values(?,?,?,?,?)",
               (f["name"].strip(), f["phone"].strip(), generate_password_hash(f["password"]),
                "admin", now()))
            flash("تم تجهيز المنظومة، سجّل دخولك")
            return redirect(url_for("login"))
    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr
        n, t0 = FAILS.get(ip, (0, 0))
        if n >= 8 and time.time() - t0 < 300:
            flash("محاولات كثيرة، انتظر 5 دقائق ثم حاول")
            return render_template("login.html")
        u = q1("select * from users where phone=?", (request.form["phone"].strip(),))
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            FAILS.pop(ip, None)
            nxt = session.get("next")
            session.clear()
            session["uid"] = u["id"]
            return redirect(safe_next(nxt))
        FAILS[ip] = (n + 1 if time.time() - t0 < 300 else 1, time.time())
        flash("رقم الهاتف أو كلمة المرور غير صحيحة")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        f = request.form
        if not f["name"].strip() or not f["phone"].strip() or len(f["password"]) < 6:
            flash("أكمل الحقول، وكلمة المرور 6 خانات على الأقل")
        elif q1("select 1 from users where phone=?", (f["phone"].strip(),)):
            flash("رقم الهاتف مسجّل من قبل، جرّب الدخول")
        else:
            uid = ex("insert into users(name,phone,email,password_hash,role,created) values(?,?,?,?,?,?)",
                     (f["name"].strip(), f["phone"].strip(), f.get("email", "").strip(),
                      generate_password_hash(f["password"]), "client", now()))
            nxt = session.get("next")
            session.clear()
            session["uid"] = uid
            return redirect(safe_next(nxt) if nxt else url_for("client"))
    return render_template("register.html")


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------- قسم العميل ----------
@app.route("/client")
@login_required
def client():
    if is_staff():
        return redirect(url_for("admin"))
    rows = q("select * from requests where client_id=? order by id desc", (g.user["id"],))
    return render_template("client_home.html", rows=rows,
                           packages=q("select * from packages where active=1 order by id desc"))


@app.route("/client/new", methods=["GET", "POST"])
@login_required
def client_new():
    if is_staff():
        return redirect(url_for("admin_new"))
    if request.method == "POST":
        rid = create_request(g.user["id"], request.form)
        flash("وصل طلبك للمكتب، وراح يتواصلون معك")
        return redirect(url_for("request_view", rid=rid))
    pk = None
    if request.args.get("package"):
        pk = q1("select * from packages where id=? and active=1", (request.args["package"],))
    return render_template("request_new.html", clients=None, pk=pk)


def create_request(client_id, f):
    t = f.get("type") if f.get("type") in TYPES else "other"
    rid = ex("insert into requests(client_id,type,destination,depart_date,return_date,pax,notes,"
             "currency,created) values(?,?,?,?,?,?,?,?,?)",
             (client_id, t, f.get("destination", "").strip(), f.get("depart_date", ""),
              f.get("return_date", ""), int(num(f.get("pax")) or 1), f.get("notes", "").strip(),
              setting("base_currency", "LYD"), now()))
    ex("update requests set ref=? where id=?", (f"R{rid:05d}", rid))
    log(rid, "تم إنشاء الطلب")
    return rid


@app.route("/request/<int:rid>")
@login_required
def request_view(rid):
    r = get_req(rid)
    if is_staff():
        ex("update messages set seen=1 where request_id=? and sender_id in "
           "(select id from users where role='client')", (rid,))
    else:
        ex("update messages set seen=1 where request_id=? and sender_id in "
           "(select id from users where role!='client')", (rid,))
    ctx = dict(r=r, travelers=q("select * from travelers where request_id=?", (rid,)),
               docs=q("select * from documents where request_id=? order by id", (rid,)),
               msgs=q("select m.*, u.name, u.role from messages m left join users u on u.id=m.sender_id "
                      "where request_id=? order by m.id", (rid,)),
               check=q("select * from checklist where request_id=?", (rid,)),
               pays=q("select * from payments where request_id=? order by id", (rid,)),
               iti=q("select * from itinerary where request_id=? order by day, id", (rid,)),
               t=totals(r))
    if is_staff():
        return render_template(
            "admin_request.html", items=q(
                "select i.*, s.name sname from items i left join suppliers s on s.id=i.supplier_id "
                "where request_id=?", (rid,)),
            suppliers=q("select * from suppliers order by name"),
            staff=q("select * from users where role in ('admin','staff')"),
            warns=passport_warnings(r),
            tasks=q("select t.*, u.name uname from tasks t left join users u on u.id=t.assigned_to "
                    "where request_id=? order by done, due", (rid,)),
            logs=q("select l.*, u.name from log l left join users u on u.id=l.user_id "
                   "where request_id=? order by l.id desc limit 40", (rid,)), **ctx)
    return render_template("client_request.html", **ctx)


@app.route("/request/<int:rid>/itinerary")
@login_required
def itinerary(rid):
    r = get_req(rid)
    return render_template("itinerary.html", r=r,
                           iti=q("select * from itinerary where request_id=? order by day, id", (rid,)),
                           travelers=q("select * from travelers where request_id=?", (rid,)),
                           phone=setting("office_phone"), address=setting("office_address"))


@app.post("/request/<int:rid>/quote")
@login_required
def quote_decision(rid):
    r = get_req(rid)
    if is_staff() or r["status"] != "عرض سعر مرسل":
        abort(403)
    if request.form.get("d") == "accept":
        ex("update requests set status='بانتظار المستندات' where id=?", (rid,))
        log(rid, "العميل قبل عرض السعر")
        body = "✔ قبلت عرض السعر"
    else:
        ex("update requests set status='قيد الدراسة' where id=?", (rid,))
        log(rid, "العميل طلب تعديل العرض")
        body = "طلب تعديل على العرض: " + request.form.get("reason", "").strip()
    ex("insert into messages(request_id,sender_id,body,created) values(?,?,?,?)",
       (rid, g.user["id"], body, now()))
    return redirect(url_for("request_view", rid=rid))


def passport_warnings(r):
    out = []
    try:
        dep = datetime.strptime(r["depart_date"], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return out
    for t in q("select * from travelers where request_id=?", (r["id"],)):
        try:
            exp = datetime.strptime(t["passport_expiry"], "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if exp < dep + timedelta(days=180):
            out.append(f"جواز {t['full_name']} ينتهي {t['passport_expiry']} وهذا أقل من 6 أشهر بعد السفر")
    return out


@app.post("/request/<int:rid>/traveler")
@login_required
def add_traveler(rid):
    get_req(rid)
    f = request.form
    if f["full_name"].strip():
        ex("insert into travelers(request_id,full_name,passport_no,passport_expiry,birth_date,nationality)"
           " values(?,?,?,?,?,?)", (rid, f["full_name"].strip(), f.get("passport_no", "").strip(),
                                    f.get("passport_expiry", ""), f.get("birth_date", ""),
                                    f.get("nationality", "").strip()))
    return redirect(url_for("request_view", rid=rid))


@app.post("/request/<int:rid>/traveler/<int:tid>/delete")
@login_required
def del_traveler(rid, tid):
    get_req(rid)
    ex("delete from travelers where id=? and request_id=?", (tid, rid))
    return redirect(url_for("request_view", rid=rid))


@app.post("/request/<int:rid>/doc")
@login_required
def add_doc(rid):
    get_req(rid)
    fl = request.files.get("file")
    if fl and fl.filename:
        ext = fl.filename.rsplit(".", 1)[-1].lower() if "." in fl.filename else ""
        if ext not in ALLOWED:
            flash("نوع الملف غير مسموح (pdf, jpg, png, doc, docx)")
        else:
            name = uuid.uuid4().hex + "." + ext
            fl.save(os.path.join(UPLOADS, name))
            ex("insert into documents(request_id,label,original,stored,uploaded_by,created) values(?,?,?,?,?,?)",
               (rid, request.form.get("label", "").strip(), fl.filename, name, g.user["id"], now()))
    return redirect(url_for("request_view", rid=rid))


@app.route("/file/<int:did>")
@login_required
def get_file(did):
    d = q1("select * from documents where id=?", (did,))
    if not d:
        abort(404)
    get_req(d["request_id"])
    return send_from_directory(UPLOADS, d["stored"], as_attachment=True,
                               download_name=d["original"])


@app.post("/request/<int:rid>/msg")
@login_required
def add_msg(rid):
    get_req(rid)
    b = request.form.get("body", "").strip()
    if b:
        ex("insert into messages(request_id,sender_id,body,created) values(?,?,?,?)",
           (rid, g.user["id"], b, now()))
    return redirect(url_for("request_view", rid=rid) + "#msgs")


@app.post("/request/<int:rid>/cancel")
@login_required
def client_cancel(rid):
    r = get_req(rid)
    if r["status"] in ("جديد", "عرض سعر مرسل"):
        ex("update requests set status='ملغي' where id=?", (rid,))
        log(rid, "العميل ألغى الطلب")
    return redirect(url_for("request_view", rid=rid))


# ---------- قسم الإدارة ----------
@app.route("/admin")
@staff_required
def admin():
    counts = {s: 0 for s in STATUSES}
    for r in q("select status, count(*) c from requests group by status"):
        counts[r["status"]] = r["c"]
    today = date.today().isoformat()
    soon = (date.today() + timedelta(days=14)).isoformat()
    upcoming = q("select r.*, u.name client_name from requests r join users u on u.id=r.client_id "
                 "where depart_date between ? and ? and status not in ('ملغي') order by depart_date",
                 (today, soon))
    fresh = q("select r.*, u.name client_name from requests r join users u on u.id=r.client_id "
              "where status='جديد' order by r.id desc limit 10")
    R = rates()
    owing = []
    for r in q("select r.*, u.name client_name from requests r join users u on u.id=r.client_id "
               "where status not in ('ملغي') and price>0"):
        t = totals(r, R)
        if t["due"] > 0.001:
            owing.append((r, t))
    month = date.today().strftime("%Y-%m")
    mrows = [(r, totals(r, R)) for r in q(
        "select * from requests where status!='ملغي' and price>0 and created like ?", (month + "%",))]
    kpi = dict(sales=sum(t["price"] for _, t in mrows), profit=sum(t["profit"] for _, t in mrows),
               n=len(mrows))
    mytasks = q("select t.*, r.ref from tasks t left join requests r on r.id=t.request_id "
                "where done=0 and due!='' and due<=? order by due", (today,))
    overdue = [(r, t) for r, t in owing if r["payment_due"] and r["payment_due"] < today]
    visas = q("select tr.*, r.ref, r.id rid from travelers tr join requests r on r.id=tr.request_id "
              "where tr.visa_status='مقدَّمة للسفارة' and r.status!='ملغي'")
    return render_template("admin_dashboard.html", counts=counts, upcoming=upcoming, fresh=fresh,
                           owing=owing[:10], owing_total=sum(t["due"] for _, t in owing),
                           missing=missing_rates(), kpi=kpi, mytasks=mytasks, overdue=overdue,
                           visas=visas)


@app.route("/admin/requests")
@staff_required
def admin_requests():
    st, ty, s = request.args.get("status", ""), request.args.get("type", ""), request.args.get("q", "").strip()
    sql = ("select r.*, u.name client_name, u.phone client_phone, (select count(*) from messages m "
           "join users s on s.id=m.sender_id where m.request_id=r.id and m.seen=0 and s.role='client') unread "
           "from requests r join users u on u.id=r.client_id where 1=1")
    a = []
    if st:
        sql += " and r.status=?"; a.append(st)
    if ty:
        sql += " and r.type=?"; a.append(ty)
    if s:
        sql += " and (r.ref like ? or u.name like ? or u.phone like ? or r.destination like ?)"
        a += [f"%{s}%"] * 4
    rows = q(sql + " order by r.id desc", a)
    return render_template("admin_requests.html", rows=rows, st=st, ty=ty, s=s)


@app.route("/admin/new", methods=["GET", "POST"])
@staff_required
def admin_new():
    if request.method == "POST":
        cid = request.form.get("client_id")
        if not q1("select 1 from users where id=? and role='client'", (cid,)):
            flash("اختر عميلًا")
        else:
            rid = create_request(int(cid), request.form)
            return redirect(url_for("request_view", rid=rid))
    return render_template("request_new.html",
                           clients=q("select * from users where role='client' order by name"))


@app.route("/admin/clients", methods=["GET", "POST"])
@staff_required
def admin_clients():
    if request.method == "POST":
        f = request.form
        if f["name"].strip() and f["phone"].strip():
            if q1("select 1 from users where phone=?", (f["phone"].strip(),)):
                flash("الرقم مسجّل من قبل")
            else:
                ex("insert into users(name,phone,email,password_hash,role,created) values(?,?,?,?,?,?)",
                   (f["name"].strip(), f["phone"].strip(), f.get("email", "").strip(),
                    generate_password_hash(secrets.token_hex(8)), "client", now()))
                flash("تمت إضافة العميل. لو يبي يدخل بنفسه افتح ملفه وعيّن له كلمة مرور")
    s = request.args.get("q", "").strip()
    rows = q("select u.*, (select count(*) from requests where client_id=u.id) n from users u "
             "where role='client' and (name like ? or phone like ?) order by id desc", (f"%{s}%", f"%{s}%"))
    return render_template("admin_clients.html", rows=rows, s=s)


@app.route("/admin/client/<int:uid>", methods=["GET", "POST"])
@staff_required
def admin_client(uid):
    u = q1("select * from users where id=? and role='client'", (uid,))
    if not u:
        abort(404)
    if request.method == "POST":
        pw = request.form.get("password", "")
        if len(pw) >= 6:
            ex("update users set password_hash=? where id=?", (generate_password_hash(pw), uid))
            flash("تم تعيين كلمة المرور الجديدة")
        else:
            flash("كلمة المرور 6 خانات على الأقل")
    return render_template("admin_client.html", u=u,
                           rows=q("select * from requests where client_id=? order by id desc", (uid,)))


@app.post("/admin/request/<int:rid>/<action>")
@staff_required
def admin_action(rid, action):
    r = get_req(rid)
    f = request.form
    if action == "update":
        st = f["status"] if f["status"] in STATUSES else r["status"]
        ex("update requests set status=?,price=?,currency=?,assigned_to=?,internal_note=?,"
           "payment_due=?,quote_note=? where id=?",
           (st, num(f.get("price")), f.get("currency"), f.get("assigned_to") or None,
            f.get("internal_note", ""), f.get("payment_due", ""), f.get("quote_note", ""), rid))
        if st != r["status"]:
            log(rid, f"تغيير الحالة: {r['status']} ← {st}")
        if num(f.get("price")) != (r["price"] or 0):
            log(rid, f"تعديل السعر إلى {num(f.get('price')):.2f} {f.get('currency')}")
    elif action == "item_add":
        ex("insert into items(request_id,kind,details,supplier_id,cost,currency,ref_code) values(?,?,?,?,?,?,?)",
           (rid, f.get("kind"), f.get("details", "").strip(), f.get("supplier_id") or None,
            num(f.get("cost")), f.get("currency"), f.get("ref_code", "").strip()))
        log(rid, f"إضافة خدمة: {ITEM_KINDS.get(f.get('kind'), '')} {f.get('details', '').strip()}")
    elif action == "item_pay":
        ex("update items set paid=1-paid where id=? and request_id=?", (f.get("id"), rid))
    elif action == "task_add":
        if f.get("title", "").strip():
            ex("insert into tasks(request_id,title,due,assigned_to,created) values(?,?,?,?,?)",
               (rid, f["title"].strip(), f.get("due", ""), f.get("assigned_to") or None, now()))
    elif action == "iti_add":
        if f.get("title", "").strip():
            ex("insert into itinerary(request_id,day,title,details) values(?,?,?,?)",
               (rid, f.get("day", ""), f["title"].strip(), f.get("details", "").strip()))
    elif action == "iti_del":
        ex("delete from itinerary where id=? and request_id=?", (f.get("id"), rid))
    elif action == "visa_update":
        vs = f.get("visa_status") if f.get("visa_status") in VISA_STATUSES else VISA_STATUSES[0]
        ex("update travelers set visa_status=?,visa_no=?,visa_expiry=? where id=? and request_id=?",
           (vs, f.get("visa_no", "").strip(), f.get("visa_expiry", ""), f.get("id"), rid))
        tr = q1("select full_name from travelers where id=?", (f.get("id"),))
        if tr:
            log(rid, f"تأشيرة {tr['full_name']}: {vs}")
    elif action == "item_del":
        ex("delete from items where id=? and request_id=?", (f.get("id"), rid))
    elif action == "check_add":
        if f.get("title", "").strip():
            ex("insert into checklist(request_id,title) values(?,?)", (rid, f["title"].strip()))
    elif action == "check_defaults":
        have = {c["title"] for c in q("select title from checklist where request_id=?", (rid,))}
        for t in VISA_CHECKLIST:
            if t not in have:
                ex("insert into checklist(request_id,title) values(?,?)", (rid, t))
    elif action == "check_toggle":
        ex("update checklist set done=1-done where id=? and request_id=?", (f.get("id"), rid))
    elif action == "check_del":
        ex("delete from checklist where id=? and request_id=?", (f.get("id"), rid))
    elif action == "pay_add":
        if num(f.get("amount")) > 0:
            ex("insert into payments(request_id,amount,currency,method,kind,note,created) values(?,?,?,?,?,?,?)",
               (rid, num(f["amount"]), f.get("currency"), f.get("method"),
                "refund" if f.get("kind") == "refund" else "received", f.get("note", "").strip(), now()))
            log(rid, f"{'استرجاع' if f.get('kind') == 'refund' else 'دفعة'} {num(f['amount']):.2f} {f.get('currency')}")
    elif action == "pay_del":
        ex("delete from payments where id=? and request_id=?", (f.get("id"), rid))
    elif action == "doc_del":
        d = q1("select * from documents where id=? and request_id=?", (f.get("id"), rid))
        if d:
            try:
                os.remove(os.path.join(UPLOADS, d["stored"]))
            except OSError:
                pass
            ex("delete from documents where id=?", (d["id"],))
    else:
        abort(404)
    return redirect(url_for("request_view", rid=rid))


@app.route("/request/<int:rid>/invoice")
@login_required
def invoice(rid):
    r = get_req(rid)
    return render_template("invoice.html", r=r, t=totals(r),
                           items=q("select * from items where request_id=?", (rid,)),
                           travelers=q("select * from travelers where request_id=?", (rid,)),
                           pays=q("select * from payments where request_id=?", (rid,)),
                           phone=setting("office_phone"), address=setting("office_address"))


@app.route("/admin/finance")
@staff_required
def finance():
    m = request.args.get("m", "")
    sql = ("select r.*, u.name client_name from requests r join users u on u.id=r.client_id "
           "where status!='ملغي' and price>0")
    a = []
    if m:
        sql += " and r.created like ?"; a.append(m + "%")
    R = rates()
    rows = [(r, totals(r, R)) for r in q(sql + " order by r.id desc", a)]
    tot = {k: sum(t[k] for _, t in rows) for k in ("price", "paid", "due", "cost", "profit")}
    return render_template("finance.html", rows=rows, tot=tot, m=m, missing=missing_rates())


@app.route("/admin/suppliers", methods=["GET", "POST"])
@staff_required
def suppliers():
    if request.method == "POST":
        f = request.form
        if f.get("delete"):
            ex("update items set supplier_id=null where supplier_id=?", (f["delete"],))
            ex("delete from suppliers where id=?", (f["delete"],))
        elif f["name"].strip():
            ex("insert into suppliers(name,kind,phone,notes) values(?,?,?,?)",
               (f["name"].strip(), f.get("kind", "").strip(), f.get("phone", "").strip(),
                f.get("notes", "").strip()))
    R = rates()
    rows = []
    for s in q("select * from suppliers order by name"):
        its = q("select * from items where supplier_id=?", (s["id"],))
        c = sum(conv(i["cost"], i["currency"], R) for i in its)
        pd = sum(conv(i["cost"], i["currency"], R) for i in its if i["paid"])
        rows.append((s, c, pd))
    return render_template("suppliers.html", rows=rows)


@app.route("/admin/staff", methods=["GET", "POST"])
@admin_required
def staff():
    if request.method == "POST":
        f = request.form
        if f.get("delete"):
            if int(f["delete"]) != g.user["id"]:
                ex("update requests set assigned_to=null where assigned_to=?", (f["delete"],))
                ex("delete from users where id=? and role='staff'", (f["delete"],))
        elif f["name"].strip() and f["phone"].strip() and len(f["password"]) >= 6:
            if q1("select 1 from users where phone=?", (f["phone"].strip(),)):
                flash("الرقم مسجّل من قبل")
            else:
                ex("insert into users(name,phone,password_hash,role,created) values(?,?,?,?,?)",
                   (f["name"].strip(), f["phone"].strip(), generate_password_hash(f["password"]),
                    "staff", now()))
        else:
            flash("أكمل الحقول، وكلمة المرور 6 خانات على الأقل")
    return render_template("staff.html", rows=q("select * from users where role in ('admin','staff')"))


@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def settings():
    if request.method == "POST":
        f = request.form
        if f.get("delete_cur"):
            ex("delete from currencies where code=?", (f["delete_cur"],))
        elif f.get("cur_code"):
            if num(f.get("cur_rate")) > 0:
                ex("insert into currencies(code,rate) values(?,?) on conflict(code) do update set rate=excluded.rate",
                   (f["cur_code"].strip().upper(), num(f["cur_rate"])))
        else:
            set_setting("office_name", f.get("office_name", "").strip() or "مكتب السفر")
            set_setting("office_phone", f.get("office_phone", "").strip())
            set_setting("office_address", f.get("office_address", "").strip())
            cc = "".join(ch for ch in f.get("country_code", "") if ch.isdigit())
            set_setting("country_code", cc or "218")
            flash("تم الحفظ")
    return render_template("settings.html", country_code=setting("country_code", "218"), curs=q("select * from currencies order by code"),
                           phone=setting("office_phone"), address=setting("office_address"))


@app.route("/admin/tasks", methods=["GET", "POST"])
@staff_required
def tasks():
    if request.method == "POST":
        f = request.form
        if f.get("toggle"):
            ex("update tasks set done=1-done where id=?", (f["toggle"],))
        elif f.get("delete"):
            ex("delete from tasks where id=?", (f["delete"],))
        elif f.get("title", "").strip():
            ex("insert into tasks(title,due,assigned_to,created) values(?,?,?,?)",
               (f["title"].strip(), f.get("due", ""), f.get("assigned_to") or None, now()))
        return redirect(url_for("tasks"))
    rows = q("select t.*, r.ref, u.name uname from tasks t left join requests r on r.id=t.request_id "
             "left join users u on u.id=t.assigned_to order by t.done, (t.due is null or t.due=''), t.due")
    return render_template("tasks.html", rows=rows, today=date.today().isoformat(),
                           staff=q("select * from users where role in ('admin','staff')"))


@app.route("/admin/packages", methods=["GET", "POST"])
@staff_required
def packages():
    if request.method == "POST":
        f = request.form
        if f.get("toggle"):
            ex("update packages set active=1-active where id=?", (f["toggle"],))
        elif f.get("delete"):
            ex("delete from packages where id=?", (f["delete"],))
        elif f.get("name", "").strip():
            ex("insert into packages(name,destination,duration,price,currency,description) values(?,?,?,?,?,?)",
               (f["name"].strip(), f.get("destination", "").strip(), f.get("duration", "").strip(),
                num(f.get("price")), f.get("currency"), f.get("description", "").strip()))
        return redirect(url_for("packages"))
    return render_template("packages.html", rows=q("select * from packages order by id desc"))


@app.route("/admin/reports")
@staff_required
def reports():
    y = request.args.get("y") or str(date.today().year)
    R = rates()
    rows = [(r, totals(r, R)) for r in q(
        "select * from requests where status!='ملغي' and price>0 and created like ?", (y + "%",))]
    sc = q("select status, count(*) c from requests where created like ? group by status", (y + "%",))
    months = {f"{y}-{m:02d}": dict(n=0, price=0, profit=0) for m in range(1, 13)}
    bytype, bydest, bystaff = {}, {}, {}
    names = {u["id"]: u["name"] for u in q("select id,name from users")}
    for r, t in rows:
        m = months.get((r["created"] or "")[:7])
        if m:
            m["n"] += 1; m["price"] += t["price"]; m["profit"] += t["profit"]
        for d, k in ((bytype, TYPES.get(r["type"], r["type"])),
                     (bydest, (r["destination"] or "غير محدد").strip()),
                     (bystaff, names.get(r["assigned_to"], "غير مُسند"))):
            e = d.setdefault(k, dict(n=0, price=0, profit=0))
            e["n"] += 1; e["price"] += t["price"]; e["profit"] += t["profit"]

    def top(d, n=10):
        return sorted(d.items(), key=lambda x: -x[1]["price"])[:n]
    mx = max([m["price"] for m in months.values()] + [1])
    return render_template("reports.html", y=y, months=months, mx=mx, bytype=top(bytype),
                           bydest=top(bydest), bystaff=top(bystaff),
                           cancelled=sum(c["c"] for c in sc if c["status"] == "ملغي"),
                           total_n=sum(c["c"] for c in sc), missing=missing_rates())


def safe_cell(x):
    x = "" if x is None else str(x)
    return "'" + x if x[:1] in ("=", "+", "-", "@") else x


@app.route("/admin/export/<what>.csv")
@staff_required
def export(what):
    out = io.StringIO()
    w = csv.writer(out)
    if what == "requests":
        w.writerow(["الرقم", "العميل", "الهاتف", "الخدمة", "الوجهة", "السفر", "العودة", "الحالة",
                    "السعر", "العملة", "التاريخ"])
        for r in q("select r.*, u.name cn, u.phone cp from requests r join users u on u.id=r.client_id order by r.id"):
            w.writerow([safe_cell(x) for x in (r["ref"], r["cn"], r["cp"], TYPES.get(r["type"], r["type"]),
                        r["destination"], r["depart_date"], r["return_date"], r["status"], r["price"],
                        r["currency"], r["created"])])
    elif what == "payments":
        w.writerow(["التاريخ", "الطلب", "العميل", "النوع", "المبلغ", "العملة", "الطريقة", "ملاحظة"])
        for p in q("select p.*, r.ref, u.name cn from payments p join requests r on r.id=p.request_id "
                   "join users u on u.id=r.client_id order by p.id"):
            w.writerow([safe_cell(x) for x in (p["created"], p["ref"], p["cn"],
                        "استرجاع" if p["kind"] == "refund" else "استلام", p["amount"], p["currency"],
                        p["method"], p["note"])])
    elif what == "clients":
        w.writerow(["الاسم", "الهاتف", "البريد", "تاريخ التسجيل"])
        for u in q("select * from users where role='client' order by id"):
            w.writerow([safe_cell(x) for x in (u["name"], u["phone"], u["email"], u["created"])])
    else:
        abort(404)
    return Response("\ufeff" + out.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={what}.csv"})


@app.route("/admin/backup")
@admin_required
def backup():
    tmp = os.path.join(DATA, "backup.db")
    src, dst = sqlite3.connect(DB), sqlite3.connect(tmp)
    src.backup(dst)
    dst.close(); src.close()
    return send_file(tmp, as_attachment=True, download_name=f"backup-{date.today().isoformat()}.db")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
