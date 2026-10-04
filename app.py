import os
import re
import secrets
import shutil
from functools import wraps
from flask import Flask, jsonify, request, render_template, session, send_from_directory, url_for
from PIL import Image
import pytesseract
import psycopg2
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

tesseract_command = os.environ.get('TESSERACT_CMD') or shutil.which('tesseract')
if tesseract_command:
    pytesseract.pytesseract.tesseract_cmd = tesseract_command

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('BPL_SECRET_KEY') or secrets.token_hex(32)
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
UPLOAD_FOLDER = os.path.join(app.root_path, 'uploads')
MVP_VOTE_LOCK_KEY = 817246091
MVP_LOCK_MESSAGE = 'Głosowanie zablokowane - sezon w toku'

# JEŚLI KORZYSTASZ Z WINDOWSA, ODKOMENTUJ PONIŻSZĄ LINIJKĘ I WPISZ SWOJĄ ŚCIEŻKĘ:
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'

# ==========================================
# 1. ROZBUDOWANA BAZA DANYCH
# ==========================================
class DatabaseConnection:
    def __init__(self, connection):
        self._connection = connection

    def cursor(self):
        return self._connection.cursor()

    def execute(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query, params)
        return cursor

    def executemany(self, query, params):
        cursor = self.cursor()
        cursor.executemany(query, params)
        return cursor

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()


def connect_db():
    database_url = os.environ.get('DATABASE_URL')
    if not database_url:
        raise RuntimeError('DATABASE_URL must be set to a PostgreSQL connection string.')
    connection = psycopg2.connect(database_url, cursor_factory=RealDictCursor)
    return DatabaseConnection(connection)


def generate_unique_player_pin(conn):
    while True:
        pin_code = f'{secrets.randbelow(1_000_000):06d}'
        if not conn.execute('SELECT 1 FROM players WHERE pin_code = %s', (pin_code,)).fetchone():
            return pin_code


