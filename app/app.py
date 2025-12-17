import os
import socket
import sqlite3
import subprocess
import json
import threading
import time
import shutil
import urllib.request
import urllib.parse
from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

# --- CONFIGURACION ---
app.secret_key = 'shieldpi_clave_maestra_fija_v2.8_inplace'
DB_PATH = '/app/config/shieldpi.db'
KOPIA_CONFIG = '/app/config/repository.config'

# --- GESTION DE ZONA HORARIA DINAMICA ---
try:
    target_tz = os.environ.get('TZ', 'UTC')
    LOCAL_TZ = ZoneInfo(target_tz)
    print(f"Zona Horaria Configurada: {target_tz}", flush=True)
except Exception as e:
    print(f"Error cargando TZ, usando UTC: {e}", flush=True)
    LOCAL_TZ = ZoneInfo("UTC")

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
    c.execute('''CREATE TABLE IF NOT EXISTS cloud_config (id INTEGER PRIMARY KEY CHECK (id = 1), provider TEXT, bucket TEXT, access_key TEXT, secret_key TEXT, endpoint TEXT, region TEXT)''')
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

def get_cloud_config():
    conn = sqlite3.connect(DB_PATH); conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM cloud_config WHERE id = 1')
    row = c.fetchone(); conn.close()
    return dict(row) if row else None

def set_cloud_config(provider, bucket, access, secret, endpoint, region):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO cloud_config (id, provider, bucket, access_key, secret_key, endpoint, region) VALUES (1, ?, ?, ?, ?, ?, ?)', 
              (provider, bucket, access, secret, endpoint, region))
    conn.commit(); conn.close()

# --- NOTIFICACIONES ---
def send_notification(message, is_success=True):
    provider = get_setting('notify_provider', 'none')
    prefix = "ShieldPi: " if is_success else "ShieldPi Error: "
    full_msg = prefix + message
    try:
        if provider == 'telegram':
            token = get_setting('notify_token'); chatid = get_setting('notify_chatid')
            if token and chatid:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                data = urllib.parse.urlencode({'chat_id': chatid, 'text': full_msg}).encode()
                urllib.request.urlopen(url, data=data)
        elif provider == 'webhook':
            url = get_setting('notify_url')
            if url:
                headers = {'Content-Type': 'application/json'}
                data = json.dumps({'content': full_msg, 'message': full_msg, 'priority': 5}).encode()
                req = urllib.request.Request(url, data=data, headers=headers)
                urllib.request.urlopen(req)
        return True
    except Exception as e:
        print(f"Error Notif: {e}"); return False

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
                path = p.get('target', {}).get('path', ''); 
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
                    dt_local = dt_utc.astimezone(LOCAL_TZ) 
                    return dt_local.strftime('%Y-%m-%d %I:%M %p')
                except: return raw_time
        except: pass
    return "Nunca"

# --- SCHEDULER ---
def scheduler_loop():
    while True:
        try:
            freq = get_setting('freq', 'manual'); target_time = get_setting('time', '03:00'); last_run_date = get_setting('last_run_date', '')
            if freq == 'daily':
                now = datetime.now(LOCAL_TZ)
                current_time_str = now.strftime('%H:%M'); today_str = now.strftime('%Y-%m-%d')
                if current_time_str == target_time and last_run_date != today_str:
                    sources = get_policies(); paths = [s['path'] for s in sources]
                    if paths:
                        print(f"--- Auto Backup: {today_str} ---", flush=True)
                        success, _, err = run_kopia(['snapshot', 'create'] + paths)
                        if success: 
                            set_setting('last_run_date', today_str)
                            sync_msg = ""
                            cloud_cfg = get_cloud_config()
                            if cloud_cfg:
                                print("--- Auto Sync Nube ---", flush=True)
                                cmd_sync = ['repository', 'sync-to', 's3', '--bucket', cloud_cfg['bucket'], '--access-key', cloud_cfg['access_key'], '--secret-access-key', cloud_cfg['secret_key']]
                                if cloud_cfg['endpoint']: cmd_sync.extend(['--endpoint', cloud_cfg['endpoint']])
                                if cloud_cfg['region']: cmd_sync.extend(['--region', cloud_cfg['region']])
                                s_sync, _, err_sync = run_kopia(cmd_sync)
                                if s_sync: sync_msg = " + Sync Nube OK"
                                else: print(f"Error Sync: {err_sync}", flush=True); sync_msg = f" + Error Nube"
                            send_notification(f"Ciclo Diario Completado: Backup Local OK {sync_msg}")
                        else: send_notification(f"Fallo Backup Local: {err}", is_success=False)
        except Exception as e: print(f"Scheduler Error: {e}", flush=True)
        time.sleep(60)

