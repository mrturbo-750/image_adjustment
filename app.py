import os
import io
import json
import shutil
import logging
import platform
import uuid
import threading
from queue import Queue
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, request, jsonify, render_template, Response
from PIL import Image

# SMB Imports
from smbclient import shutil as smb_shutil
from smbclient import path as smb_path
import smbclient

app = Flask(__name__)

# In-memory store for task queues
task_queues = {}

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("service.log"),
        logging.StreamHandler()
    ]
)
# --- CONFIGURATION ---
SMB_CONFIG_FILE = "data/smb_configs.json"
APP_VERSION = "1.0.0"

@app.route('/log-stream/<task_id>')
def log_stream(task_id):
    def stream():
        q = task_queues.get(task_id)
        if not q:
            yield f"data: {json.dumps({'type': 'log', 'level': 'error', 'message': 'Task ID not found.'})}\n\n"
            yield f"data: {json.dumps({'type': 'EOF'})}\n\n"
            return

        while True:
            message = q.get()
            yield f"data: {json.dumps(message)}\n\n"
            if isinstance(message, dict) and message.get("type") == "EOF":
                break
        
        if task_id in task_queues:
            del task_queues[task_id]

    return Response(stream(), mimetype='text/event-stream')

def load_smb_configs():
    if not os.path.exists(SMB_CONFIG_FILE):
        return []
    try:
        with open(SMB_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def save_smb_configs(configs):
    with open(SMB_CONFIG_FILE, 'w') as f:
        json.dump(configs, f, indent=4)

def get_smb_config(smb_id):
    configs = load_smb_configs()
    for c in configs:
        if c.get('id') == smb_id:
            return c
    return None

def setup_smb_session(smb_id):
    """Registers an SMB session for the given config ID."""
    config = get_smb_config(smb_id)
    if not config:
        raise ValueError("SMB Config not found")
    
    smbclient.register_session(
        config['server'],
        username=config['username'],
        password=config['password']
    )
    return config

# --- ROUTES ---

@app.context_processor
def inject_version():
    return dict(version=APP_VERSION)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/settings')
def settings():
    return render_template('settings.html')

@app.route('/browse', methods=['POST'])
def browse_filesystem():
    """Returns a list of directories for the given path to support the UI Folder Picker."""
    data = request.get_json()
    current_path = data.get('path')
    smb_id = data.get('smb_id')

    folders = []
    parent_dir = ""

    try:
        if smb_id:
            # --- SMB MODE ---
            config = setup_smb_session(smb_id)
            
            # Default to share root if no path provided
            if not current_path:
                current_path = f"\\\\{config['server']}\\{config['share']}"
            
            if not smbclient.path.isdir(current_path):
                return jsonify({"error": "Invalid directory"}), 400

            for item in smbclient.listdir(current_path):
                full_path = os.path.join(current_path, item)
                if smbclient.path.isdir(full_path):
                    folders.append(item)
            
            parent_dir = os.path.dirname(current_path)

        else:
            # --- LOCAL MODE ---
            if not current_path:
                current_path = os.path.expanduser("~") 

            if not os.path.isdir(current_path):
                return jsonify({"error": "Invalid directory"}), 400

            for item in os.listdir(current_path):
                full_path = os.path.join(current_path, item)
                if os.path.isdir(full_path):
                    folders.append(item)
            
            parent_dir = os.path.dirname(current_path)

    except Exception as e:
        return jsonify({"error": str(e), "path": current_path}), 403
    
    return jsonify({
        "current_path": current_path,
        "parent_path": parent_dir,
        "folders": sorted(folders)
    })

@app.route('/test-smb-connection', methods=['POST'])
def test_smb_connection():
    """Tests SMB credentials."""
    data = request.get_json()
    server = data.get('server')
    share = data.get('share')
    username = data.get('username')
    password = data.get('password')

    if not all([server, share, username, password]):
        return jsonify({"success": False, "error": "Missing required fields"}), 400

    try:
        smbclient.register_session(server, username=username, password=password)
        path = f"\\\\{server}\\{share}"
        smbclient.listdir(path)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/smb-configs', methods=['GET', 'POST', 'DELETE'])
def manage_smb_configs():
    """Manage SMB configurations."""
    if request.method == 'GET':
        return jsonify(load_smb_configs())
    
    data = request.get_json()
    configs = load_smb_configs()

    if request.method == 'POST':
        new_config = {
            'id': str(datetime.now().timestamp()),
            'name': data.get('name'),
            'server': data.get('server'),
            'share': data.get('share'),
            'username': data.get('username'),
            'password': data.get('password')
        }
        configs.append(new_config)
        save_smb_configs(configs)
        return jsonify({"status": "added", "config": new_config})

    elif request.method == 'DELETE':
        smb_id = data.get('id')
        configs = [c for c in configs if c['id'] != smb_id]
        save_smb_configs(configs)
        return jsonify({"status": "deleted"})

    return jsonify({"error": "Invalid action"}), 400

def process_image(file_path, width, height, dry_run=False, is_smb=False):
    """Backs up and resizes a single image, respecting dry_run and SMB paths."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = f"{file_path}.backup_{timestamp}"
        
        # Determine which basename function to use
        basename = os.path.basename

        if dry_run:
            return True, f"[DRY RUN] Would backup to {basename(backup_path)} and resize to {width}x{height}"

        # 1. Perform Backup
        if is_smb:
            smb_shutil.copyfile(file_path, backup_path)
        else:
            shutil.copy2(file_path, backup_path)
        
        # 2. Resize Image
        if is_smb:
            # Read the entire file into memory to avoid locking issues on SMB shares
            with smbclient.open_file(file_path, 'rb') as f:
                image_data = f.read()

            with Image.open(io.BytesIO(image_data)) as img:
                img_format = img.format
                resized_img = img.resize((width, height))
                
                with io.BytesIO() as buffer:
                    resized_img.save(buffer, format=img_format)
                    buffer.seek(0)
                    with smbclient.open_file(file_path, 'wb') as remote_f:
                        shutil.copyfileobj(buffer, remote_f)
        else:
            with Image.open(file_path) as img:
                img_format = img.format
                resized_img = img.resize((width, height))
                resized_img.save(file_path, format=img_format)
            
        return True, f"Success (Backup: {basename(backup_path)})"
    except Exception as e:
        logging.error(f"Error processing {file_path}: {e}")
        return False, str(e)

def scan_and_resize_task(task_id, folder_path, target_filename, width, height, dry_run, smb_id, max_workers, detailed_logs):
    q = task_queues[task_id]

    def log(level, message):
        q.put({'type': 'log', 'level': level, 'message': message})

    try:
        if not all([folder_path, target_filename, width, height]):
            log('error', "Missing required fields")
            q.put({'type': 'EOF'})
            return

        is_smb = False
        walker = os.walk
        
        if smb_id:
            try:
                setup_smb_session(smb_id)
                is_smb = True
                walker = smbclient.walk
                if not smbclient.path.isdir(folder_path):
                    log('error', "Directory does not exist")
                    q.put({'type': 'EOF'})
                    return
            except Exception as e:
                log('error', f"SMB connection failed: {e}")
                q.put({'type': 'EOF'})
                return
        elif not os.path.isdir(folder_path):
            log('error', "Directory does not exist")
            q.put({'type': 'EOF'})
            return

        found_files = []
        files_to_process = []
        processed_count = 0

        log('info', f"Scan started in {folder_path}. Dry Run: {dry_run}")

        for root, dirs, files in walker(folder_path):
            if detailed_logs:
                log('scan', f"Scanning: {root}")
            if target_filename in files:
                full_path = os.path.join(root, target_filename)
                found_files.append(full_path)
                
                backup_prefix = f"{target_filename}.backup_"
                has_backup = any(f.startswith(backup_prefix) for f in files)
                
                if has_backup:
                    msg = "Skipped (Backup already exists)"
                    log('skipped', f"{full_path} -> {msg}")
                    continue
                
                files_to_process.append(full_path)

        files_to_process = list(set(files_to_process))

        if files_to_process:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_path = {
                    executor.submit(process_image, path, int(width), int(height), dry_run, is_smb): path 
                    for path in files_to_process
                }

                for future in as_completed(future_to_path):
                    path = future_to_path[future]
                    try:
                        success, msg = future.result()
                        status_str = "OK" if success else "FAIL"
                        log('success' if success else 'error', f"[{status_str}] {path} -> {msg}")
                        
                        if success:
                            processed_count += 1
                    except Exception as exc:
                        log('error', f'[FAIL] {path} -> {exc}')
        
        summary_data = {
            "files_found": len(found_files),
            "processed": processed_count,
        }
        q.put({'type': 'summary', 'data': summary_data})
        q.put({'type': 'EOF'})

    except Exception as e:
        log('error', f"An unexpected error occurred: {str(e)}")
        q.put({'type': 'EOF'})

@app.route('/scan-and-resize', methods=['POST'])
def scan_and_resize():
    data = request.get_json()
    task_id = str(uuid.uuid4())
    task_queues[task_id] = Queue()

    args = (
        task_id,
        data.get('folder_path'),
        data.get('image_name'),
        data.get('width'),
        data.get('height'),
        data.get('dry_run', False),
        data.get('smb_id'),
        data.get('max_workers', 10),
        data.get('detailed_logs', True)
    )

    thread = threading.Thread(target=scan_and_resize_task, args=args)
    thread.start()

    return jsonify({'task_id': task_id})

@app.route('/scan-backups', methods=['POST'])
def scan_backups():
    """Scans for existing backup files so the user can choose to restore them."""
    data = request.get_json()
    folder_path = data.get('folder_path')
    smb_id = data.get('smb_id')
    
    walker = os.walk
    if smb_id:
        setup_smb_session(smb_id)
        walker = smbclient.walk
        if not smbclient.path.isdir(folder_path):
            return jsonify({"error": "Directory does not exist"}), 404
    elif not os.path.isdir(folder_path):
        return jsonify({"error": "Directory does not exist"}), 404
        
    backups = []
    # Use the appropriate walker (local or smb)
    for root, dirs, files in walker(folder_path):
        for file in files:
            if ".backup_" in file:
                full_path = os.path.join(root, file)
                original_name = file.split(".backup_")[0]
                backups.append({
                    "backup_path": full_path,
                    "original_path": os.path.join(root, original_name),
                    "filename": file
                })
    
    return jsonify({"backups": backups})

def process_restore(backup_path, original_path, is_smb=False):
    """Restores a single backup file and deletes it."""
    try:
        if is_smb:
            # 1. Overwrite original with backup
            smb_shutil.copyfile(backup_path, original_path)
            # 2. DELETE the backup file
            smbclient.remove(backup_path)
        else:
            # 1. Overwrite original with backup
            shutil.copy2(backup_path, original_path)
            # 2. DELETE the backup file
            os.remove(backup_path)
        
        return True, f"Restored & Deleted Backup: {os.path.basename(original_path)}"
    except Exception as e:
        return False, f"Error restoring {backup_path}: {str(e)}"

def restore_backups_task(task_id, files_to_restore, smb_id, max_workers):
    q = task_queues[task_id]

    def log(level, message):
        q.put({'type': 'log', 'level': level, 'message': message})

    try:
        is_smb = False
        if smb_id:
            try:
                setup_smb_session(smb_id)
                is_smb = True
            except Exception as e:
                log('error', f"SMB connection failed: {e}")
                q.put({'type': 'EOF'})
                return

        restored_count = 0
        log('info', f"Restoring {len(files_to_restore)} files...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_item = {
                executor.submit(process_restore, item['backup_path'], item['original_path'], is_smb): item
                for item in files_to_restore
            }

            for future in as_completed(future_to_item):
                item = future_to_item[future]
                try:
                    success, msg = future.result()
                    log('success' if success else 'error', msg)
                    if success:
                        restored_count += 1
                except Exception as exc:
                    log('error', f'Error processing {item["backup_path"]}: {exc}')
        
        summary_data = {
            "files_to_restore": len(files_to_restore),
            "restored": restored_count,
        }
        q.put({'type': 'summary', 'data': summary_data})
        q.put({'type': 'EOF'})

    except Exception as e:
        log('error', f"An unexpected error occurred: {str(e)}")
        q.put({'type': 'EOF'})

@app.route('/restore', methods=['POST'])
def restore_files():
    """Restores selected backup files and DELETES the backup."""
    data = request.get_json()
    task_id = str(uuid.uuid4())
    task_queues[task_id] = Queue()

    args = (
        task_id,
        data.get('files'),
        data.get('smb_id'),
        data.get('max_workers', 10)
    )

    thread = threading.Thread(target=restore_backups_task, args=args)
    thread.start()

    return jsonify({'task_id': task_id})

# --- MAIN ENTRY POINT ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
