import os
import shutil
import logging
import platform
from datetime import datetime
from flask import Flask, request, jsonify, render_template
from PIL import Image

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

# --- ROUTES ---

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/browse', methods=['POST'])
def browse_filesystem():
    """Returns a list of directories for the given path to support the UI Folder Picker."""
    data = request.get_json()
    current_path = data.get('path')

    # Default to root if no path provided
    if not current_path:
        current_path = os.path.expanduser("~") 

    if not os.path.isdir(current_path):
        return jsonify({"error": "Invalid directory"}), 400

    folders = []
    try:
        # List directories only
        for item in os.listdir(current_path):
            full_path = os.path.join(current_path, item)
            if os.path.isdir(full_path):
                folders.append(item)
    except PermissionError:
        return jsonify({"error": "Permission denied", "path": current_path}), 403

    # Add parent directory option (..)
    parent_dir = os.path.dirname(current_path)
    
    return jsonify({
        "current_path": current_path,
        "parent_path": parent_dir,
        "folders": sorted(folders)
    })

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

    if not all([folder_path, target_filename, width, height]):
        return jsonify({"error": "Missing required fields"}), 400

    if not os.path.isdir(folder_path):
        return jsonify({"error": "Directory does not exist"}), 404

    found_files = []
    processed_count = 0
    details = [] 

    logging.info(f"Scan started in {folder_path}. Dry Run: {dry_run}")

    # Recursive Scan
    for root, dirs, files in os.walk(folder_path):
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
            
            success, msg = process_image(full_path, int(width), int(height), dry_run)
            
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
    
    if not os.path.isdir(folder_path):
        return jsonify({"error": "Directory does not exist"}), 404
        
    backups = []
    for root, dirs, files in os.walk(folder_path):
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
    
    restored_count = 0
    logs = []

    for item in files_to_restore:
        backup = item['backup_path']
        original = item['original_path']
        
        try:
            # 1. Overwrite original with backup
            shutil.copy2(backup, original)
            
            # 2. DELETE the backup file (NEW CHANGE)
            os.remove(backup)
            
            restored_count += 1
            logs.append(f"Restored & Deleted Backup: {os.path.basename(original)}")
        except Exception as e:
            logs.append(f"Error restoring {backup}: {str(e)}")

    return jsonify({"status": "completed", "restored": restored_count, "logs": logs})

# --- MAIN ENTRY POINT ---
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5002, debug=True)