# --- AUTH ---
def user_exists(): conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute('SELECT count(*) FROM users'); count = c.fetchone()[0]; conn.close(); return count > 0
def create_user(u, p): 
    try: conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', (u, generate_password_hash(p))); conn.commit(); conn.close(); return True
    except: return False
def verify_user(u, p): conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute('SELECT password_hash FROM users WHERE username = ?', (u,)); row = c.fetchone(); conn.close(); return True if row and check_password_hash(row[0], p) else False

if not os.path.exists('/app/config'): os.makedirs('/app/config')
init_db()
sched_thread = threading.Thread(target=scheduler_loop, daemon=True); sched_thread.start()

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
    server_time = datetime.now(LOCAL_TZ).strftime('%I:%M %p')
    return render_template('dashboard.html', user=session['user'], hostname=socket.gethostname(), repo_path=repo_path, sources=get_policies(), last_backup=get_last_snapshot_time(), schedule={'frequency': get_setting('freq', 'manual'), 'time': get_setting('time', '03:00')}, retention=int(get_setting('retention', '5')), server_time=server_time, notify_cfg={'provider': get_setting('notify_provider', 'none'), 'token': get_setting('notify_token', ''), 'chatid': get_setting('notify_chatid', ''), 'url': get_setting('notify_url', '')}, cloud_cfg=get_cloud_config())

@app.route('/settings/notifications', methods=['POST'])
def settings_notifications(): set_setting('notify_provider', request.form.get('notify_provider')); set_setting('notify_token', request.form.get('telegram_token')); set_setting('notify_chatid', request.form.get('telegram_chatid')); set_setting('notify_url', request.form.get('webhook_url')); flash("Notificaciones guardadas."); return redirect(url_for('home'))
@app.route('/settings/cloud', methods=['POST'])
def settings_cloud(): set_cloud_config('s3', request.form.get('bucket'), request.form.get('access_key'), request.form.get('secret_key'), request.form.get('endpoint'), request.form.get('region')); flash("Nube guardada."); return redirect(url_for('home'))

@app.route('/api/sync/run', methods=['POST'])
def sync_run():
    cfg = get_cloud_config()
    if not cfg: flash("Configura la nube primero."); return redirect(url_for('home'))
    cmd = ['repository', 'sync-to', 's3', '--bucket', cfg['bucket'], '--access-key', cfg['access_key'], '--secret-access-key', cfg['secret_key']]
    if cfg['endpoint']: cmd.extend(['--endpoint', cfg['endpoint']])
    if cfg['region']: cmd.extend(['--region', cfg['region']])
    success, _, err = run_kopia(cmd)
    if success: send_notification("Sync Nube Manual OK."); flash("Sincronización OK.")
    else: print(f"Sync Error: {err}", flush=True); send_notification(f"Sync Error: {err}", False); flash(f"Error: {err}")
    return redirect(url_for('home'))

@app.route('/api/test_notification')
def test_notification(): return "OK" if send_notification("Test ShieldPi") else "Error"
@app.route('/settings/retention', methods=['POST'])
def settings_retention(): v=request.form.get('keep_latest'); run_kopia(['policy', 'set', '--global', '--keep-latest', v]); set_setting('retention', v); return redirect(url_for('home'))
@app.route('/schedule/update', methods=['POST'])
def schedule_update(): set_setting('freq', request.form.get('frequency')); set_setting('time', request.form.get('time')); return redirect(url_for('home'))
@app.route('/api/docker/list', methods=['GET'])
def api_docker_list(): s,o,_=run_command(['docker', 'ps', '--format', '{{.Names}}', '-a']); return jsonify({'containers': [l.strip() for l in o.splitlines() if l.strip()] if s else []})
@app.route('/source/link_docker', methods=['POST'])
def source_link_docker(): p=request.form.get('path'); c=request.form.get('container_name'); set_docker_link(p,c); return redirect(url_for('home'))
@app.route('/api/browse', methods=['POST'])
def api_browse():
    try:
        cp = request.json.get('path', '/host'); cp = '/host' if not cp.startswith('/host') else cp
        items = []; 
        with os.scandir(cp) as it:
            for e in it: items.append({'name': e.name, 'path': e.path, 'type': 'dir' if e.is_dir() else 'file'})
        items.sort(key=lambda x: (x['type'] != 'dir', x['name'])); return jsonify({'current': cp, 'parent': os.path.dirname(cp) if len(os.path.dirname(cp))>=5 else '/host', 'items': items})
    except Exception as e: return jsonify({'error': str(e)}), 500
