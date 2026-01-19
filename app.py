import os
import io
import json
import shutil
import logging
import platform
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from PIL import Image

# SMB Imports
from smbclient import shutil as smb_shutil
from smbclient import path as smb_path
import smbclient

app = Flask(__name__)

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
SMB_CONFIG_FILE = "smb_configs.json"
APP_VERSION = "1.0.0"

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

def process_image(file_path, width, height, dry_run=False):
    """Backs up and resizes a single image. respecting dry_run."""
    try:
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup_path = f"{file_path}.backup_{timestamp}"
        
        if dry_run:
            return True, f"[DRY RUN] Would backup to {os.path.basename(backup_path)} and resize to {width}x{height}"

        # 1. Perform Backup
        shutil.copy2(file_path, backup_path)
        
        # 2. Resize Image
        with Image.open(file_path) as img:
            resized_img = img.resize((width, height))
            resized_img.save(file_path)
            
        return True, f"Success (Backup: {os.path.basename(backup_path)})"
    except Exception as e:
        return False, str(e)

@app.route('/scan-and-resize', methods=['POST'])
def scan_and_resize():
    data = request.get_json()

    folder_path = data.get('folder_path')
    target_filename = data.get('image_name')
    width = data.get('width')
    height = data.get('height')
    dry_run = data.get('dry_run', False)
    smb_id = data.get('smb_id')

    if not all([folder_path, target_filename, width, height]):
        return jsonify({"error": "Missing required fields"}), 400

    is_smb = False
    walker = os.walk
    
    if smb_id:
        setup_smb_session(smb_id)
        is_smb = True
        walker = smbclient.walk
        # Check dir using smbclient
        if not smbclient.path.isdir(folder_path):
             return jsonify({"error": "Directory does not exist"}), 404
    elif not os.path.isdir(folder_path):
        return jsonify({"error": "Directory does not exist"}), 404

    found_files = []
    processed_count = 0
    details = [] 

    logging.info(f"Scan started in {folder_path}. Dry Run: {dry_run}")

    # Recursive Scan
    for root, dirs, files in walker(folder_path):
        if target_filename in files:
            full_path = os.path.join(root, target_filename)
            
            # --- SKIP IF BACKUP EXISTS ---
            backup_prefix = f"{target_filename}.backup_"
            has_backup = any(f.startswith(backup_prefix) for f in files)
            
            if has_backup:
                msg = "Skipped (Backup already exists)"
                details.append(f"[SKIPPED] {full_path} -> {msg}")
                logging.info(f"Skipping {full_path} because a backup was found.")
                found_files.append(full_path)
                continue
            # -----------------------------

            found_files.append(full_path)
            
            success, msg = process_image(full_path, int(width), int(height), dry_run, is_smb=is_smb)
            
            status_str = "OK" if success else "FAIL"
            details.append(f"[{status_str}] {full_path} -> {msg}")
            
            if success:
                processed_count += 1
                logging.info(f"Processed: {full_path} - {msg}")
            else:
                logging.error(f"Failed: {full_path} - {msg}")

    result = {
        "status": "completed",
        "dry_run": dry_run,
        "scanned_path": folder_path,
        "files_found": len(found_files),
        "processed": processed_count,
        "logs": details 
    }
    
    return jsonify(result)

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

@app.route('/restore', methods=['POST'])
def restore_files():
    """Restores selected backup files and DELETES the backup."""
    data = request.get_json()
    files_to_restore = data.get('files') 
    smb_id = data.get('smb_id')
    
    restored_count = 0
    logs = []

    for item in files_to_restore:
        backup = item['backup_path']
        original = item['original_path']
        
        if smb_id:
            setup_smb_session(smb_id)
        
        try:
            # 1. Overwrite original with backup
            if smb_id:
                smbclient.shutil.copyfile(backup, original)
            else:
                shutil.copy2(backup, original)
            
            # 2. DELETE the backup file
            if smb_id:
                smbclient.remove(backup)
            else:
                os.remove(backup)
            
            restored_count += 1
            logs.append(f"Restored & Deleted Backup: {os.path.basename(original)}")
        except Exception as e:
            logs.append(f"Error restoring {backup}: {str(e)}")

    return jsonify({"status": "completed", "restored": restored_count, "logs": logs})

# --- MAIN ENTRY POINT ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