def init_advanced_db():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS teams (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE,
            tag TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS players (
            id SERIAL PRIMARY KEY,
            nickname TEXT UNIQUE,
            team_id INTEGER REFERENCES teams(id),
            pin_code TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fixtures (
            id SERIAL PRIMARY KEY,
            round_number INTEGER,
            team_a_id INTEGER REFERENCES teams(id),
            team_b_id INTEGER REFERENCES teams(id),
            score_maps_a INTEGER DEFAULT 0,
            score_maps_b INTEGER DEFAULT 0,
            points_a INTEGER DEFAULT 0,
            points_b INTEGER DEFAULT 0,
            status TEXT DEFAULT 'scheduled'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS single_matches (
            id SERIAL PRIMARY KEY,
            fixture_id INTEGER REFERENCES fixtures(id),
            match_number INTEGER,
            map_name TEXT,
            winner_team_id INTEGER REFERENCES teams(id),
            screenshot_url TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS player_match_stats (
            id SERIAL PRIMARY KEY,
            single_match_id INTEGER REFERENCES single_matches(id),
            player_id INTEGER REFERENCES players(id),
            kills INTEGER DEFAULT 0,
            asysty INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            damage INTEGER DEFAULT 0,
            plants INTEGER DEFAULT 0,
            defuses INTEGER DEFAULT 0,
            aces INTEGER DEFAULT 0,
            is_mvp INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT,
            team_id INTEGER REFERENCES teams(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS team_registrations (
            id SERIAL PRIMARY KEY,
            team_name TEXT NOT NULL,
            team_tag TEXT NOT NULL,
            players_list TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS votes (
            id SERIAL PRIMARY KEY,
            voter_player_id INTEGER REFERENCES players(id),
            voter_type TEXT,
            voted_player_id INTEGER REFERENCES players(id)
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS mvp_votes (
            id SERIAL PRIMARY KEY,
            voter_player_id INTEGER REFERENCES players(id),
            voter_type TEXT NOT NULL CHECK (voter_type IN ('ZAWODNIK', 'KIBIC')),
            rank_1_player_id INTEGER NOT NULL REFERENCES players(id),
            rank_2_player_id INTEGER NOT NULL REFERENCES players(id),
            rank_3_player_id INTEGER NOT NULL REFERENCES players(id),
            CHECK (rank_1_player_id <> rank_2_player_id),
            CHECK (rank_1_player_id <> rank_3_player_id),
            CHECK (rank_2_player_id <> rank_3_player_id)
        )
    ''')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS mvp_votes_one_vote_per_player
        ON mvp_votes (voter_player_id)
        WHERE voter_type = 'ZAWODNIK'
    ''')

    cursor.execute('ALTER TABLE players ADD COLUMN IF NOT EXISTS pin_code TEXT')
    cursor.execute('''
        CREATE UNIQUE INDEX IF NOT EXISTS players_pin_code_unique
        ON players (pin_code) WHERE pin_code IS NOT NULL
    ''')
    players_without_pin = conn.execute('SELECT id FROM players WHERE pin_code IS NULL FOR UPDATE').fetchall()
    for player in players_without_pin:
        cursor.execute(
            'UPDATE players SET pin_code = %s WHERE id = %s',
            (generate_unique_player_pin(conn), player['id'])
        )

    for column in ('deaths', 'plants', 'defuses'):
        cursor.execute(
            f'ALTER TABLE player_match_stats ADD COLUMN IF NOT EXISTS {column} INTEGER DEFAULT 0'
        )

    cursor.execute('''
        INSERT INTO users (username, password_hash, role, team_id)
        VALUES (%s, %s, %s, NULL)
        ON CONFLICT (username) DO NOTHING
    ''', ('admin', generate_password_hash('BPL_Admin123'), 'super_admin'))
    conn.commit()
    conn.close()


def require_roles(*roles):
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get('user_id'):
                return jsonify({'error': 'Zaloguj się, aby kontynuować.'}), 401
            if roles and session.get('role') not in roles:
                return jsonify({'error': 'Brak uprawnień do tej operacji.'}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def json_error(message, status):
    return jsonify({'error': message}), status


def points_for_map_score(team_score, opponent_score):
    if team_score > opponent_score:
        return 3
    return {2: 2, 1: 1}.get(team_score, 0)

# ==========================================
# 2. ZAKTUALIZOWANA LOGIKA BOTA OCR
# ==========================================
def process_screenshot_bot(image_path):
    """
    Bot analizuje układ kolumn z mapy Balistic R:
    Nick | E (Kills) | D (Deaths) | A (Assists) | Obrażenia (Damage) | Rośliny (Plants) | Defuses
    """
    try:
        img = Image.open(image_path)
        img_gray = img.convert('L') 
        raw_text = pytesseract.image_to_string(img_gray, lang='pol+eng')
        
        extracted_rows = []
        lines = raw_text.split('\n')
        
        for line in lines:
            parts = line.split()
            # Wiersze z danymi zawodników mają zazwyczaj min. 7-8 elementów na tym ekranie podsumowania
            if len(parts) >= 7:
                try:
                    # Mapowanie kolumn od końca (wiersz na Twoim screenie):
                    # ... [NICK] [KILLS] [DEATHS] [ASSISTS] [DAMAGE] [PLANTS] [DEFUSES] [RANGA]
                    # Indeksy ujemne zabezpieczają nas, gdy Nick gracza składa się z kilku spacji/wyrazów
                    
                    defuses = int(parts[-2]) if parts[-2].isdigit() else 0
                    plants = int(parts[-3]) if parts[-3].isdigit() else 0
                    damage = int(parts[-4]) if parts[-4].isdigit() else 0
                    assists = int(parts[-5]) if parts[-5].isdigit() else 0
                    deaths = int(parts[-6]) if parts[-6].isdigit() else 0
                    kills = int(parts[-7]) if parts[-7].isdigit() else 0
                    
                    # Wszystko co zostało z przodu (przed statystykami) to Nick gracza
                    nickname = " ".join(parts[:-7])
                    
                    if nickname and (kills > 0 or damage > 0 or deaths > 0):
                        extracted_rows.append({
                            "nickname": nickname,
                            "kills": kills,
                            "deaths": deaths,
                            "assists": assists,
                            "damage": damage,
                            "plants": plants,
                            "defuses": defuses,
                            "aces": 0,       # Czeka na uzupełnienie przez kapitana w formularzu
                            "is_mvp": 0
                        })
                except (ValueError, IndexError):
                    continue 
                    
        return extracted_rows
    except Exception as e:
        print(f"Błąd bota OCR: {e}")
        return []

# ==========================================
# 3. ZAKTUALIZOWANE ENDPOINTY API
# ==========================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/registrations', methods=['POST'])
def submit_team_registration():
    data = request.get_json(silent=True) or {}
    team_name = str(data.get('team_name', '')).strip()
    team_tag = str(data.get('team_tag', '')).strip()
    players = [name.strip() for name in re.split(r'[\n,;]+', str(data.get('players_list', ''))) if name.strip()]

    if len(team_name) < 2 or len(team_name) > 80:
        return json_error('Nazwa drużyny musi mieć od 2 do 80 znaków.', 400)
    if not re.fullmatch(r'[A-Za-z0-9_]{2,16}', team_tag):
        return json_error('Tag drużyny może zawierać 2-16 liter, cyfr lub znaków podkreślenia.', 400)
    if len(players) < 8:
        return json_error('Zgłoszenie musi zawierać co najmniej 8 nicków zawodników.', 400)
    if len(players) > 20 or any(len(name) > 40 for name in players):
        return json_error('Zgłoszenie może zawierać maksymalnie 20 nicków, każdy do 40 znaków.', 400)
    if len({name.casefold() for name in players}) != len(players):
        return json_error('Lista zawodników zawiera powtarzające się nicki.', 400)

    conn = connect_db()
    duplicate = conn.execute(
        '''SELECT 1 FROM teams WHERE lower(name) = lower(%s) OR lower(tag) = lower(%s)
           UNION ALL
           SELECT 1 FROM team_registrations WHERE status = 'pending'
             AND (lower(team_name) = lower(%s) OR lower(team_tag) = lower(%s)) LIMIT 1''',
        (team_name, team_tag, team_name, team_tag)
    ).fetchone()
    if duplicate:
        conn.close()
        return json_error('Drużyna o tej nazwie lub tagu już istnieje albo oczekuje na weryfikację.', 409)

    cursor = conn.execute(
        'INSERT INTO team_registrations (team_name, team_tag, players_list, status) VALUES (%s, %s, %s, %s) RETURNING id',
        (team_name, team_tag, '\n'.join(players), 'pending')
    )
    registration_id = cursor.fetchone()['id']
    conn.commit()
    conn.close()
    return jsonify({'message': 'Zgłoszenie zostało wysłane do weryfikacji.', 'registration_id': registration_id}), 201

@app.route('/api/login', methods=['POST'])
def login():
    data = request.get_json(silent=True) or {}
    username = str(data.get('username', '')).strip()
    password = str(data.get('password', ''))
    if not username or not password:
        return json_error('Podaj nazwę użytkownika i hasło.', 400)

    conn = connect_db()
    user = conn.execute(
        'SELECT id, username, password_hash, role, team_id FROM users WHERE username = %s',
        (username,)
    ).fetchone()
    conn.close()
    if not user or user['role'] not in ('captain', 'super_admin') or not user['password_hash'] or not check_password_hash(user['password_hash'], password):
        return json_error('Nieprawidłowa nazwa użytkownika lub hasło.', 401)

    session.clear()
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['team_id'] = user['team_id']
    return jsonify({'username': user['username'], 'role': user['role']})

@app.route('/api/session', methods=['GET'])
def get_session():
    if not session.get('user_id'):
        return jsonify({'authenticated': False})
    return jsonify({
        'authenticated': True,
        'username': session.get('username'),
        'role': session.get('role'),
        'team_id': session.get('team_id')
    })

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'message': 'Wylogowano.'})

@app.route('/api/captain/change_password', methods=['POST'])
@require_roles('captain')
def change_captain_password():
    data = request.get_json(silent=True) or {}
    current_password = str(data.get('current_password', ''))
    new_password = str(data.get('new_password', ''))
    if len(new_password) < 12:
        return json_error('Nowe hasło musi mieć co najmniej 12 znaków.', 400)

    conn = connect_db()
    user = conn.execute(
        'SELECT password_hash FROM users WHERE id = %s AND role = %s FOR UPDATE',
        (session['user_id'], 'captain')
    ).fetchone()
    if not user or not user['password_hash'] or not check_password_hash(user['password_hash'], current_password):
        conn.close()
        return json_error('Aktualne hasło jest nieprawidłowe.', 401)
    conn.execute(
        'UPDATE users SET password_hash = %s WHERE id = %s',
        (generate_password_hash(new_password), session['user_id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'message': 'Hasło zostało zmienione.'})

@app.route('/api/captain/fixtures', methods=['GET'])
@require_roles('captain')
def get_captain_fixtures():
    conn = connect_db()
    fixtures = conn.execute('''
        SELECT f.id, f.round_number, f.team_a_id, f.team_b_id,
               f.score_maps_a, f.score_maps_b, f.points_a, f.points_b,
               team_a.name AS team_a, team_b.name AS team_b
        FROM fixtures f
        JOIN teams team_a ON team_a.id = f.team_a_id
        JOIN teams team_b ON team_b.id = f.team_b_id
        WHERE f.status = 'scheduled' AND (f.team_a_id = %s OR f.team_b_id = %s)
        ORDER BY f.round_number, f.id
    ''', (session['team_id'], session['team_id'])).fetchall()
    conn.close()
    return jsonify([dict(fixture) for fixture in fixtures])

@app.route('/api/captain/upload_screenshot', methods=['POST'])
@require_roles('captain')
def upload_screenshot():
    fixture_id = request.form.get('fixture_id', type=int)
    if not fixture_id:
        return json_error('Brak identyfikatora meczu.', 400)

    conn = connect_db()
    fixture = conn.execute(
        "SELECT team_a_id, team_b_id FROM fixtures WHERE id = %s AND status = 'scheduled'",
        (fixture_id,)
    ).fetchone()
    conn.close()
    if not fixture or session['team_id'] not in (fixture['team_a_id'], fixture['team_b_id']):
        return json_error('Nie możesz przesłać wyniku tego meczu.', 403)

    uploaded_file = request.files.get('file')
    if not uploaded_file or not uploaded_file.filename:
        return json_error('Wybierz zrzut ekranu.', 400)
    safe_name = secure_filename(uploaded_file.filename)
    extension = os.path.splitext(safe_name)[1].lower()
    if extension not in ('.png', '.jpg', '.jpeg', '.webp'):
        return json_error('Dozwolone formaty: PNG, JPG i WEBP.', 400)

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    filename = f'{fixture_id}_{secrets.token_hex(12)}{extension}'
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    uploaded_file.save(filepath)
    try:
        with Image.open(filepath) as image:
            image.verify()
    except Exception:
        os.remove(filepath)
        return json_error('Nie udało się odczytać tego obrazu.', 400)

    try:
        pytesseract.get_tesseract_version()
    except pytesseract.TesseractNotFoundError:
        os.remove(filepath)
        return json_error('Brak silnika Tesseract OCR. Zainstaluj Tesseract i dodaj go do PATH albo ustaw zmienną TESSERACT_CMD.', 503)

    detected_stats = process_screenshot_bot(filepath)
    upload_token = secrets.token_urlsafe(24)
    uploads = session.get('captain_uploads', {})
    uploads[str(fixture_id)] = {'filename': filename, 'token': upload_token}
    session['captain_uploads'] = uploads
    return jsonify({
        'message': 'Zrzut ekranu przeanalizowany.',
        'upload_token': upload_token,
        'screenshot_url': url_for('admin_screenshot', filename=filename),
        'detected_stats': detected_stats
    })

@app.route('/api/captain/fixtures/<int:fixture_id>/submit', methods=['POST'])
@require_roles('captain')
def submit_fixture_result(fixture_id):
    data = request.get_json(silent=True) or {}
    uploads = session.get('captain_uploads', {})
    upload = uploads.get(str(fixture_id))
    if not upload or not secrets.compare_digest(str(data.get('upload_token', '')), upload['token']):
        return json_error('Najpierw prześlij zrzut ekranu tego meczu.', 400)

    map_name = str(data.get('map_name', '')).strip()
    if not map_name or len(map_name) > 80:
        return json_error('Podaj nazwę mapy (maksymalnie 80 znaków).', 400)

    def read_score(field, maximum):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
            raise ValueError(field)
        return value

    try:
        score_maps_a = read_score('score_maps_a', 3)
        score_maps_b = read_score('score_maps_b', 3)
    except ValueError as error:
        return json_error(f'Nieprawidłowy wynik: {error.args[0]}.', 400)
    if max(score_maps_a, score_maps_b) != 3 or score_maps_a == score_maps_b:
        return json_error('Wynik BO5 musi kończyć się zwycięstwem jednej drużyny 3-x.', 400)

    points_a = points_for_map_score(score_maps_a, score_maps_b)
    points_b = points_for_map_score(score_maps_b, score_maps_a)

    stats = data.get('stats')
    if not isinstance(stats, list) or not stats or len(stats) > 20:
        return json_error('Brak poprawnych statystyk zawodników.', 400)

    conn = connect_db()
    fixture = conn.execute(
        "SELECT * FROM fixtures WHERE id = %s AND status = 'scheduled'",
        (fixture_id,)
    ).fetchone()
    if not fixture or session['team_id'] not in (fixture['team_a_id'], fixture['team_b_id']):
        conn.close()
        return json_error('Nie możesz zgłosić wyniku tego meczu.', 403)

    player_stats = []
    seen_players = set()
    stat_fields = ('kills', 'deaths', 'assists', 'damage', 'plants', 'defuses', 'aces')
    for stat in stats:
        if not isinstance(stat, dict):
            conn.close()
            return json_error('Nieprawidłowy wiersz statystyk.', 400)
        nickname = str(stat.get('nickname', '')).strip()
        player = conn.execute(
            '''SELECT id, nickname FROM players
               WHERE lower(nickname) = lower(%s) AND team_id IN (%s, %s)''',
            (nickname, fixture['team_a_id'], fixture['team_b_id'])
        ).fetchone()
        if not player or player['id'] in seen_players:
            conn.close()
            return json_error(f'Nieznany lub powtórzony zawodnik: {nickname}.', 400)
        values = []
        for field in stat_fields:
            value = stat.get(field, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 99999:
                conn.close()
                return json_error(f'Nieprawidłowa wartość {field} dla {nickname}.', 400)
            values.append(value)
        seen_players.add(player['id'])
        player_stats.append((player['id'], values))

    winner_team_id = fixture['team_a_id'] if score_maps_a > score_maps_b else fixture['team_b_id'] if score_maps_b > score_maps_a else None
    cursor = conn.execute(
        '''INSERT INTO single_matches (fixture_id, match_number, map_name, winner_team_id, screenshot_url)
           VALUES (%s, 1, %s, %s, %s) RETURNING id''',
        (fixture_id, map_name, winner_team_id, upload['filename'])
    )
    single_match_id = cursor.fetchone()['id']
    conn.executemany(
        '''INSERT INTO player_match_stats
           (single_match_id, player_id, kills, deaths, asysty, damage, plants, defuses, aces)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)''',
        [(single_match_id, player_id, values[0], values[1], values[2], values[3], values[4], values[5], values[6])
         for player_id, values in player_stats]
    )
    conn.execute(
        '''UPDATE fixtures SET score_maps_a = %s, score_maps_b = %s, points_a = %s, points_b = %s,
           status = 'pending_approval' WHERE id = %s''',
        (score_maps_a, score_maps_b, points_a, points_b, fixture_id)
    )
    conn.commit()
    conn.close()
    uploads.pop(str(fixture_id), None)
    session['captain_uploads'] = uploads
    return jsonify({'message': 'Wynik i statystyki wysłano do zatwierdzenia.'}), 201

@app.route('/api/admin/registrations', methods=['GET'])
@require_roles('super_admin')
def get_pending_registrations():
    conn = connect_db()
    registrations = conn.execute(
        "SELECT id, team_name, team_tag, players_list FROM team_registrations WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    conn.close()
    return jsonify([
        {
            'id': row['id'],
            'team_name': row['team_name'],
            'team_tag': row['team_tag'],
            'players': [name for name in row['players_list'].splitlines() if name.strip()]
        }
        for row in registrations
    ])

@app.route('/api/admin/registrations/<int:registration_id>/approve', methods=['POST'])
@require_roles('super_admin')
def approve_team_registration(registration_id):
    conn = connect_db()
    registration = conn.execute(
        "SELECT * FROM team_registrations WHERE id = %s AND status = 'pending' FOR UPDATE",
        (registration_id,)
    ).fetchone()
    if not registration:
        conn.close()
        return json_error('Zgłoszenie nie istnieje lub zostało już rozpatrzone.', 404)

    players = [name.strip() for name in registration['players_list'].splitlines() if name.strip()]
    captain_username = f"{registration['team_tag']}_captain"
    captain_password = 'BPL_ChangeMe123'
    player_pins = []
    try:
        cursor = conn.execute(
            'INSERT INTO teams (name, tag) VALUES (%s, %s) RETURNING id',
            (registration['team_name'], registration['team_tag'])
        )
        team_id = cursor.fetchone()['id']
        for nickname in players:
            pin_code = generate_unique_player_pin(conn)
            conn.execute(
                'INSERT INTO players (nickname, team_id, pin_code) VALUES (%s, %s, %s)',
                (nickname, team_id, pin_code)
            )
            player_pins.append({'nickname': nickname, 'pin_code': pin_code})
        conn.execute(
            'INSERT INTO users (username, password_hash, role, team_id) VALUES (%s, %s, %s, %s)',
            (captain_username, generate_password_hash(captain_password), 'captain', team_id)
        )
        conn.execute("UPDATE team_registrations SET status = 'approved' WHERE id = %s", (registration_id,))
        conn.commit()
    except IntegrityError as error:
        conn.rollback()
        conn.close()
        return json_error(f'Nie można zatwierdzić drużyny: {error}. Sprawdź, czy tag lub nicki nie są już używane.', 409)
    conn.close()
    return jsonify({
        'message': 'Drużyna została zatwierdzona.',
        'captain': {'username': captain_username, 'temporary_password': captain_password},
        'player_pins': player_pins
    })

@app.route('/api/admin/registrations/<int:registration_id>/reject', methods=['POST'])
@require_roles('super_admin')
def reject_team_registration(registration_id):
    conn = connect_db()
    cursor = conn.execute(
        "UPDATE team_registrations SET status = 'rejected' WHERE id = %s AND status = 'pending'",
        (registration_id,)
    )
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        return json_error('Zgłoszenie nie istnieje lub zostało już rozpatrzone.', 404)
    return jsonify({'message': 'Zgłoszenie zostało odrzucone.'})

@app.route('/api/admin/teams', methods=['GET'])
@require_roles('super_admin')
def get_admin_teams():
    conn = connect_db()
    teams = conn.execute('''
        SELECT t.id, t.name, t.tag, COUNT(p.id) AS player_count
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        GROUP BY t.id, t.name, t.tag
        ORDER BY lower(t.name), t.id
    ''').fetchall()
    conn.close()
    return jsonify([dict(team) for team in teams])

@app.route('/api/admin/delete_team/<int:team_id>', methods=['POST'])
@require_roles('super_admin')
def delete_team(team_id):
    conn = connect_db()
    team = conn.execute('SELECT id, name FROM teams WHERE id = %s FOR UPDATE', (team_id,)).fetchone()
    if not team:
        conn.rollback()
        conn.close()
        return json_error('Nie znaleziono drużyny.', 404)

    screenshots = conn.execute('''
        SELECT sm.screenshot_url
        FROM single_matches sm
        JOIN fixtures f ON f.id = sm.fixture_id
        WHERE f.team_a_id = %s OR f.team_b_id = %s
    ''', (team_id, team_id)).fetchall()
    conn.execute('''
        DELETE FROM votes
        WHERE voter_player_id IN (SELECT id FROM players WHERE team_id = %s)
           OR voted_player_id IN (SELECT id FROM players WHERE team_id = %s)
    ''', (team_id, team_id))

    conn.execute('DELETE FROM users WHERE team_id = %s', (team_id,))
    conn.execute('''
        DELETE FROM player_match_stats
        WHERE player_id IN (SELECT id FROM players WHERE team_id = %s)
           OR single_match_id IN (
               SELECT sm.id FROM single_matches sm
               JOIN fixtures f ON f.id = sm.fixture_id
               WHERE f.team_a_id = %s OR f.team_b_id = %s
           )
    ''', (team_id, team_id, team_id))
    conn.execute('''
        DELETE FROM single_matches
        WHERE fixture_id IN (SELECT id FROM fixtures WHERE team_a_id = %s OR team_b_id = %s)
    ''', (team_id, team_id))
    conn.execute('DELETE FROM fixtures WHERE team_a_id = %s OR team_b_id = %s', (team_id, team_id))
    conn.execute('UPDATE single_matches SET winner_team_id = NULL WHERE winner_team_id = %s', (team_id,))
    conn.execute('DELETE FROM players WHERE team_id = %s', (team_id,))
    conn.execute('DELETE FROM teams WHERE id = %s', (team_id,))
    conn.commit()
    conn.close()

    for screenshot in screenshots:
        filename = secure_filename(os.path.basename(screenshot['screenshot_url'] or ''))
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if filename and os.path.isfile(filepath):
            os.remove(filepath)
    return jsonify({'message': f'Drużyna {team["name"]} i powiązane dane zostały usunięte.'})

@app.route('/api/admin/generate_schedule', methods=['POST'])
@require_roles('super_admin')
def generate_schedule():
    conn = connect_db()
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (MVP_VOTE_LOCK_KEY,))
    teams = conn.execute('SELECT id, name FROM teams ORDER BY lower(name), id FOR UPDATE').fetchall()
    if len(teams) < 2:
        conn.rollback()
        conn.close()
        return json_error('Do wygenerowania terminarza potrzeba co najmniej 2 zatwierdzonych drużyn.', 400)
    if conn.execute('SELECT 1 FROM fixtures LIMIT 1').fetchone():
        conn.rollback()
        conn.close()
        return json_error('Terminarz już istnieje. Nie można wygenerować drugiego zestawu meczów.', 409)

    rotation = [team['id'] for team in teams]
    team_names = {team['id']: team['name'] for team in teams}
    if len(rotation) % 2:
        rotation.append(None)
    matches_per_round = len(rotation) // 2
    fixture_rows = []
    bye_rows = []
    for round_index in range(len(rotation) - 1):
        round_number = round_index + 1
        for match_index in range(matches_per_round):
            team_a = rotation[match_index]
            team_b = rotation[-(match_index + 1)]
            if team_a is None:
                bye_rows.append({'round_number': round_number, 'team_id': team_b, 'team': team_names[team_b]})
                continue
            if team_b is None:
                bye_rows.append({'round_number': round_number, 'team_id': team_a, 'team': team_names[team_a]})
                continue
            if (round_index + match_index) % 2:
                team_a, team_b = team_b, team_a
            fixture_rows.append((round_number, team_a, team_b, 'scheduled'))
        rotation = [rotation[0], rotation[-1], *rotation[1:-1]]

    conn.executemany(
        'INSERT INTO fixtures (round_number, team_a_id, team_b_id, status) VALUES (%s, %s, %s, %s)',
        fixture_rows
    )
    conn.commit()
    conn.close()
    return jsonify({
        'message': 'Terminarz ligi został wygenerowany.',
        'team_count': len(teams),
        'round_count': len(rotation) - 1,
        'fixture_count': len(fixture_rows),
        'byes': bye_rows
    }), 201

@app.route('/api/fixtures', methods=['GET'])
def get_fixtures():
    conn = connect_db()
    teams = conn.execute('SELECT id, name FROM teams ORDER BY lower(name), id').fetchall()
    fixtures = conn.execute('''
        SELECT f.id, f.round_number, f.team_a_id, f.team_b_id,
               f.score_maps_a, f.score_maps_b, f.points_a, f.points_b, f.status,
               team_a.name AS team_a, team_b.name AS team_b
        FROM fixtures f
        JOIN teams team_a ON team_a.id = f.team_a_id
        JOIN teams team_b ON team_b.id = f.team_b_id
        ORDER BY f.round_number, f.id
    ''').fetchall()
    conn.close()
    team_names = {team['id']: team['name'] for team in teams}
    round_teams = {}
    fixture_rows = [dict(fixture) for fixture in fixtures]
    for fixture in fixture_rows:
        if fixture['round_number'] is not None:
            round_teams.setdefault(fixture['round_number'], set()).update((fixture['team_a_id'], fixture['team_b_id']))
    byes = []
    for round_number, participating_teams in round_teams.items():
        missing_teams = set(team_names) - participating_teams
        if len(missing_teams) == 1:
            team_id = missing_teams.pop()
            byes.append({'round_number': round_number, 'team_id': team_id, 'team': team_names[team_id]})
    return jsonify({'fixtures': fixture_rows, 'byes': byes})

@app.route('/api/fixture_details/<int:fixture_id>', methods=['GET'])
def get_fixture_details(fixture_id):
    conn = connect_db()
    fixture = conn.execute('''
        SELECT f.id, f.round_number, f.score_maps_a, f.score_maps_b, f.points_a, f.points_b,
               team_a.name AS team_a, team_b.name AS team_b
        FROM fixtures f
        JOIN teams team_a ON team_a.id = f.team_a_id
        JOIN teams team_b ON team_b.id = f.team_b_id
        WHERE f.id = %s AND f.status = 'finished'
    ''', (fixture_id,)).fetchone()
    if not fixture:
        conn.close()
        return json_error('Szczegóły są dostępne po zatwierdzeniu meczu.', 404)

    matches = conn.execute('''
        SELECT sm.id, sm.match_number, sm.map_name, sm.winner_team_id, sm.screenshot_url,
               winner.name AS winner_team
        FROM single_matches sm
        LEFT JOIN teams winner ON winner.id = sm.winner_team_id
        WHERE sm.fixture_id = %s
        ORDER BY sm.match_number, sm.id
    ''', (fixture_id,)).fetchall()
    match_details = []
    for match in matches:
        stats = conn.execute('''
            SELECT p.nickname, t.name AS team, pms.kills, pms.deaths,
                   pms.asysty AS assists, pms.damage, pms.plants, pms.defuses,
                   pms.aces, pms.is_mvp
            FROM player_match_stats pms
            JOIN players p ON p.id = pms.player_id
            LEFT JOIN teams t ON t.id = p.team_id
            WHERE pms.single_match_id = %s
            ORDER BY p.nickname
        ''', (match['id'],)).fetchall()
        screenshot_name = os.path.basename(match['screenshot_url'] or '')
        match_details.append({
            'id': match['id'],
            'match_number': match['match_number'],
            'map_name': match['map_name'],
            'winner_team_id': match['winner_team_id'],
            'winner_team': match['winner_team'],
            'screenshot_url': url_for('fixture_screenshot', filename=screenshot_name) if screenshot_name else None,
            'stats': [dict(stat) for stat in stats]
        })
    conn.close()
    return jsonify({'fixture': dict(fixture), 'matches': match_details})

@app.route('/api/fixture_screenshots/<path:filename>', methods=['GET'])
def fixture_screenshot(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return json_error('Nieprawidłowa nazwa pliku.', 400)
    conn = connect_db()
    is_public = conn.execute('''
        SELECT 1 FROM single_matches sm
        JOIN fixtures f ON f.id = sm.fixture_id
        WHERE sm.screenshot_url = %s AND f.status = 'finished'
        LIMIT 1
    ''', (safe_filename,)).fetchone()
    conn.close()
    if not is_public:
        return json_error('Zrzut ekranu nie jest dostępny publicznie.', 404)
    return send_from_directory(UPLOAD_FOLDER, safe_filename)

@app.route('/api/admin/fixtures', methods=['GET'])
@require_roles('super_admin')
def get_pending_fixtures():
    conn = connect_db()
    fixtures = conn.execute('''
        SELECT f.*, team_a.name AS team_a, team_b.name AS team_b
        FROM fixtures f
        JOIN teams team_a ON team_a.id = f.team_a_id
        JOIN teams team_b ON team_b.id = f.team_b_id
        WHERE f.status = 'pending_approval'
        ORDER BY f.round_number, f.id
    ''').fetchall()
    result = []
    for fixture in fixtures:
        matches = conn.execute('''
            SELECT sm.id, sm.match_number, sm.map_name, sm.screenshot_url,
                   p.nickname, pms.kills, pms.deaths, pms.asysty AS assists,
                   pms.damage, pms.plants, pms.defuses, pms.aces
            FROM single_matches sm
            LEFT JOIN player_match_stats pms ON pms.single_match_id = sm.id
            LEFT JOIN players p ON p.id = pms.player_id
            WHERE sm.fixture_id = %s ORDER BY sm.match_number, p.nickname
        ''', (fixture['id'],)).fetchall()
        match_map = {}
        for match in matches:
            item = match_map.setdefault(match['id'], {
                'match_number': match['match_number'],
                'map_name': match['map_name'],
                'screenshot_url': url_for('admin_screenshot', filename=match['screenshot_url']),
                'stats': []
            })
            if match['nickname']:
                item['stats'].append({
                    'nickname': match['nickname'], 'kills': match['kills'],
                    'deaths': match['deaths'], 'assists': match['assists'],
                    'damage': match['damage'], 'plants': match['plants'],
                    'defuses': match['defuses'], 'aces': match['aces']
                })
        item = dict(fixture)
        item['matches'] = list(match_map.values())
        result.append(item)
    conn.close()
    return jsonify(result)

@app.route('/api/admin/fixtures/<int:fixture_id>/approve', methods=['POST'])
@require_roles('super_admin')
def approve_fixture(fixture_id):
    conn = connect_db()
    cursor = conn.execute(
        "UPDATE fixtures SET status = 'finished' WHERE id = %s AND status = 'pending_approval'",
        (fixture_id,)
    )
    conn.commit()
    conn.close()
    if not cursor.rowcount:
        return json_error('Mecz nie oczekuje na zatwierdzenie.', 404)
    return jsonify({'message': 'Mecz zatwierdzono. Wyniki są już widoczne w rankingach.'})

@app.route('/api/admin/fixtures/<int:fixture_id>/reject', methods=['POST'])
@require_roles('super_admin')
def reject_fixture(fixture_id):
    conn = connect_db()
    fixture = conn.execute(
        "SELECT id FROM fixtures WHERE id = %s AND status = 'pending_approval' FOR UPDATE",
        (fixture_id,)
    ).fetchone()
    if not fixture:
        conn.close()
        return json_error('Mecz nie oczekuje na zatwierdzenie.', 404)
    screenshots = conn.execute(
        'SELECT screenshot_url FROM single_matches WHERE fixture_id = %s',
        (fixture_id,)
    ).fetchall()
    conn.execute(
        'DELETE FROM player_match_stats WHERE single_match_id IN (SELECT id FROM single_matches WHERE fixture_id = %s)',
        (fixture_id,)
    )
    conn.execute('DELETE FROM single_matches WHERE fixture_id = %s', (fixture_id,))
    conn.execute(
        "UPDATE fixtures SET status = 'scheduled', score_maps_a = 0, score_maps_b = 0, points_a = 0, points_b = 0 WHERE id = %s",
        (fixture_id,)
    )
    conn.commit()
    conn.close()
    for screenshot in screenshots:
        filename = secure_filename(os.path.basename(screenshot['screenshot_url'] or ''))
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        if filename and os.path.isfile(filepath):
            os.remove(filepath)
    return jsonify({'message': 'Wynik odrzucono. Kapitan może przesłać go ponownie.'})

@app.route('/api/admin/screenshots/<path:filename>', methods=['GET'])
@require_roles('super_admin')
def admin_screenshot(filename):
    safe_filename = secure_filename(filename)
    if safe_filename != filename:
        return json_error('Nieprawidłowa nazwa pliku.', 400)
    return send_from_directory(UPLOAD_FOLDER, safe_filename)

@app.route('/api/team_standings', methods=['GET'])
def get_team_standings():
    conn = connect_db()
    rows = conn.execute('''
        WITH team_matches AS (
            SELECT team_a_id AS team_id,
                   COALESCE(points_a, 0) AS league_points,
                   CASE WHEN COALESCE(score_maps_a, 0) > COALESCE(score_maps_b, 0) THEN 1 ELSE 0 END AS wins,
                   COALESCE(score_maps_a, 0) AS map_wins,
                   COALESCE(score_maps_b, 0) AS map_losses
            FROM fixtures
            WHERE status = 'finished'
            UNION ALL
            SELECT team_b_id AS team_id,
                   COALESCE(points_b, 0) AS league_points,
                   CASE WHEN COALESCE(score_maps_b, 0) > COALESCE(score_maps_a, 0) THEN 1 ELSE 0 END AS wins,
                   COALESCE(score_maps_b, 0) AS map_wins,
                   COALESCE(score_maps_a, 0) AS map_losses
            FROM fixtures
            WHERE status = 'finished'
             ),
             team_totals AS (
                 SELECT t.id AS team_id,
                     t.name AS team,
                     COUNT(tm.team_id)::INTEGER AS matches_played,
                     COALESCE(SUM(tm.wins), 0)::INTEGER AS wins,
                     COALESCE(SUM(tm.league_points), 0)::INTEGER AS league_points,
                     COALESCE(SUM(tm.map_wins), 0)::INTEGER AS map_wins,
                     COALESCE(SUM(tm.map_losses), 0)::INTEGER AS map_losses,
                     COALESCE(SUM(tm.map_wins - tm.map_losses), 0)::INTEGER AS map_balance
                 FROM teams t
                 LEFT JOIN team_matches tm ON tm.team_id = t.id
                 GROUP BY t.id, t.name
        )
             SELECT current_team.*,
                 COALESCE((
                     SELECT SUM(CASE WHEN f.team_a_id = current_team.team_id
                            THEN COALESCE(f.points_a, 0)
                            ELSE COALESCE(f.points_b, 0) END)
                     FROM fixtures f
                     JOIN team_totals opponent
                    ON opponent.team_id = CASE
                          WHEN f.team_a_id = current_team.team_id THEN f.team_b_id
                          ELSE f.team_a_id
                       END
                     WHERE f.status = 'finished'
                    AND (f.team_a_id = current_team.team_id OR f.team_b_id = current_team.team_id)
                    AND opponent.league_points = current_team.league_points
                    AND opponent.map_balance = current_team.map_balance
                 ), 0)::INTEGER AS head_to_head_points
             FROM team_totals current_team
             ORDER BY current_team.league_points DESC,
                   current_team.map_balance DESC,
                   head_to_head_points DESC,
                   lower(current_team.team) ASC
    ''').fetchall()
    conn.close()

    return jsonify([
        {
            "team_id": row['team_id'],
            "team": row['team'],
            "matches_played": row['matches_played'],
            "wins": row['wins'],
            "league_points": row['league_points'],
            "map_wins": row['map_wins'],
            "map_losses": row['map_losses'],
            "map_balance": row['map_balance'],
            "head_to_head_points": row['head_to_head_points']
        }
        for row in rows
    ])

@app.route('/api/leaderboards', methods=['GET'])
def get_leaderboards():
    conn = connect_db()
    
    # Pobieramy pełne, zsumowane statystyki wyłącznie z zatwierdzonych meczów (status = 'finished')
    query = '''
        SELECT p.nickname AS nickname, t.name AS team,
               COALESCE(SUM(s.kills), 0) as total_kills,
               COALESCE(SUM(s.asysty), 0) as total_assists,
               COALESCE(SUM(s.deaths), 0) as total_deaths,
               COALESCE(SUM(s.damage), 0) as total_damage,
               COALESCE(SUM(s.plants), 0) as total_plants,
               COALESCE(SUM(s.defuses), 0) as total_defuses,
               COALESCE(SUM(s.aces), 0) as total_aces,
               COALESCE(SUM(s.is_mvp), 0) as total_mvp,
               COUNT(s.id) as matches_played
        FROM player_match_stats s
        JOIN players p ON s.player_id = p.id
        JOIN teams t ON p.team_id = t.id
        JOIN single_matches sm ON s.single_match_id = sm.id
        JOIN fixtures f ON sm.fixture_id = f.id
        WHERE f.status = 'finished'
        GROUP BY p.id, p.nickname, t.name
    '''
    rows = conn.execute(query).fetchall()
    conn.close()
    
    players_stats = []
    for row in rows:
        nick = row['nickname']
        team = row['team']
        kills = row['total_kills']
        assists = row['total_assists']
        deaths = row['total_deaths']
        dmg = row['total_damage']
        plants = row['total_plants']
        defuses = row['total_defuses']
        aces = row['total_aces']
        mvp = row['total_mvp']
        m_played = row['matches_played']
        
        # Ochrona przed dzieleniem przez zero
        avg_deaths = float(deaths) / float(m_played) if m_played > 0 else 0.0
        
        # NOWY WZÓR NA CESARZA: Kills(1) + Assists(0.5) + Aces(2) + Plants(0.5) + Defuses(0.5)
        cesarz_points = float(kills) + (float(assists) * 0.5) + (float(aces) * 2.0) + (float(plants) * 0.5) + (float(defuses) * 0.5)
        
        players_stats.append({
            "player": nick,
            "team": team,
            "kills": kills,
            "deaths": deaths,
            "assists": assists,
            "damage": dmg,
            "plants": plants,
            "defuses": defuses,
            "aces": aces,
            "mvp": mvp,
            "matches_played": m_played,
            "avg_deaths": round(avg_deaths, 2),
            "cesarz": cesarz_points
        })
        
    # Filtrujemy tylko graczy, którzy rozegrali chociaż 1 mecz do statystyki Nieśmiertelnego
    active_players = [p for p in players_stats if p["matches_played"] > 0]
    
    # Sortowanie kategorii (Dla Nieśmiertelnego reverse=False, bo szukamy NAJMNIEJSZEJ średniej)
    krol_killi = sorted(players_stats, key=lambda x: x["kills"], reverse=True)[:10]
    krol_asyst = sorted(players_stats, key=lambda x: x["assists"], reverse=True)[:10]
    krol_damage = sorted(players_stats, key=lambda x: x["damage"], reverse=True)[:10]
    cesarz = sorted(players_stats, key=lambda x: x["cesarz"], reverse=True)[:10]
    niesmiertelny = sorted(active_players, key=lambda x: x["avg_deaths"], reverse=False)[:10]
    
    return jsonify({
        "krol_killi": krol_killi,
        "krol_asyst": krol_asyst,
        "krol_damage": krol_damage,
        "cesarz": cesarz,
        "niesmiertelny": niesmiertelny
    })

@app.route('/api/teams_rosters', methods=['GET'])
def get_teams_rosters():
    conn = connect_db()
    rows = conn.execute('''
        WITH player_totals AS (
            SELECT pms.player_id,
                   SUM(COALESCE(pms.kills, 0))::INTEGER AS total_kills,
                   SUM(COALESCE(pms.deaths, 0))::INTEGER AS total_deaths,
                   SUM(COALESCE(pms.damage, 0))::INTEGER AS total_damage,
                   SUM(COALESCE(pms.plants, 0))::INTEGER AS total_plants,
                   SUM(COALESCE(pms.defuses, 0))::INTEGER AS total_defuses,
                   SUM(COALESCE(pms.aces, 0))::INTEGER AS total_aces,
                   COUNT(pms.id)::INTEGER AS matches_played
            FROM player_match_stats pms
            JOIN single_matches sm ON sm.id = pms.single_match_id
            JOIN fixtures f ON f.id = sm.fixture_id
            WHERE f.status = 'finished'
            GROUP BY pms.player_id
        )
        SELECT t.id AS team_id, t.name AS team_name,
               p.id AS player_id, p.nickname,
               COALESCE(pt.total_kills, 0) AS total_kills,
               COALESCE(pt.total_deaths, 0) AS total_deaths,
               CASE WHEN COALESCE(pt.matches_played, 0) = 0 THEN 0::DOUBLE PRECISION
                    ELSE ROUND(pt.total_deaths::NUMERIC / pt.matches_played, 2)::DOUBLE PRECISION
               END AS avg_deaths,
               COALESCE(pt.total_damage, 0) AS total_damage,
               COALESCE(pt.total_plants, 0) AS total_plants,
               COALESCE(pt.total_defuses, 0) AS total_defuses,
               COALESCE(pt.total_aces, 0) AS total_aces
        FROM teams t
        LEFT JOIN players p ON p.team_id = t.id
        LEFT JOIN player_totals pt ON pt.player_id = p.id
        ORDER BY lower(t.name), lower(p.nickname)
    ''').fetchall()
    conn.close()

    teams_by_id = {}
    for row in rows:
        team = teams_by_id.setdefault(row['team_id'], {
            'team_id': row['team_id'],
            'team_name': row['team_name'],
            'players': []
        })
        if row['player_id'] is not None:
            team['players'].append({
                'player_id': row['player_id'],
                'nickname': row['nickname'],
                'total_kills': row['total_kills'],
                'total_deaths': row['total_deaths'],
                'avg_deaths': row['avg_deaths'],
                'total_damage': row['total_damage'],
                'total_plants': row['total_plants'],
                'total_defuses': row['total_defuses'],
                'total_aces': row['total_aces']
            })
    return jsonify(list(teams_by_id.values()))

@app.route('/api/mvp_results', methods=['GET'])
def get_mvp_results():
    conn = connect_db()
    rows = conn.execute('''
        SELECT p.id AS player_id, p.nickname, t.name AS team,
               (COUNT(v.id) FILTER (WHERE v.rank_1_player_id = p.id) * 3
                + COUNT(v.id) FILTER (WHERE v.rank_2_player_id = p.id) * 2
                + COUNT(v.id) FILTER (WHERE v.rank_3_player_id = p.id))::INTEGER AS points,
               COUNT(v.id) FILTER (WHERE v.rank_1_player_id = p.id)::INTEGER AS rank_1_votes,
               COUNT(v.id) FILTER (WHERE v.rank_2_player_id = p.id)::INTEGER AS rank_2_votes,
               COUNT(v.id) FILTER (WHERE v.rank_3_player_id = p.id)::INTEGER AS rank_3_votes
        FROM players p
        JOIN teams t ON t.id = p.team_id
        LEFT JOIN mvp_votes v ON p.id IN (v.rank_1_player_id, v.rank_2_player_id, v.rank_3_player_id)
        GROUP BY p.id, p.nickname, t.name
        ORDER BY points DESC, rank_1_votes DESC, rank_2_votes DESC, lower(p.nickname)
    ''').fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])

@app.route('/api/mvp_status', methods=['GET'])
def get_mvp_status():
    conn = connect_db()
    status = conn.execute('''
        SELECT NOT EXISTS (SELECT 1 FROM fixtures)
            OR EXISTS (SELECT 1 FROM fixtures WHERE status IS DISTINCT FROM 'finished') AS locked
    ''').fetchone()
    conn.close()
    locked = status['locked']
    return jsonify({
        'locked': locked,
        'message': MVP_LOCK_MESSAGE if locked else 'Głosowanie MVP jest otwarte.'
    })

@app.route('/api/mvp_votes', methods=['POST'])
def submit_mvp_vote():
    data = request.get_json(silent=True) or {}
    conn = connect_db()
    conn.execute('SELECT pg_advisory_xact_lock(%s)', (MVP_VOTE_LOCK_KEY,))
    status = conn.execute('''
        SELECT NOT EXISTS (SELECT 1 FROM fixtures)
            OR EXISTS (SELECT 1 FROM fixtures WHERE status IS DISTINCT FROM 'finished') AS locked
    ''').fetchone()
    if status['locked']:
        conn.close()
        return json_error(MVP_LOCK_MESSAGE, 423)

    voter_type = str(data.get('voter_type', '')).strip().upper()
    if voter_type not in ('ZAWODNIK', 'KIBIC'):
        conn.close()
        return json_error('Wybierz typ głosującego.', 400)

    rank_ids = [data.get('rank_1_player_id'), data.get('rank_2_player_id'), data.get('rank_3_player_id')]
    if any(isinstance(player_id, bool) or not isinstance(player_id, int) or player_id < 1 for player_id in rank_ids):
        conn.close()
        return json_error('Wybierz trzech zawodników w rankingu 1, 2 i 3.', 400)
    if len(set(rank_ids)) != 3:
        conn.close()
        return json_error('Każde miejsce musi wskazywać innego zawodnika.', 400)

    voter_player_id = None
    voter_team_id = None
    if voter_type == 'ZAWODNIK':
        pin_code = str(data.get('pin_code', '')).strip()
        if not re.fullmatch(r'\d{6}', pin_code):
            conn.close()
            return json_error('Podaj swój 6-cyfrowy PIN zawodnika.', 400)
        voter = conn.execute(
            'SELECT id, team_id FROM players WHERE pin_code = %s FOR UPDATE',
            (pin_code,)
        ).fetchone()
        if not voter:
            conn.close()
            return json_error('Nieprawidłowy PIN zawodnika.', 401)
        voter_player_id = voter['id']
        voter_team_id = voter['team_id']

    candidates = conn.execute(
        'SELECT id, team_id FROM players WHERE id = ANY(%s)',
        (rank_ids,)
    ).fetchall()
    if len(candidates) != 3:
        conn.close()
        return json_error('Jeden z wybranych zawodników nie istnieje.', 400)
    if voter_team_id is not None and any(candidate['team_id'] == voter_team_id for candidate in candidates):
        conn.close()
        return json_error('Zawodnik nie może głosować na graczy ze swojej drużyny.', 403)

    try:
        conn.execute('''
            INSERT INTO mvp_votes
                (voter_player_id, voter_type, rank_1_player_id, rank_2_player_id, rank_3_player_id)
            VALUES (%s, %s, %s, %s, %s)
        ''', (voter_player_id, voter_type, *rank_ids))
        conn.commit()
    except IntegrityError:
        conn.rollback()
        conn.close()
        return json_error('Ten zawodnik oddał już swój głos.', 409)
    conn.close()
    return jsonify({'message': 'Głos MVP został zapisany.'}), 201

if __name__ == '__main__':
    init_advanced_db()
    app.run(debug=True, port=5000)