@app.route('/restore/history', methods=['GET'])
def restore_history(): 
    p=request.args.get('path'); 
    if not p: return redirect(url_for('home'))
    d=get_docker_link(p); s,o,_=run_kopia(['snapshot', 'list', p, '--json']); snaps=[]
    if s:
        try:
            rs=json.loads(o)
            for x in rs: 
                sz=x.get('stats',{}).get('totalSize',0); szs=f"{sz} B"
                if sz>1024: szs=f"{sz/1024:.1f} KB"
                if sz>1048576: szs=f"{sz/1048576:.1f} MB"
                raw_time = x.get('startTime', '')
                try:
                    dt_utc = datetime.fromisoformat(raw_time.replace('Z', '+00:00'))
                    display_time = dt_utc.astimezone(LOCAL_TZ).strftime('%Y-%m-%d %I:%M %p')
                except: display_time = raw_time
                snaps.append({'id': x.get('id',''), 'short_id': x.get('id','')[:8], 'time': display_time, 'size': szs, 'files': x.get('stats',{}).get('fileCount',0)})
            snaps.reverse()
        except: pass
    return render_template('history.html', snapshots=snaps, source_path=p, docker_link=d)
@app.route('/backup/restore', methods=['POST'])
def backup_restore():
    sid=request.form.get('snapshot_id'); p=request.form.get('path'); d=get_docker_link(p)
    if d: run_command(['docker', 'stop', d])
    s,_,e=run_kopia(['snapshot', 'restore', sid, p])
    if d: run_command(['docker', 'start', d])
    flash("Restaurado." if s else f"Error: {e}"); return redirect(url_for('restore_history', path=p))
@app.route('/snapshot/delete', methods=['POST'])
def snapshot_delete(): sid=request.form.get('snapshot_id'); p=request.form.get('path'); run_kopia(['snapshot', 'delete', sid, '--delete']); return redirect(url_for('restore_history', path=p))
@app.route('/source/add', methods=['POST'])
def source_add(): run_kopia(['policy', 'set', request.form.get('path'), '--compression', 'zstd']); return redirect(url_for('home'))
@app.route('/source/ignore', methods=['POST'])
def source_ignore(): run_kopia(['policy', 'set', request.form.get('path'), '--add-ignore', os.path.relpath(request.form.get('target'), request.form.get('path'))]); return redirect(url_for('home'))
@app.route('/source/delete', methods=['POST'])
def source_delete(): run_kopia(['policy', 'delete', request.form.get('path')]); set_docker_link(request.form.get('path'), None); return redirect(url_for('home'))

@app.route('/backup/run', methods=['POST'])
def backup_run():
    s = get_policies(); p = [x['path'] for x in s]
    success, _, err = run_kopia(['snapshot', 'create'] + p)
    if success:
        flash_msg = "Backup Local Exitoso."
        cloud_cfg = get_cloud_config()
        if cloud_cfg:
            print("--- Auto Sync tras Backup Manual ---", flush=True)
            cmd_sync = ['repository', 'sync-to', 's3', '--bucket', cloud_cfg['bucket'], '--access-key', cloud_cfg['access_key'], '--secret-access-key', cloud_cfg['secret_key']]
            if cloud_cfg['endpoint']: cmd_sync.extend(['--endpoint', cloud_cfg['endpoint']])
            if cloud_cfg['region']: cmd_sync.extend(['--region', cloud_cfg['region']])
            s_sync, _, err_sync = run_kopia(cmd_sync)
            if s_sync:
                flash_msg += " Y Sincronización a Nube Completada."
                send_notification("Backup Manual + Sync Nube Exitoso")
            else:
                flash_msg += f" Pero falló la Nube: {err_sync}"
                print(f"Error Sync Manual: {err_sync}", flush=True)
                send_notification(f"Sync Nube falló: {err_sync}", False)
        else: send_notification("Backup Manual Local Exitoso")
        flash(flash_msg)
    else:
        flash(f"Error: {err}"); send_notification(f"Fallo Backup: {err}", False)
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

@app.route('/repo/setup')
def repo_setup():
    if os.path.exists(KOPIA_CONFIG): return redirect(url_for('home'))
    return render_template('repo.html')

