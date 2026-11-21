"""YOUKO HUB - Flask script library.

Keeps the existing SQLite database and Flask endpoints while adding a
persistent admin session, script ordering, and owner avatar uploads.
"""
import os
import re
import sqlite3
import secrets
import argparse
from datetime import timedelta
from pathlib import Path
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    flash, send_from_directory, abort,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('DATA_DIR', str(BASE / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
UPLOADS = DATA / 'uploads'
UPLOADS.mkdir(parents=True, exist_ok=True)
DB = DATA / 'site.sqlite3'

app = Flask(__name__)

# Use a stable secret so browser sessions survive app reloads.
# Store it alongside the database (NOT in the GitHub repository).
secret_file = DATA / '.flask_secret'
if not os.environ.get('SECRET_KEY'):
    try:
        with secret_file.open('x') as f:
            f.write(secrets.token_hex(32))
        secret_file.chmod(0o600)
    except FileExistsError:
        pass
app.secret_key = os.environ.get('SECRET_KEY') or secret_file.read_text().strip()

app.config.update(
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
)


def db():
    connection = sqlite3.connect(DB, timeout=15)
    connection.row_factory = sqlite3.Row
    return connection


def init():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS scripts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, game TEXT NOT NULL, tier TEXT NOT NULL, description TEXT, code TEXT NOT NULL, logo TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        c.execute('CREATE TABLE IF NOT EXISTS owners(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, telegram TEXT NOT NULL DEFAULT "")')
        # Safe migration: add owner avatars without deleting existing owners or links.
        owner_columns = {col['name'] for col in c.execute('PRAGMA table_info(owners)')}
        if 'avatar' not in owner_columns:
            c.execute('ALTER TABLE owners ADD COLUMN avatar TEXT')
        c.execute('CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL)')

        # Add a sort column without dropping or recreating existing scripts.
        existing_columns = {col['name'] for col in c.execute('PRAGMA table_info(scripts)')}
        if 'sort_order' not in existing_columns:
            c.execute('ALTER TABLE scripts ADD COLUMN sort_order INTEGER')

        # Fill in positions only where missing, preserving stored manual order.
        current_max = c.execute('SELECT MAX(sort_order) FROM scripts').fetchone()[0]
        next_position = (current_max + 1) if current_max is not None else 0
        missing = c.execute('SELECT id FROM scripts WHERE sort_order IS NULL ORDER BY id DESC').fetchall()
        for row in missing:
            c.execute('UPDATE scripts SET sort_order=? WHERE id=?', (next_position, row['id']))
            next_position += 1

        for name in ('Youko', 'NhaZz', 'RU NG2', 'MENG'):
            c.execute('INSERT OR IGNORE INTO owners(name) VALUES (?)', (name,))
        username = os.environ.get('ADMIN_USERNAME', '').strip()
        password = os.environ.get('ADMIN_PASSWORD', '')
        if username and password and not c.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
            c.execute('INSERT INTO admins(username, password_hash) VALUES (?, ?)',
                      (username, generate_password_hash(password)))


def set_admin(username, password):
    username = username.strip()
    if not username or not password:
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


SCRIPT_ORDER = 'SELECT * FROM scripts ORDER BY sort_order ASC, id DESC'


@app.route('/')
def home():
    with db() as c:
        scripts = c.execute(SCRIPT_ORDER).fetchall()
        owners = c.execute('SELECT * FROM owners').fetchall()
    return render_template('home.html', scripts=scripts, owners=owners)


@app.route('/script/<int:sid>')
def script_detail(sid):
    with db() as c:
        script = c.execute('SELECT * FROM scripts WHERE id=?', (sid,)).fetchone()
    if script is None:
        abort(404)
    return render_template('script_detail.html', script=script)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        with db() as c:
            admin = c.execute('SELECT * FROM admins WHERE username=?',
                              (request.form.get('username', ''),)).fetchone()
        if admin and check_password_hash(admin['password_hash'], request.form.get('password', '')):
            session.clear()
            session['admin'] = admin['id']
            session.permanent = True
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
        scripts = c.execute(SCRIPT_ORDER).fetchall()
        owners = c.execute('SELECT * FROM owners').fetchall()
    return render_template('admin.html', scripts=scripts, owners=owners)


def image_upload(field_name):
    """Validate an optional uploaded PNG/JPEG/WEBP and save with a random name."""
    file = request.files.get(field_name)
    if not file or not file.filename:
        return None
    ext = Path(secure_filename(file.filename)).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        abort(400, 'Only PNG, JPG or WEBP images are allowed')

    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(file.stream) as image:
            image.verify()
            detected_format = image.format
        if detected_format not in ('PNG', 'JPEG', 'WEBP'):
            abort(400, 'Invalid image format')
        valid_extensions = {
            'PNG': ('.png',),
            'JPEG': ('.jpg', '.jpeg'),
            'WEBP': ('.webp',),
        }
        if ext not in valid_extensions[detected_format]:
            abort(400, 'Image extension does not match its contents')
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        abort(400, 'Invalid image')

    file.stream.seek(0)
    name = secrets.token_hex(16) + ext
    file.save(UPLOADS / name)
    return name


def logo_upload():
    """Keep compatibility with the existing script upload form."""
    return image_upload('logo')


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
            minimum = c.execute('SELECT MIN(sort_order) FROM scripts').fetchone()[0]
            first_position = (minimum - 1) if minimum is not None else 0
            c.execute('INSERT INTO scripts(name,game,tier,description,code,logo,sort_order) VALUES (?,?,?,?,?,?,?)',
                      (name, game, tier, request.form.get('description', ''), code, logo, first_position))
    return redirect(url_for('dashboard'))


@app.route('/admin/move/<int:sid>/<direction>', methods=['POST'])
@admin_required
def move_script(sid, direction):
    """Move a script one position up or down; persist the new order."""
    if direction not in ('up', 'down'):
        abort(400, 'Invalid direction')
    with db() as c:
        ordered_ids = [row['id'] for row in c.execute(
            'SELECT id FROM scripts ORDER BY sort_order ASC, id DESC'
        ).fetchall()]
        if sid not in ordered_ids:
            abort(404)
        current_index = ordered_ids.index(sid)
        new_index = current_index + (-1 if direction == 'up' else 1)
        if 0 <= new_index < len(ordered_ids):
            ordered_ids[current_index], ordered_ids[new_index] = (
                ordered_ids[new_index], ordered_ids[current_index]
            )
            c.executemany('UPDATE scripts SET sort_order=? WHERE id=?',
                          [(position, script_id) for position, script_id in enumerate(ordered_ids)])
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
    # Validate all usernames before changing the database.
    with db() as c:
        rows = c.execute('SELECT id FROM owners').fetchall()
    updates = []
    for row in rows:
        owner_id = row['id']
        telegram = request.form.get(f'owner_{owner_id}', '').strip().lstrip('@')
        if telegram and not re.fullmatch(r'[A-Za-z0-9_]{5,32}', telegram):
            abort(400, 'Invalid Telegram username')
        # Form must have enctype="multipart/form-data" and this input name.
        avatar = image_upload(f'owner_image_{owner_id}')
        updates.append((telegram, avatar, owner_id))

    with db() as c:
        for telegram, avatar, owner_id in updates:
            if avatar is None:
                c.execute('UPDATE owners SET telegram=? WHERE id=?',
                          (telegram, owner_id))
            else:
                c.execute('UPDATE owners SET telegram=?, avatar=? WHERE id=?',
                          (telegram, avatar, owner_id))
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
