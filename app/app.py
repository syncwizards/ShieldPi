import os
import socket
import sqlite3
import subprocess
import json
import threading
import time
import urllib.request
import urllib.parse
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime

app = Flask(__name__)

# CONFIGURACION
app.secret_key = os.urandom(24)
DB_PATH = '/app/config/shieldpi.db'
KOPIA_CONFIG = '/app/config/repository.config'

# --- HELPERS ---
def run_command(cmd, env=None):
    final_env = os.environ.copy()
    if env: final_env.update(env)
    try:
        result = subprocess.run(cmd, env=final_env, capture_output=True, text=True)
        return result.returncode == 0, result.stdout, result.stderr
    except Exception as e:
        return False, "", str(e)

def run_kopia(args, env=None):
    cmd = ['kopia', '--config-file', KOPIA_CONFIG] + args
    return run_command(cmd, env)

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS docker_links (path TEXT PRIMARY KEY, container_name TEXT)''')
    conn.commit(); conn.close()

def get_setting(key, default=None):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT value FROM settings WHERE key = ?', (key,))
    row = c.fetchone(); conn.close()
    return row[0] if row else default

def set_setting(key, value):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
    conn.commit(); conn.close()

def get_docker_link(path):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT container_name FROM docker_links WHERE path = ?', (path,))
    row = c.fetchone(); conn.close()
    return row[0] if row else None

def set_docker_link(path, container_name):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    if container_name: c.execute('INSERT OR REPLACE INTO docker_links (path, container_name) VALUES (?, ?)', (path, container_name))
    else: c.execute('DELETE FROM docker_links WHERE path = ?', (path,))
    conn.commit(); conn.close()

# --- NOTIFICACIONES ---
def send_notification(message, is_success=True):
    provider = get_setting('notify_provider', 'none')
    prefix = "✅ ShieldPi: " if is_success else "❌ ShieldPi Error: "
    full_msg = prefix + message

    try:
        if provider == 'telegram':
            token = get_setting('notify_token')
            chatid = get_setting('notify_chatid')
            if token and chatid:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = urllib.parse.urlencode({'chat_id': chatid, 'text': full_msg}).encode()
                urllib.request.urlopen(url, data=data)
        
        elif provider == 'webhook':
            url = get_setting('notify_url')
            if url:
                # Formato generico JSON (Funciona en Discord, Slack, Gotify)
                headers = {'Content-Type': 'application/json'}
                data = json.dumps({'content': full_msg, 'message': full_msg, 'priority': 5}).encode()
                req = urllib.request.Request(url, data=data, headers=headers)
                urllib.request.urlopen(req)
                
        return True
    except Exception as e:
        print(f"Error enviando notificacion: {e}")
        return False

# --- FUNCIONES KOPIA ---
def get_repo_status():
    if not os.path.exists(KOPIA_CONFIG): return False, None
    success, out, _ = run_kopia(['repository', 'status', '--json'])
    if success:
        try:
            data = json.loads(out)
            return True, data.get('storage', {}).get('config', {}).get('path', 'Desconocido')
        except: return True, "Conectado"
    return False, None

def get_policies():
    success, out, _ = run_kopia(['policy', 'list', '--json'])
    sources = []
    if success:
        try:
            policies_raw = json.loads(out)
            unique_paths = set()
            for p in policies_raw:
                path = p.get('target', {}).get('path', '')
                if path: unique_paths.add(path)
            for path in unique_paths:
                s, o, _ = run_kopia(['policy', 'get', path, '--json'])
                ignores = []
                if s:
                    try:
                        pol_data = json.loads(o)
                        files = pol_data.get('files', {})
                        if not files: files = pol_data.get('definition', {}).get('files', {})
                        ignores = files.get('ignore', [])
                        if not ignores: ignores = files.get('ignoreRules', [])
                    except: pass
                if not isinstance(ignores, list): ignores = []
                d_link = get_docker_link(path)
                sources.append({'path': path, 'ignores': ignores, 'docker_link': d_link})
            sources.sort(key=lambda x: x['path'])
        except: pass
    return sources

def get_last_snapshot_time():
    success, out, _ = run_kopia(['snapshot', 'list', '--json'])
    if success:
        try:
            data = json.loads(out)
            if data and len(data) > 0:
                data.sort(key=lambda x: x.get('startTime', ''), reverse=True)
                raw_time = data[0].get('startTime', '')
                try: 
                    dt_utc = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                    dt_local = dt_utc.astimezone() 
                    return dt_local.strftime('%Y-%m-%d %I:%M %p')
                except: return raw_time
        except: pass
    return "Nunca"

# --- THREAD AUTO ---
def scheduler_loop():
    while True:
        try:
            freq = get_setting('freq', 'manual')
            target_time = get_setting('time', '03:00')
            last_run_date = get_setting('last_run_date', '')
            if freq == 'daily':
                now = datetime.now()
                current_time_str = now.strftime('%H:%M')
                today_str = now.strftime('%Y-%m-%d')
                if current_time_str == target_time and last_run_date != today_str:
                    sources = get_policies()
                    paths = [s['path'] for s in sources]
                    if paths:
                        success, _, err = run_kopia(['snapshot', 'create'] + paths)
                        if success: 
                            set_setting('last_run_date', today_str)
                            send_notification("Backup Automático Completado Exitosamente.")
                        else:
                            send_notification(f"Fallo en Backup Automático: {err}", is_success=False)
        except: pass
        time.sleep(60)

# --- AUTH ---
def user_exists():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT count(*) FROM users'); count = c.fetchone()[0]
    conn.close(); return count > 0
def create_user(u, p):
    try:
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (u, generate_password_hash(p)))
        conn.commit(); conn.close(); return True
    except: return False
def verify_user(u, p):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT password_hash FROM users WHERE username = ?', (u,))
    row = c.fetchone(); conn.close()
    return True if row and check_password_hash(row[0], p) else False

if not os.path.exists('/app/config'): os.makedirs('/app/config')
init_db()
sched_thread = threading.Thread(target=scheduler_loop, daemon=True)
sched_thread.start()

# --- RUTAS ---
@app.before_request
def check_auth():
    if request.endpoint in ['static', 'setup', 'login']: return
    if not user_exists(): return redirect(url_for('setup'))
    if 'user' not in session and request.endpoint != 'login': return redirect(url_for('login'))

@app.route('/')
def home():
    is_connected, repo_path = get_repo_status()
    if not is_connected: return redirect(url_for('repo_setup'))
    schedule = {'frequency': get_setting('freq', 'manual'), 'time': get_setting('time', '03:00')}
    retention = int(get_setting('retention', '5'))
    server_time = datetime.now().strftime('%I:%M %p')
    
    # Datos Notificacion
    notify_cfg = {
        'provider': get_setting('notify_provider', 'none'),
        'token': get_setting('notify_token', ''),
        'chatid': get_setting('notify_chatid', ''),
        'url': get_setting('notify_url', '')
    }

    return render_template('dashboard.html', 
                          user=session['user'], 
                          hostname=socket.gethostname(), 
                          repo_path=repo_path, 
                          sources=get_policies(), 
                          last_backup=get_last_snapshot_time(), 
                          schedule=schedule,
                          retention=retention,
                          server_time=server_time,
                          notify_cfg=notify_cfg)

# --- RUTAS NOTIFICACION ---
@app.route('/settings/notifications', methods=['POST'])
def settings_notifications():
    set_setting('notify_provider', request.form.get('notify_provider'))
    set_setting('notify_token', request.form.get('telegram_token'))
    set_setting('notify_chatid', request.form.get('telegram_chatid'))
    set_setting('notify_url', request.form.get('webhook_url'))
    flash("Configuración de notificaciones guardada.")
    return redirect(url_for('home'))

@app.route('/api/test_notification')
def test_notification():
    if send_notification("Esta es una prueba de conexión desde ShieldPi."):
        return "Prueba enviada correctamente. Revisa tu App."
    else:
        return "Error enviando prueba. Revisa los logs o tu configuración."

@app.route('/settings/retention', methods=['POST'])
def settings_retention():
    val = request.form.get('keep_latest')
    if val:
        success, _, err = run_kopia(['policy', 'set', '--global', '--keep-latest', val])
        if success:
            set_setting('retention', val)
            flash(f"Política actualizada: Se conservarán los últimos {val} backups.")
        else:
            flash(f"Error actualizando política: {err}")
    return redirect(url_for('home'))

@app.route('/schedule/update', methods=['POST'])
def schedule_update():
    set_setting('freq', request.form.get('frequency'))
    if request.form.get('time'): set_setting('time', request.form.get('time'))
    flash("Programación actualizada."); return redirect(url_for('home'))

@app.route('/api/docker/list', methods=['GET'])
def api_docker_list():
    success, out, _ = run_command(['docker', 'ps', '--format', '{{.Names}}', '-a'])
    containers = [l.strip() for l in out.splitlines() if l.strip()] if success else []
    return jsonify({'containers': containers})

@app.route('/source/link_docker', methods=['POST'])
def source_link_docker():
    path = request.form.get('path'); container = request.form.get('container_name')
    if path: set_docker_link(path, container); flash(f"Vínculo actualizado para {path}")
    return redirect(url_for('home'))

@app.route('/api/browse', methods=['POST'])
def api_browse():
    try:
        current_path = request.json.get('path', '/host')
        if not current_path.startswith('/host'): current_path = '/host'
        items = []
        with os.scandir(current_path) as it:
            for entry in it: items.append({'name': entry.name, 'path': entry.path, 'type': 'dir' if entry.is_dir() else 'file'})
        items.sort(key=lambda x: (x['type'] != 'dir', x['name']))
        return jsonify({'current': current_path, 'parent': os.path.dirname(current_path) if len(os.path.dirname(current_path)) >= 5 else '/host', 'items': items})
    except Exception as e: return jsonify({'error': str(e)}), 500

@app.route('/restore/history', methods=['GET'])
def restore_history():
    path = request.args.get('path')
    if not path: return redirect(url_for('home'))
    docker_link = get_docker_link(path)
    success, out, _ = run_kopia(['snapshot', 'list', path, '--json'])
    snapshots = []
    if success:
        try:
            raw_snaps = json.loads(out)
            for s in raw_snaps:
                size = s.get('stats', {}).get('totalSize', 0)
                size_str = f"{size} B"
                if size > 1024: size_str = f"{size/1024:.1f} KB"
                if size > 1024*1024: size_str = f"{size/(1024*1024):.1f} MB"
                raw_time = s.get('startTime', '')
                try: 
                    dt_utc = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                    dt_local = dt_utc.astimezone()
                    display_time = dt_local.strftime('%Y-%m-%d %I:%M %p')
                except: display_time = raw_time
                snapshots.append({'id': s.get('id', ''), 'short_id': s.get('id', '')[:8], 'time': display_time, 'size': size_str, 'files': s.get('stats', {}).get('fileCount', 0)})
            snapshots.reverse()
        except: pass
    return render_template('history.html', snapshots=snapshots, source_path=path, docker_link=docker_link)

@app.route('/backup/restore', methods=['POST'])
def backup_restore():
    snap_id = request.form.get('snapshot_id'); path = request.form.get('path')
    if not snap_id or not path: return redirect(url_for('home'))
    docker_container = get_docker_link(path)
    if docker_container: run_command(['docker', 'stop', docker_container])
    success, _, err = run_kopia(['snapshot', 'restore', snap_id, path])
    if docker_container: run_command(['docker', 'start', docker_container])
    if success: flash("Restauración completada exitosamente.")
    else: flash(f"Error en restauración: {err}")
    return redirect(url_for('restore_history', path=path))

@app.route('/snapshot/delete', methods=['POST'])
def snapshot_delete():
    snap_id = request.form.get('snapshot_id'); path = request.form.get('path')
    if snap_id:
        success, _, err = run_kopia(['snapshot', 'delete', snap_id, '--delete'])
        if success: flash("Backup eliminado del historial.")
        else: flash(f"Error eliminando backup: {err}")
    return redirect(url_for('restore_history', path=path))

@app.route('/repo/setup')
def repo_setup():
    is_connected, _ = get_repo_status()
    if is_connected: return redirect(url_for('home'))
    return render_template('repo.html')
@app.route('/repo/create', methods=['POST'])
def repo_create():
    path = request.form.get('path'); password = request.form.get('repo_password')
    if not os.path.exists(path): os.makedirs(path, exist_ok=True)
    env = {'KOPIA_PASSWORD': password}
    success, _, err = run_kopia(['repository', 'create', 'filesystem', '--path', path], env)
    if not success and "found existing data" in err: success, _, err = run_kopia(['repository', 'connect', 'filesystem', '--path', path], env)
    if success: run_kopia(['policy', 'set', '--global', '--keep-latest', '5'], env); flash('Inicializado.'); return redirect(url_for('home'))
    flash(f'Error: {err}'); return redirect(url_for('repo_setup'))
@app.route('/source/add', methods=['POST'])
def source_add():
    path = request.form.get('path'); success, _, err = run_kopia(['policy', 'set', path, '--compression', 'zstd'])
    if success: flash(f'Carpeta agregada: {path}')
    else: flash(f'Error: {err}')
    return redirect(url_for('home'))
@app.route('/source/ignore', methods=['POST'])
def source_ignore():
    path = request.form.get('path'); target = request.form.get('target')
    if path and target and target.startswith(path): run_kopia(['policy', 'set', path, '--add-ignore', os.path.relpath(target, path)]); flash('Excluido.')
    return redirect(url_for('home'))
@app.route('/source/delete', methods=['POST'])
def source_delete():
    path = request.form.get('path'); run_kopia(['policy', 'delete', path]); set_docker_link(path, None); flash(f'Removido: {path}')
    return redirect(url_for('home'))
@app.route('/backup/run', methods=['POST'])
def backup_run():
    sources = get_policies(); paths = [s['path'] for s in sources]
    if paths: 
        success, _, err = run_kopia(['snapshot', 'create'] + paths)
        if success: 
            send_notification("Backup Manual Completado Exitosamente.")
            flash("Backup completado exitosamente.")
        else: 
            send_notification(f"Fallo en Backup Manual: {err}", is_success=False)
            flash(f"Error: {err}")
    else: flash("Agrega carpetas primero.")
    return redirect(url_for('home'))
@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if request.method=='POST': create_user(request.form.get('username'), request.form.get('password')); return redirect(url_for('login'))
    return render_template('setup.html')
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method=='POST': 
        if verify_user(request.form.get('username'), request.form.get('password')): session['user']=request.form.get('username'); return redirect(url_for('home'))
    return render_template('login.html')
@app.route('/logout')
def logout(): session.pop('user',None); return redirect(url_for('login'))

if __name__ == '__main__': app.run(host='0.0.0.0', port=51515)
