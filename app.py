"""Roblox Script Hub - Flask application (Wasmer-ready).

MySQL storage. Existing SQLite data requires separate migration.
"""
import os
import re
import pymysql
from pymysql.cursors import DictCursor
from io import BytesIO
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

app = Flask(__name__)
app.secret_key = os.environ['SECRET_KEY']

app.config.update(
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=True,
)


class Database:
    def __enter__(self):
        self.conn = pymysql.connect(host=os.environ['DB_HOST'], port=int(os.environ.get('DB_PORT', '3306')), user=os.environ['DB_USERNAME'], password=os.environ['DB_PASSWORD'], database=os.environ['DB_NAME'], charset='utf8mb4', cursorclass=DictCursor, connect_timeout=10)
        return self
    def execute(self, sql, args=()):
        cursor = self.conn.cursor()
        cursor.execute(sql.replace('?', '%s'), args)
        return cursor
    def executemany(self, sql, args):
        cursor = self.conn.cursor()
        cursor.executemany(sql.replace('?', '%s'), args)
        return cursor
    def __exit__(self, typ, value, traceback):
        try:
            self.conn.rollback() if typ else self.conn.commit()
        finally:
            self.conn.close()

def db():
    return Database()

def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS scripts(id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(255) NOT NULL, game VARCHAR(255) NOT NULL, tier VARCHAR(20) NOT NULL, description TEXT, code LONGTEXT NOT NULL, logo VARCHAR(255), preview_image VARCHAR(255), sort_order BIGINT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS owners(id BIGINT PRIMARY KEY AUTO_INCREMENT, name VARCHAR(255) UNIQUE NOT NULL, telegram VARCHAR(255) NOT NULL DEFAULT '', avatar VARCHAR(255))")
        c.execute("CREATE TABLE IF NOT EXISTS admins(id BIGINT PRIMARY KEY AUTO_INCREMENT, username VARCHAR(255) UNIQUE NOT NULL, password_hash VARCHAR(512) NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS images(name VARCHAR(255) PRIMARY KEY, content LONGBLOB NOT NULL)")
        for name in ('Youko', 'NhaZz', 'RU NG2', 'MENG'):
            c.execute('INSERT IGNORE INTO owners(name) VALUES (?)', (name,))
        username = os.environ.get('ADMIN_USERNAME', '').strip()
        password = os.environ.get('ADMIN_PASSWORD', '')
        if username and password and not c.execute('SELECT 1 FROM admins LIMIT 1').fetchone():
            c.execute('INSERT IGNORE INTO admins(username,password_hash) VALUES (?,?)', (username, generate_password_hash(password)))


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


@app.route('/health')
def health():
    """Basic liveness check; does not expose secrets or database content."""
    try:
        with db() as c:
            c.execute('SELECT 1').fetchone()
    except pymysql.MySQLError:
        return {'status': 'error'}, 503
    return {'status': 'ok'}


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
    """Save a resized, optimized WEBP image; preserve existing files/DB rows.

    The original upload is not stored. Each replacement gets a new random name,
    so long-lived browser caching is safe and existing images remain accessible.
    """
    upload = request.files.get(field_name)
    if not upload or not upload.filename:
        return None

    ext = Path(secure_filename(upload.filename)).suffix.lower()
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        abort(400, 'Only PNG, JPG and WEBP images are allowed')

    from PIL import Image, ImageOps, UnidentifiedImageError
    import warnings
    # A large compressed image must not exhaust the worker's memory.
    Image.MAX_IMAGE_PIXELS = 20_000_000
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(upload.stream) as source:
                detected = source.format
                if detected not in ('PNG', 'JPEG', 'WEBP'):
                    abort(400, 'Unsupported image contents')
                valid = {
                    'PNG': ('.png',),
                    'JPEG': ('.jpg', '.jpeg'),
                    'WEBP': ('.webp',),
                }
                if ext not in valid[detected]:
                    abort(400, 'File extension does not match image')
                # Apply phone rotation metadata before stripping EXIF.
                image = ImageOps.exif_transpose(source)
                image.load()

        # Smaller images improve loading times inside Telegram's browser.
        is_preview = field_name == 'preview_image'
        limit = 1280 if is_preview else 256
        image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
        if image.mode not in ('RGB', 'RGBA'):
            image = image.convert('RGBA' if 'A' in image.getbands() else 'RGB')

        name = secrets.token_hex(16) + '.webp'
        buffer = BytesIO()
        image.save(buffer, format='WEBP', quality=78 if is_preview else 76, method=4)
        with db() as c:
            c.execute('INSERT INTO images(name,content) VALUES (?,?)', (name, buffer.getvalue()))
        return name
    except (OSError, ValueError, UnidentifiedImageError,
            Image.DecompressionBombError, Image.DecompressionBombWarning):
        abort(400, 'Invalid or excessively large image')


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
    preview_image = image_upload('preview_image')
    with db() as c:
        if sid:
            old = c.execute('SELECT * FROM scripts WHERE id=?', (sid,)).fetchone()
            if not old:
                abort(404)
            c.execute('UPDATE scripts SET name=?,game=?,tier=?,description=?,code=?,logo=?,preview_image=? WHERE id=?',
                      (name, game, tier, request.form.get('description', ''), code,
                       logo or old['logo'], preview_image or old['preview_image'], sid))
        else:
            minimum = c.execute('SELECT MIN(sort_order) AS minimum FROM scripts').fetchone()['minimum']
            first_position = (minimum - 1) if minimum is not None else 0
            c.execute('INSERT INTO scripts(name,game,tier,description,code,logo,preview_image,sort_order) VALUES (?,?,?,?,?,?,?,?)',
                      (name, game, tier, request.form.get('description', ''), code,
                       logo, preview_image, first_position))
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
    with db() as c:
        row = c.execute('SELECT content FROM images WHERE name=?', (name,)).fetchone()
    if not row:
        abort(404)
    from flask import send_file
    return send_file(BytesIO(row['content']), mimetype='image/webp', max_age=31536000)


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
