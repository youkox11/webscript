import os
import re
import sqlite3
import secrets
import argparse
from pathlib import Path
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, session, flash, send_from_directory, abort
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 3 * 1024 * 1024
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
DATA = Path(os.environ.get('DATA_DIR', str(BASE / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / 'uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = DATA / 'site.sqlite3'


def db():
    connection = sqlite3.connect(DB, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, game TEXT NOT NULL, tier TEXT NOT NULL, description TEXT, code TEXT NOT NULL, logo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, telegram TEXT NOT NULL DEFAULT "")')
        c.execute('CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)')
        for name in ('Youko', 'NhaZz', 'RU NG2', 'MENG'):
            c.execute('INSERT OR IGNORE INTO owners(name) VALUES (?)', (name,))
        username = os.environ.get('ADMIN_USERNAME', '').strip()
        password = os.environ.get('ADMIN_PASSWORD', '')
        if username and password and not c.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
            c.execute('INSERT INTO admins(username, password_hash) VALUES (?, ?)',
                      (username, generate_password_hash(password)))


def set_admin(username, password):
    if not username.strip() or not password:
        raise ValueError('Username and password must not be empty')
    with db() as c:
        existing = c.execute('SELECT id FROM admins WHERE username=?', (username,)).fetchone()
        if existing:
            c.execute('UPDATE admins SET password_hash=? WHERE id=?',
                      (generate_password_hash(password), existing['id']))
        else:
            c.execute('INSERT INTO admins(username,password_hash) VALUES (?,?)',
                      (username, generate_password_hash(password)))


init()


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


@app.route('/script/<int:sid>')
def script_detail(sid):
    with db() as c:
        script = c.execute(
            'SELECT * FROM scripts WHERE id=?',
            (sid,)
        ).fetchone()

    if script is None:
        abort(404)

    return render_template(
        'script_detail.html',
        script=script
    )


@app.route('/')
def home():
    with db() as c:
        scripts = c.execute('SELECT * FROM scripts ORDER BY id DESC').fetchall()
        owners = c.execute('SELECT * FROM owners').fetchall()
    return render_template('home.html', scripts=scripts, owners=owners)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        with db() as c:
            admin = c.execute('SELECT * FROM admins WHERE username=?',
                              (request.form.get('username', ''),)).fetchone()
        if admin and check_password_hash(admin['password_hash'], request.form.get('password', '')):
            session.clear()
            session['admin'] = admin['id']
            return redirect(url_for('dashboard'))
        flash('Invalid credentials')
    return render_template('login.html')


@app.route('/logout', methods=['POST'])
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/admin')
@admin_required
def dashboard():
    with db() as c:
        scripts = c.execute('SELECT * FROM scripts ORDER BY id DESC').fetchall()
        owners = c.execute('SELECT * FROM owners').fetchall()
    return render_template('admin.html', scripts=scripts, owners=owners)


def logo_upload():
    file = request.files.get('logo')
    if not file or not file.filename:
        return None
    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        abort(400, 'Only PNG, JPG, WEBP')
    from PIL import Image, UnidentifiedImageError
    try:
        image = Image.open(file.stream)
        image.verify()
        if image.format not in ('PNG', 'JPEG', 'WEBP'):
            abort(400, 'Invalid image format')
    except (OSError, ValueError, UnidentifiedImageError):
        abort(400, 'Invalid image')
    file.stream.seek(0)
    name = secrets.token_hex(16) + ext
    file.save(UPLOADS / name)
    return name


@app.route('/admin/script', methods=['POST'])
@admin_required
def save_script():
    sid = request.form.get('id')
    name = request.form.get('name', '').strip()
    game = request.form.get('game', '').strip()
    tier = request.form.get('tier', 'Free')
    code = request.form.get('code', '')
    if not name or not game or not code or tier not in ('Free', 'Premium'):
        abort(400, 'Invalid script data')
    logo = logo_upload()
    with db() as c:
        if sid:
            old = c.execute('SELECT * FROM scripts WHERE id=?', (sid,)).fetchone()
            if not old:
                abort(404)
            c.execute('UPDATE scripts SET name=?,game=?,tier=?,description=?,code=?,logo=? WHERE id=?',
                      (name, game, tier, request.form.get('description', ''), code, logo or old['logo'], sid))
        else:
            c.execute('INSERT INTO scripts(name,game,tier,description,code,logo) VALUES (?,?,?,?,?,?)',
                      (name, game, tier, request.form.get('description', ''), code, logo))
    return redirect(url_for('dashboard'))


@app.route('/admin/delete/<int:sid>', methods=['POST'])
@admin_required
def delete_script(sid):
    with db() as c:
        c.execute('DELETE FROM scripts WHERE id=?', (sid,))
    return redirect(url_for('dashboard'))


@app.route('/admin/owners', methods=['POST'])
@admin_required
def owners_update():
    with db() as c:
        updates = []
        for row in c.execute('SELECT id FROM owners').fetchall():
            value = request.form.get('owner_' + str(row['id']), '').strip().lstrip('@')
            if value and not re.fullmatch(r'[A-Za-z0-9_]{5,32}', value):
                abort(400, 'Invalid Telegram username')
            updates.append((value, row['id']))
        c.executemany('UPDATE owners SET telegram=? WHERE id=?', updates)
    return redirect(url_for('dashboard'))


@app.route('/uploads/<path:name>')
def uploaded(name):
    return send_from_directory(UPLOADS, name)


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--set-admin', action='store_true', help='Create or reset an admin account')
    args = parser.parse_args()
    if args.set_admin:
        import getpass
        username = input('Admin username: ').strip()
        password = getpass.getpass('Admin password: ')
        set_admin(username, password)
        print('Admin account saved successfully.')
    else:
        app.run(debug=False)