@app.route('/repo/create', methods=['POST'])
def repo_create():
    pwd=request.form.get('repo_password'); env={'KOPIA_PASSWORD': pwd}; prov=request.form.get('provider')
    cmd=['filesystem', '--path', request.form.get('path')] if prov=='filesystem' else []
    if prov=='s3':
        cmd=['s3', '--bucket', request.form.get('bucket'), '--access-key', request.form.get('access_key'), '--secret-access-key', request.form.get('secret_key'), '--endpoint', request.form.get('endpoint'), '--region', request.form.get('region')]
    s,_,e = run_kopia(['repository', 'create']+cmd, env)
    if not s: s,_,e = run_kopia(['repository', 'connect']+cmd, env)
    if s: 
        run_kopia(['policy', 'set', '--global', '--keep-latest', '5'], env)
        return redirect(url_for('home'))
    print(f"Repo Error: {e}", flush=True); flash(f"Error: {e}"); return redirect(url_for('repo_setup'))

# --- RESCATE v2.8 (IN-PLACE RESTORE) ---
@app.route('/repo/rescue', methods=['POST'])
def repo_rescue():
    # Nota: local_path en el form se ignora para la restauracion de archivos, 
    # pero lo usamos para saber donde poner el Repo Local (Database).
    repo_location = request.form.get('local_path'); 
    pwd = request.form.get('repo_password')
    bucket = request.form.get('bucket'); access = request.form.get('access_key'); secret = request.form.get('secret_key')
    endpoint = request.form.get('endpoint'); region = request.form.get('region'); env = {'KOPIA_PASSWORD': pwd}
    
    print("--- INICIANDO RESCATE v2.8 (IN-PLACE) ---", flush=True)

    # 1. Conectar a Nube
    s, _, e = run_kopia(['repository', 'connect', 's3', '--bucket', bucket, '--access-key', access, '--secret-access-key', secret, '--endpoint', endpoint, '--region', region], env)
    if not s: flash(f"Error Nube: {e}"); return redirect(url_for('repo_setup'))

    # 2. Obtener Snapshots
    s, out, _ = run_kopia(['snapshot', 'list', '--json', '--all'], env)
    
    restored_count = 0
    if s:
        try:
            snaps = json.loads(out)
            snaps.sort(key=lambda x: x.get('startTime', ''), reverse=True)
            
            # Agrupar por origen unico (Rutas Originales)
            unique_sources = {}
            for snap in snaps:
                src_path = snap.get('source', {}).get('path', '')
                if src_path and src_path not in unique_sources:
                    unique_sources[src_path] = snap['id']
            
            # 3. RESTAURACION EN SITIO (In-Place)
            for src_path, snap_id in unique_sources.items():
                print(f"Restaurando IN-PLACE: {src_path} (Snap: {snap_id})", flush=True)
                
                # Restauramos DIRECTAMENTE a la ruta original (src_path)
                s_res, _, err_res = run_kopia(['snapshot', 'restore', snap_id, src_path], env)
                
                if s_res:
                    restored_count += 1
                else:
                    print(f"Error restaurando {src_path}: {err_res}", flush=True)

        except Exception as ex:
            print(f"Error procesando snapshots: {ex}", flush=True)

    if restored_count > 0:
        # 4. Reconfiguracion Local (Database)
        if os.path.exists(KOPIA_CONFIG): os.remove(KOPIA_CONFIG)
        
        # Usamos la ruta que puso el usuario SOLO para guardar la DB de Kopia, no los archivos.
        # Si el usuario puso /host/backups, ahi vivira la DB.
        repo_storage = repo_location
        if os.path.exists(repo_storage): shutil.rmtree(repo_storage, ignore_errors=True)
        if not os.path.exists(repo_storage): os.makedirs(repo_storage, exist_ok=True)
        
        s, _, e = run_kopia(['repository', 'create', 'filesystem', '--path', repo_storage], env)
        
        if s:
            set_cloud_config('s3', bucket, access, secret, endpoint, region)
            flash(f"¡Rescate Exitoso! Se restauraron {restored_count} rutas en su ubicación original.")
            return redirect(url_for('home'))
        else:
            flash(f"Datos restaurados, pero falló config local: {e}")
            return redirect(url_for('repo_setup'))
    else:
        flash("No se encontraron snapshots válidos.")
        return redirect(url_for('repo_setup'))

if __name__ == '__main__': app.run(host='0.0.0.0', port=51515)
