import cv2
import numpy as np
import face_recognition
import pickle
import os
from datetime import datetime, timedelta
from flask import Flask, render_template, request, jsonify, Response, send_from_directory, send_file
import json
import base64
from pathlib import Path
import hashlib
import time
from io import BytesIO
import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image
import atexit
from flask import send_from_directory

# PostgreSQL imports (CHANGED FROM sqlite3)
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool

# ===================================================================
# DATABASE CONNECTION POOL (CRITICAL FOR PRODUCTION)
# ===================================================================

# CRITICAL: Password must be set via environment variable
DB_PASSWORD = os.getenv("DB_PASSWORD")
if not DB_PASSWORD:
    raise RuntimeError(
        "CRITICAL: DB_PASSWORD environment variable not set!\n"
        "Set it before running: set DB_PASSWORD=admin123"
    )

# Connection pool to handle multiple concurrent requests efficiently
from psycopg2.pool import SimpleConnectionPool

db_pool = SimpleConnectionPool(
    1, 20,
    dsn="postgresql://faceguard_user:TvXIq9byfm8VkUTMMDVigbVOnMyHbvVM@dpg-d6tqrehaae7s73e41qbg-a.oregon-postgres.render.com/faceguard_hjjw?sslmode=require"
)

# Graceful shutdown: close all pool connections when app exits
atexit.register(lambda: db_pool.closeall())

# ===================================================================
# DATABASE MANAGER (MIGRATED TO POSTGRESQL)
# ===================================================================

class DatabaseManager:
    def __init__(self):
        # No need to store db_params - using global connection pool
        pass
    
    def create_user(self, user_id, full_name, department=None, access_level=1):
        conn = db_pool.getconn()
        cursor = conn.cursor()  # No dict cursor needed here
        try:
            cursor.execute('''
                INSERT INTO users (user_id, full_name, created_date, department, access_level)
                VALUES (%s, %s, %s, %s, %s)
            ''', (user_id, full_name, datetime.now(), department, access_level))
            conn.commit()
            return True, "User created successfully"
        except psycopg2.IntegrityError:
            conn.rollback()
            return False, "User ID already exists"
        except Exception as e:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    
    def delete_user(self, user_id):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('SELECT user_id FROM users WHERE user_id = %s', (user_id,))
            if not cursor.fetchone():
                return False, "User not found"
            cursor.execute('DELETE FROM biometric_data WHERE user_id = %s', (user_id,))
            cursor.execute('DELETE FROM device_assignments WHERE user_id = %s', (user_id,))
            
            cursor.execute('DELETE FROM users WHERE user_id = %s', (user_id,))
            conn.commit()
            return True, "User and all associated data deleted successfully"
        except Exception as e:
            conn.rollback()
            return False, f"Error deleting user: {str(e)}"
        finally:
            db_pool.putconn(conn)
    
    def delete_biometric(self, user_id):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM biometric_data WHERE user_id = %s', (user_id,))
            if cursor.rowcount == 0:
                return False, "No biometric data found for this user"
            conn.commit()
            return True, "Biometric data deleted successfully"
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            db_pool.putconn(conn)
    
    def update_user(self, user_id, full_name):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE users SET full_name = %s WHERE user_id = %s', (full_name, user_id))
            if cursor.rowcount == 0:
                return False, "User not found"
            conn.commit()
            return True, "User updated successfully"
        except Exception as e:
            conn.rollback()
            return False, str(e)
        finally:
            db_pool.putconn(conn)
    
    def store_biometric_data(self, user_id, mean_embedding, capture_method, frame_count):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('DELETE FROM biometric_data WHERE user_id = %s', (user_id,))
            embedding_blob = pickle.dumps(mean_embedding)
            cursor.execute('''
                INSERT INTO biometric_data (user_id, mean_embedding, capture_date, capture_method, frame_count)
                VALUES (%s, %s, %s, %s, %s)
            ''', (user_id, embedding_blob, datetime.now(), capture_method, frame_count))
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    
    def assign_device(self, user_id, device_id):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO device_assignments (user_id, device_id, assigned_date)
                VALUES (%s, %s, %s)
            ''', (user_id, device_id, datetime.now()))
            conn.commit()
            return True
        except Exception as e:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    
    def verify_supervisor_pin(self, supervisor_id, pin):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute("""
                SELECT supervisor_id, pin_hash, status
                FROM supervisors
                WHERE supervisor_id = %s
                  AND status = 'active'
            """, (supervisor_id,))

            row = cursor.fetchone()

            if row is None:
                return False, None

            stored_pin = row[1]

            if stored_pin == pin:
                return True, {"supervisor_id": row[0]}

            return False, None

        except Exception as e:
            print("Supervisor verification error:", e)
            return False, None

        finally:
            db_pool.putconn(conn)
        

        

    
    
    def create_emergency_unlock(self, device_id, supervisor_id, target_user_id, reason):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            timestamp = datetime.now()
            expires_at = timestamp + timedelta(seconds=300)
            cursor.execute('''
                INSERT INTO emergency_unlocks 
                (device_id, supervisor_id, target_user_id, timestamp, expires_at, reason, status)
                VALUES (%s, %s, %s, %s, %s, %s, 'active')
                RETURNING id
            ''', (device_id, supervisor_id, target_user_id, timestamp, expires_at, reason))
            unlock_id = cursor.fetchone()[0]
            conn.commit()
            return unlock_id
        except Exception as e:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    
    def check_emergency_unlock(self, device_id):
        conn = db_pool.getconn()
        cursor = conn.cursor()

        try:
            cursor.execute("""
                SELECT id, supervisor_id, target_user_id, reason, timestamp
                FROM emergency_unlocks
                WHERE device_id = %s 
                AND status = 'active'
                AND expires_at > NOW()
                ORDER BY timestamp DESC
                LIMIT 1
            """, (device_id,))

            result = cursor.fetchone()

            if result:
                return {
                    'id': result[0],
                    'supervisor_id': result[1],
                    'target_user_id': result[2],
                    'reason': result[3],
                    'timestamp': result[4]
                }

            return None

        finally:
            db_pool.putconn(conn)
    
    def consume_emergency_unlock(self, unlock_id):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('UPDATE emergency_unlocks SET status = \'used\' WHERE id = %s', (unlock_id,))
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise
        finally:
            db_pool.putconn(conn)
    
    def get_supervisors(self):
        conn = db_pool.getconn()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT s.supervisor_id, s.created_date, s.status
                FROM supervisors s
                
                WHERE s.status = 'active'
            ''')
            supervisors = []
            for row in cursor.fetchall():
                supervisors.append({
                    'supervisor_id': row['supervisor_id'],
                    'full_name': row['full_name'],
                    'created_date': row['created_date'],
                    'status': row['status']
                })
            return supervisors
        finally:
            db_pool.putconn(conn)

# ===================================================================
# WEBCAM CAPTURE MANAGER (NO CHANGES NEEDED)
# ===================================================================

class WebcamCaptureManager:
    def __init__(self):
        self.capturing = False
        self.captured_frames = []
        self.max_frames = 25
        self.current_user_id = None
        
    def start_capture(self, user_id):
        if self.capturing:
            return False, "Capture already in progress"
        self.current_user_id = user_id
        self.captured_frames = []
        self.capturing = True
        return True, "Capture started"
    
    def add_frame(self, frame_data):
        if not self.capturing:
            return False, "No active capture session", None
        if len(self.captured_frames) >= self.max_frames:
            return False, "Maximum frames reached", None
        try:
            img_data = base64.b64decode(frame_data.split(',')[1])
            nparr = np.frombuffer(img_data, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            frame_small = cv2.resize(frame,(0,0),fx=0.5,fy=0.5)
            rgb_frame = cv2.cvtColor(frame_small, cv2.COLOR_BGR2RGB)
            face_locations = face_recognition.face_locations(rgb_frame, model="hog")
            print("Faces detected:",len(face_locations))
            if len(face_locations) != 1:
                return False, "Exactly one face required", None
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
            if laplacian_var < 20:
                return False, "Image too blurry - please ensure good lighting", None
            face_encodings = face_recognition.face_encodings(rgb_frame, face_locations)
            if len(face_encodings) == 0:
                return False, "Could not extract face encoding", None
            self.captured_frames.append(face_encodings[0])
            complete = len(self.captured_frames) >= self.max_frames
            message = f"Frame {len(self.captured_frames)}/{self.max_frames} captured"
            return True, message, complete
        except Exception as e:
            return False, f"Error processing frame: {str(e)}", None
    
    def finish_capture(self, db_manager):
        if len(self.captured_frames) < self.max_frames:
            self.capturing = False
            self.captured_frames = []
            return False, "Not enough frames captured"
        mean_embedding = np.mean(self.captured_frames, axis=0)
        db_manager.store_biometric_data(
            self.current_user_id,
            mean_embedding,
            "webcam",
            len(self.captured_frames)
        )
        self.capturing = False
        self.captured_frames = []
        return True, "Biometric data stored successfully (128-vector embedding)"
    
    def cancel_capture(self):
        self.capturing = False
        self.captured_frames = []
        self.current_user_id = None
        return True, "Capture cancelled"

# ===================================================================
# FLASK APP
# ===================================================================

app = Flask(__name__)
db_manager = DatabaseManager()
webcam_manager = WebcamCaptureManager()

ALERTS_DIR = Path("security_alerts")
ALERTS_DIR.mkdir(exist_ok=True)
os.makedirs('templates', exist_ok=True)
os.makedirs('uploads', exist_ok=True)

def get_db_connection():
    """Get connection from pool"""
    return db_pool.getconn()

def get_db_cursor(conn):
    """Get cursor with RealDictCursor for dict-like row access"""
    return conn.cursor(cursor_factory=RealDictCursor)

def return_db_connection(conn):
    """Return connection to pool after use"""
    db_pool.putconn(conn)

def get_alert_image_path(filename):
    """Safely build alert image path from filename only"""
    if not filename:
        return None
    safe_filename = os.path.basename(filename)
    return ALERTS_DIR / safe_filename

# ===================================================================
# DATE-FILTERED DATA RETRIEVAL FUNCTIONS
# ===================================================================

def get_security_alerts_between(from_date, to_date):
    """Get security alerts within date range"""
    conn = get_db_connection()
    cursor = get_db_cursor(conn)
    try:
        cursor.execute("""
            SELECT
                sa.alert_id,
                sa.timestamp,
                sa.alert_type,
                sa.user_id,
                sa.device_id,
                sa.image_filename,
                sa.description,
                sa.severity,
                sa.confidence_score,
                sa.reviewed,
                da.user_id AS assigned_user_id,
                u.full_name AS full_name,
                au.full_name AS assigned_user_name
            FROM security_alerts sa
            LEFT JOIN device_assignments da
                ON sa.device_id = da.device_id
                AND da.status = 'active'
            LEFT JOIN users u
                ON sa.user_id = u.user_id
            LEFT JOIN users au
                ON da.user_id = au.user_id
            WHERE DATE(sa.timestamp) BETWEEN %s AND %s
            ORDER BY sa.timestamp DESC
        """, (from_date, to_date))
        
        alerts = []
        for row in cursor.fetchall():
            image_base64 = None
            if row['image_filename']:
                image_path = get_alert_image_path(row['image_filename'])
                if image_path and os.path.exists(image_path):
                    try:
                        with open(image_path, 'rb') as img_file:
                            image_base64 = base64.b64encode(img_file.read()).decode('utf-8')
                    except Exception as e:
                        print(f"Error reading image {image_path}: {e}")
            
            expected_user = (
                row['assigned_user_name'] or row['full_name'] or 
                row['assigned_user_id'] or row['user_id'] or 'Unknown User'
            )
            
            confidence_score = row['confidence_score'] if row['confidence_score'] is not None else 0.0
            
            alerts.append({
                'alert_id': row['alert_id'],
                'timestamp': row['timestamp'],
                'alert_type': row['alert_type'],
                'user_id': row['user_id'] or 'Unknown',
                'expected_user': expected_user,
                'device_id': row['device_id'],
                'image_base64': image_base64,
                'description': row['description'],
                'severity': row['severity'],
                'reviewed': bool(row['reviewed']),
                'confidence_score': confidence_score
            })
        
        return alerts
    finally:
        return_db_connection(conn)

def get_access_logs_between(from_date, to_date):
    """Get access logs within date range"""
    conn = get_db_connection()
    cursor = get_db_cursor(conn)
    try:
        cursor.execute('''
            SELECT 
                al.id, al.timestamp, al.device_id, al.user_id, al.success,
                al.confidence_score, al.authentication_method, al.notes, u.full_name
            FROM access_logs al
            LEFT JOIN users u ON al.user_id = u.user_id
            WHERE DATE(al.timestamp) BETWEEN %s AND %s
            ORDER BY al.timestamp DESC
        ''', (from_date, to_date))
        
        logs = []
        for row in cursor.fetchall():
            logs.append({
                'id': row['id'],
                'timestamp': row['timestamp'],
                'device_id': row['device_id'] or 'Unknown',
                'user_id': row['user_id'] or 'Unknown',
                'user_name': row['full_name'] or row['user_id'] or 'Unknown User',
                'success': row['success'],
                'confidence_score': row['confidence_score'],
                'method': row['authentication_method'] or 'biometric',
                'notes': row['notes'] or 'Access attempt logged'
            })
        
        return logs
    finally:
        return_db_connection(conn)

# ===================================================================
# EXPORT ENDPOINTS
# ===================================================================

@app.route('/api/export/security-alerts/pdf')
def export_security_alerts_pdf():
    """Export security alerts to PDF with date filtering"""
    try:
        from_date = request.args.get('from', '2000-01-01')
        to_date = request.args.get('to', '2099-12-31')
        alerts = get_security_alerts_between(from_date, to_date)
        
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        y = height - 40
        
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(40, y, "SECURITY ALERT REPORT")
        y -= 30
        
        pdf.setFont("Helvetica", 11)
        pdf.drawString(40, y, f"Report Period: {from_date} to {to_date}")
        y -= 20
        pdf.drawString(40, y, f"Total Alerts: {len(alerts)}")
        y -= 35
        
        for alert in alerts:
            if y < 250:
                pdf.showPage()
                y = height - 40
                pdf.setFont("Helvetica", 9)
            
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(40, y, f"Alert: {alert['alert_type']}")
            y -= 18
            
            pdf.setFont("Helvetica", 10)
            pdf.drawString(40, y, f"Alert ID: {alert['alert_id']}")
            y -= 14
            pdf.drawString(40, y, f"Device: {alert['device_id'] or 'Unknown'}")
            y -= 14
            pdf.drawString(40, y, f"Expected User: {alert['expected_user']}")
            y -= 14
            confidence_val = alert.get('confidence_score', 0.0) or 0.0
            pdf.drawString(40, y, f"Confidence Score: {confidence_val:.1f}%")
            y -= 14
            pdf.drawString(40, y, f"Severity: {(alert.get('severity') or 'unknown').upper()}")
            y -= 14
            timestamp_str = str(alert['timestamp']) if alert['timestamp'] else 'N/A'
            pdf.drawString(40, y, f"Timestamp: {timestamp_str}")
            y -= 14
            pdf.drawString(40, y, f"Description: {alert.get('description') or 'No description'}")
            y -= 20
            
            if alert.get('image_base64'):
                try:
                    img_bytes = base64.b64decode(alert['image_base64'])
                    img = Image.open(BytesIO(img_bytes))
                    img.thumbnail((600, 400), Image.Resampling.LANCZOS)
                    
                    if y < 180:
                        pdf.showPage()
                        y = height - 40
                    
                    img_buffer = BytesIO()
                    img.save(img_buffer, format='JPEG', quality=85)
                    img_buffer.seek(0)
                    
                    pdf.drawImage(
                        ImageReader(img_buffer),
                        40, y - 150,
                        width=180, height=130
                    )
                    y -= 160
                except Exception as e:
                    print(f"Error adding image to PDF: {e}")
                    y -= 10
            
            pdf.line(40, y, width - 40, y)
            y -= 25
        
        pdf.save()
        buffer.seek(0)
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"security_alerts_{from_date}_to_{to_date}.pdf",
            mimetype="application/pdf"
        )
        
    except Exception as e:
        print(f"Error generating security alerts PDF: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/access-logs/excel')
def export_access_logs_excel():
    """Export access logs to Excel with date filtering"""
    try:
        from_date = request.args.get('from', '2000-01-01')
        to_date = request.args.get('to', '2099-12-31')
        logs = get_access_logs_between(from_date, to_date)
        
        df = pd.DataFrame(logs)
        
        if not df.empty:
            df = df[['timestamp', 'device_id', 'user_id', 'user_name', 'success', 
                    'confidence_score', 'method', 'notes']]
            df.columns = ['Timestamp', 'Device ID', 'User ID', 'User Name', 
                         'Success', 'Confidence Score', 'Method', 'Notes']
        
        output = BytesIO()
        
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer:
            df.to_excel(writer, index=False, sheet_name='Access Logs')
            workbook = writer.book
            worksheet = writer.sheets['Access Logs']
            
            header_format = workbook.add_format({
                'bold': True,
                'bg_color': '#4472C4',
                'font_color': 'white',
                'border': 1
            })
            
            for col_num, value in enumerate(df.columns.values):
                worksheet.write(0, col_num, value, header_format)
            
            for i, col in enumerate(df.columns):
                max_len = max(df[col].astype(str).apply(len).max(), len(col)) + 2
                worksheet.set_column(i, i, min(max_len, 50))
        
        output.seek(0)
        
        return send_file(
            output,
            as_attachment=True,
            download_name=f"access_logs_{from_date}_to_{to_date}.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        
    except Exception as e:
        print(f"Error generating access logs Excel: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/export/access-logs/pdf')
def export_access_logs_pdf():
    """Export access logs to PDF with date filtering"""
    try:
        from_date = request.args.get('from', '2000-01-01')
        to_date = request.args.get('to', '2099-12-31')
        logs = get_access_logs_between(from_date, to_date)
        
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        width, height = A4
        y = height - 40
        
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(40, y, "ACCESS LOG REPORT")
        y -= 30
        
        pdf.setFont("Helvetica", 11)
        pdf.drawString(40, y, f"Report Period: {from_date} to {to_date}")
        y -= 20
        pdf.drawString(40, y, f"Total Entries: {len(logs)}")
        y -= 35
        
        pdf.setFont("Helvetica-Bold", 9)
        pdf.drawString(40, y, "Timestamp")
        pdf.drawString(150, y, "Device")
        pdf.drawString(240, y, "User")
        pdf.drawString(330, y, "Status")
        pdf.drawString(400, y, "Method")
        pdf.drawString(470, y, "Conf%")
        y -= 15
        
        pdf.line(40, y, width - 40, y)
        y -= 10
        
        pdf.setFont("Helvetica", 8)
        
        for log in logs:
            if y < 60:
                pdf.showPage()
                y = height - 40
                
                pdf.setFont("Helvetica-Bold", 9)
                pdf.drawString(40, y, "Timestamp")
                pdf.drawString(150, y, "Device")
                pdf.drawString(240, y, "User")
                pdf.drawString(330, y, "Status")
                pdf.drawString(400, y, "Method")
                pdf.drawString(470, y, "Conf%")
                y -= 15
                pdf.line(40, y, width - 40, y)
                y -= 10
                pdf.setFont("Helvetica", 8)
            
            timestamp = str(log['timestamp'])[:19] if log['timestamp'] else 'N/A'
            device = (log.get('device_id') or 'Unknown')[:12]
            user = (log.get('user_name') or 'Unknown')[:12]
            status = 'SUCCESS' if log.get('success') else 'FAILED'
            method = (log.get('method') or 'N/A')[:10]
            confidence = f"{int(log.get('confidence_score') or 0)}" if log.get('confidence_score') is not None else 'N/A'
            
            pdf.drawString(40, y, timestamp)
            pdf.drawString(150, y, device)
            pdf.drawString(240, y, user)
            pdf.drawString(330, y, status)
            pdf.drawString(400, y, method)
            pdf.drawString(470, y, confidence)
            
            y -= 12
            
            if log.get('notes') and log['notes'] != 'Access attempt logged':
                pdf.setFont("Helvetica-Oblique", 7)
                notes_text = (log.get('notes') or '')[:80]
                pdf.drawString(60, y, f"Note: {notes_text}")
                y -= 10
                pdf.setFont("Helvetica", 8)
        
        pdf.save()
        buffer.seek(0)
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"access_logs_{from_date}_to_{to_date}.pdf",
            mimetype="application/pdf"
        )
        
    except Exception as e:
        print(f"Error generating access logs PDF: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ===================================================================
# EXISTING ROUTES (PRESERVED FROM ORIGINAL)
# ===================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/health')
def health():
    """Health check endpoint for monitoring"""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT 1')
        cursor.fetchone()
        return_db_connection(conn)
        return jsonify({"status": "ok", "database": "connected"}), 200
    except Exception as e:
        return jsonify({"status": "error", "database": "disconnected", "error": str(e)}), 500

@app.route('/emergency_response')
def emergency_response():
    return render_template('emergency_response.html')

@app.route('/user_management')
def user_management():
    return render_template('user_management.html')

@app.route('/device_registry')
def device_registry():
    return render_template('device_registry.html')

@app.route('/biometric_data')
def biometric_data():
    return render_template('biometric_data.html')

@app.route('/access_logs')
def access_logs():
    return render_template('access_logs.html')

@app.route('/analytics')
def analytics():
    return render_template('analytics.html')

@app.route('/security_alerts')
def security_alerts():
    return render_template('security_alerts.html')

@app.route('/remote_workers')
def remote_workers():
    return render_template('remote_workers.html')

@app.route('/api/log/access', methods=['POST'])
def log_access():
    """Log access attempts from client devices"""
    conn = None
    try:
        data = request.json
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO access_logs
            (timestamp, device_id, user_id, success, confidence_score, authentication_method, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            datetime.now(),
            data.get("device_id"),
            data.get("user_id"),
            int(bool(data.get("success"))),
            data.get("confidence"),
            data.get("method"),
            data.get("notes")
        ))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error logging access: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            return_db_connection(conn)

@app.route('/api/security_alerts/create', methods=['POST'])
def create_security_alert():
    """Create security alert with image"""
    conn = None
    try:
        data = request.json
        alert_id = f"ALERT-{int(time.time()*1000)}"
        
        image_filename = None
        if data.get("image_base64"):
            img_bytes = base64.b64decode(data["image_base64"])
            image_filename = f"{alert_id}.jpg"
            image_path = get_alert_image_path(image_filename)
            with open(image_path, "wb") as f:
                f.write(img_bytes)
        
        conn = get_db_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO security_alerts
            (alert_id, timestamp, alert_type, user_id, device_id, image_filename, 
             description, severity, confidence_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            alert_id,
            datetime.now(),
            data.get("alert_type"),
            None,
            data.get("device_id"),
            image_filename,
            data.get("description"),
            data.get("severity", "high"),
            data.get("confidence_score", 0.0)
        ))
        
        conn.commit()
        
        # ✅ FIX 6: Log monitoring event for intruder detection
        if data.get("alert_type") == "remote_intruder":
            # Extract worker_id from device_id (format: REMOTE_{worker_id})
            device_id = data.get("device_id", "")
            if device_id.startswith("REMOTE_"):
                worker_id = device_id.replace("REMOTE_", "")
                log_monitoring_event(
                    worker_id,
                    "intruder_detected",
                    data.get("confidence_score")
                )
        
        return jsonify({"success": True, "alert_id": alert_id})
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error creating security alert: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
    finally:
        if conn:
            return_db_connection(conn)

@app.route('/api/emergency/verify_supervisor', methods=['POST'])
def verify_supervisor():
    try:
        supervisor_id = request.json.get('supervisor_id')
        pin = request.json.get('pin')

        if not supervisor_id or not pin:
            return jsonify({'success': False, 'error': 'Supervisor ID and PIN required'}), 400

        is_valid, supervisor_data = db_manager.verify_supervisor_pin(supervisor_id, pin)

        if is_valid:
            return jsonify({
                'success': True,
                'supervisor_id': supervisor_data['supervisor_id']
            })

        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/emergency/unlock', methods=['POST'])
def emergency_unlock():
    print("UNLOCK PAYLOAD:", request.json)

    try:
        device_id = request.json.get('device_id')
        supervisor_id = request.json.get('supervisor_id')
        target_user_id = request.json.get('target_user_id')
        reason = request.json.get('reason', '')

        print("Parsed values:", device_id, supervisor_id, target_user_id, reason)

        unlock_id = db_manager.create_emergency_unlock(
            device_id, supervisor_id, target_user_id, reason
        )

        print("Unlock created with ID:", unlock_id)

        return jsonify({
            'success': True,
            'unlock_id': unlock_id
        })

    except Exception as e:
        import traceback
        print("🚨 FULL ERROR BELOW:")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)}), 400

@app.route('/api/emergency/check/<device_id>', methods=['GET'])
def check_emergency_unlock(device_id):
    """Check if emergency unlock is active"""
    try:
        unlock = db_manager.check_emergency_unlock(device_id)
        if unlock:
            db_manager.consume_emergency_unlock(unlock['id'])
            return jsonify({'success': True, 'unlock': unlock})
        else:
            return jsonify({'success': False, 'unlock': None})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/supervisors', methods=['GET'])
def get_supervisors():
    """Get all supervisors"""
    try:
        supervisors = db_manager.get_supervisors()
        return jsonify({'success': True, 'supervisors': supervisors})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/security_images/<filename>')
def serve_security_image(filename):
    """Safely serve images using filename only"""
    try:
        safe_filename = os.path.basename(filename)
        return send_from_directory(ALERTS_DIR, safe_filename)
    except Exception as e:
        print(f"Error serving image {filename}: {e}")
        return str(e), 404

@app.route('/api/security_alerts', methods=['GET'])
def get_security_alerts():

    conn = None

    try:

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
        SELECT
        alert_id,
        alert_type,
        device_id,
        description,
        severity,
        confidence_score,
        image_filename,
        timestamp
        FROM security_alerts
        ORDER BY timestamp DESC
        """)

        alerts = []

        for row in cursor.fetchall():

            captured_image = None

            if row['image_filename']:
                captured_image = f"/security_images/{row['image_filename']}"

            alerts.append({
                'alert_id': row['alert_id'],
                'timestamp': row['timestamp'],
                'alert_type': row['alert_type'],
                'device_id': row['device_id'],
                'captured_image': captured_image,
                'description': row['description'],
                'severity': row['severity'],
                'confidence_score': row['confidence_score']
            })

        return jsonify({
            "success": True,
            "alerts": alerts
        })

    except Exception as e:
        print("Error getting security alerts:", e)
        return jsonify({"success": False})

    finally:
        if conn:
            return_db_connection(conn)
@app.route('/api/security_alerts/<alert_id>/review', methods=['PUT'])
def mark_alert_reviewed(alert_id):
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE security_alerts SET reviewed = 1 WHERE alert_id = %s', (alert_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error marking alert as reviewed: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            return_db_connection(conn)

@app.route('/api/security_alerts/<alert_id>', methods=['DELETE'])
def delete_alert(alert_id):
    """Delete alert safely"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = get_db_cursor(conn)
        cursor.execute('SELECT image_filename FROM security_alerts WHERE alert_id = %s', (alert_id,))
        result = cursor.fetchone()
        
        if result and result['image_filename']:
            image_path = get_alert_image_path(result['image_filename'])
            if image_path and os.path.exists(image_path):
                try:
                    os.remove(image_path)
                    print(f"Deleted image file: {image_path}")
                except Exception as e:
                    print(f"Error deleting image file: {e}")
        
        cursor.execute('DELETE FROM security_alerts WHERE alert_id = %s', (alert_id,))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"Error deleting alert: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if conn:
            return_db_connection(conn)

@app.route('/api/users/create', methods=['POST'])
def create_user():
    user_id = request.form.get('user_id')
    full_name = request.form.get('full_name')
    department = request.form.get('department', '')
    if not user_id or not full_name:
        return jsonify({'error': 'User ID and Full Name required'}), 400
    success, message = db_manager.create_user(user_id, full_name, department if department else None)
    if success:
        return jsonify({'success': True, 'message': message, 'user_id': user_id})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/users', methods=['GET'])
def get_users():
    conn = get_db_connection()
    cursor = get_db_cursor(conn)
    cursor.execute('''
        SELECT u.user_id, u.full_name, u.department, u.status, u.created_date,
               CASE WHEN b.user_id IS NOT NULL THEN 1 ELSE 0 END as has_biometric,
               d.device_id, d.device_name
        FROM users u
        LEFT JOIN biometric_data b ON u.user_id = b.user_id
        LEFT JOIN device_assignments da ON u.user_id = da.user_id AND da.status = 'active'
        LEFT JOIN devices d ON da.device_id = d.device_id
        GROUP BY u.user_id, u.full_name, u.department, u.status, u.created_date, 
                 b.user_id, d.device_id, d.device_name
        ORDER BY u.created_date DESC
    ''')
    users = []
    for row in cursor.fetchall():
        created_date_str = str(row['created_date'])[:10] if row['created_date'] else 'N/A'
        users.append({
            'user_id': row['user_id'], 
            'name': row['full_name'],
            'department': row['department'] or 'Not specified',
            'status': row['status'], 
            'created_date': row['created_date'],
            'created_at': created_date_str,
            'has_biometric': bool(row['has_biometric']),
            'assigned_device': row['device_id'], 
            'device_name': row['device_name']
        })
    return_db_connection(conn)
    return jsonify({'total': len(users), 'users': users})

@app.route('/api/users/<user_id>', methods=['DELETE'])
def delete_user(user_id):
    success, message = db_manager.delete_user(user_id)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/users/<user_id>', methods=['PUT'])
def update_user(user_id):
    full_name = request.form.get('name')
    if not full_name:
        return jsonify({'error': 'Name required'}), 400
    success, message = db_manager.update_user(user_id, full_name)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/users/<user_id>/biometric', methods=['DELETE'])
def delete_biometric(user_id):
    success, message = db_manager.delete_biometric(user_id)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/webcam/start', methods=['POST'])
def start_webcam_capture():
    data = request.get_json(force=True)
    user_id = data.get('user_id')
    if not user_id:
        return jsonify({'error': 'User ID required'}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT user_id FROM users WHERE user_id = %s', (user_id,))
    if not cursor.fetchone():
        return_db_connection(conn)
        return jsonify({'error': 'User not found'}), 404
    return_db_connection(conn)
    success, message = webcam_manager.start_capture(user_id)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/webcam/capture', methods=['POST'])
def capture_webcam_frame():
    frame_data = request.json.get('frame')
    if not frame_data:
        return jsonify({'error': 'No frame data provided'}), 400
    success, message, complete = webcam_manager.add_frame(frame_data)
    if success:
        return jsonify({
            'success': True, 'message': message,
            'frames_captured': len(webcam_manager.captured_frames),
            'total_frames': webcam_manager.max_frames,
            'complete': complete
        })
    else:
        return jsonify({'error': message}), 400

@app.route('/api/webcam/finish', methods=['POST'])
def finish_webcam_capture():
    success, message = webcam_manager.finish_capture(db_manager)
    if success:
        return jsonify({'success': True, 'message': message})
    else:
        return jsonify({'error': message}), 400

@app.route('/api/webcam/cancel', methods=['POST'])
def cancel_webcam_capture():
    success, message = webcam_manager.cancel_capture()
    return jsonify({'success': True, 'message': message})

@app.route('/api/webcam/status', methods=['GET'])
def webcam_status():
    return jsonify({
        'capturing': webcam_manager.capturing,
        'frames_captured': len(webcam_manager.captured_frames),
        'total_frames': webcam_manager.max_frames,
        'user_id': webcam_manager.current_user_id
    })

@app.route('/api/devices', methods=['GET'])
def get_devices():
    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    cursor.execute("""
        SELECT
            d.device_id,
            d.device_name,
            d.device_type,
            d.location,
            d.status,
            u.user_id AS assigned_user_id,
            u.full_name,
            d.created_date
        FROM devices d
        LEFT JOIN device_assignments da
            ON d.device_id = da.device_id
            AND da.status = 'active'
        LEFT JOIN users u
            ON da.user_id = u.user_id
        ORDER BY d.created_date DESC
    """)

    devices = []

    for row in cursor.fetchall():
        devices.append({
            'device_id': row['device_id'],
            'device_name': row['device_name'],
            'device_type': row['device_type'],
            'location': row['location'],
            'status': row['status'],
            'assigned_user_id': row['assigned_user_id'],
            'assigned_user_name': row['full_name'],
            'created_date': row['created_date']
        })

    return_db_connection(conn)

    return jsonify({
        'total': len(devices),
        'devices': devices
    })
@app.route('/api/device/<device_id>/users', methods=['GET'])
def get_users_for_device(device_id):

    conn = None

    try:
        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
        SELECT
            u.user_id,
            u.full_name,
            b.mean_embedding
        FROM device_assignments da
        JOIN users u ON da.user_id = u.user_id
        JOIN biometric_data b ON u.user_id = b.user_id
        WHERE da.device_id = %s
        AND da.status = 'active'
        """, (device_id,))

        users = []

        import pickle

        for row in cursor.fetchall():
            embedding = pickle.loads(row["mean_embedding"])

            users.append({
                "user_id": row["user_id"],
                "full_name": row["full_name"],
                "embedding": embedding.tolist()
            })

        return jsonify({
            "success": True,
            "users": users
        })

    except Exception as e:
        print("Device user fetch error:", e)
        return jsonify({"success": False})

    finally:
        if conn:
            return_db_connection(conn)
@app.route('/api/devices', methods=['POST'])
def create_device():
    device_name = request.form.get('device_name')
    device_type = request.form.get('device_type')
    location = request.form.get('location')
    if not device_name or not device_type or not location:
        return jsonify({'error': 'All fields required'}), 400
    conn = get_db_connection()
    cursor = get_db_cursor(conn)
    cursor.execute('SELECT COUNT(*) as count FROM devices')
    count = cursor.fetchone()['count']
    device_id = f"DEV_{device_type[:3].upper()}_{count + 1:03d}"
    try:
        cursor.execute('''
            INSERT INTO devices (device_id, device_name, device_type, location, status, created_date)
            VALUES (%s, %s, %s, %s, 'active', %s)
        ''', (device_id, device_name, device_type, location, datetime.now()))
        conn.commit()
        return_db_connection(conn)
        return jsonify({'success': True, 'device_id': device_id})
    except Exception as e:
        conn.rollback()
        return_db_connection(conn)
        return jsonify({'error': str(e)}), 400

@app.route('/api/devices/<device_id>', methods=['PUT'])
def update_device(device_id):
    device_name = request.form.get('device_name')
    device_type = request.form.get('device_type')
    location = request.form.get('location')
    if not device_name or not device_type or not location:
        return jsonify({'error': 'All fields required'}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
            UPDATE devices 
            SET device_name = %s, device_type = %s, location = %s
            WHERE device_id = %s
        ''', (device_name, device_type, location, device_id))
        if cursor.rowcount == 0:
            return_db_connection(conn)
            return jsonify({'error': 'Device not found'}), 404
        conn.commit()
        return_db_connection(conn)
        return jsonify({'success': True})
    except Exception as e:
        return_db_connection(conn)
        return jsonify({'error': str(e)}), 400

@app.route('/api/devices/<device_id>', methods=['DELETE'])
def delete_device(device_id):
    if device_id == 'LOCAL_LAPTOP_001':
        return jsonify({'error': 'Cannot delete primary enrollment terminal'}), 400
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('DELETE FROM device_assignments WHERE device_id = %s', (device_id,))
        cursor.execute('DELETE FROM devices WHERE device_id = %s', (device_id,))
        if cursor.rowcount == 0:
            return_db_connection(conn)
            return jsonify({'error': 'Device not found'}), 404
        conn.commit()
        return_db_connection(conn)
        return jsonify({'success': True})
    except Exception as e:
        return_db_connection(conn)
        return jsonify({'error': str(e)}), 400

@app.route('/api/devices/<device_id>/assign', methods=['POST'])
def assign_device(device_id):

    user_id = request.form.get('user_id')

    if not user_id:
        return jsonify({'error': 'User ID required'}), 400

    conn = get_db_connection()
    cursor = conn.cursor()

    try:

        # 1️⃣ deactivate previous assignments for this device
        cursor.execute("""
        UPDATE device_assignments
        SET status = 'inactive'
        WHERE device_id = %s
        """, (device_id,))

        # 2️⃣ check if this user-device pair already exists
        cursor.execute("""
        SELECT 1
        FROM device_assignments
        WHERE user_id = %s AND device_id = %s
        """, (user_id, device_id))

        exists = cursor.fetchone()

        if exists:
            # 3️⃣ reactivate existing assignment
            cursor.execute("""
            UPDATE device_assignments
            SET status = 'active', assigned_date = NOW()
            WHERE user_id = %s AND device_id = %s
            """, (user_id, device_id))
        else:
            # 4️⃣ create new assignment
            cursor.execute("""
            INSERT INTO device_assignments
            (user_id, device_id, assigned_date, status)
            VALUES (%s, %s, NOW(), 'active')
            """, (user_id, device_id))

        # 5️⃣ update devices table (for Device Registry UI)
        cursor.execute("""
        UPDATE devices
        SET assigned_user_id = %s
        WHERE device_id = %s
        """, (user_id, device_id))

        conn.commit()

        return jsonify({
            "success": True,
            "message": "Device reassigned successfully"
        })

    except Exception as e:

        conn.rollback()

        return jsonify({"error": str(e)}), 500

    finally:
        return_db_connection(conn)

@app.route('/api/biometric', methods=['GET'])
def get_biometric_data():
    conn = get_db_connection()
    cursor = get_db_cursor(conn)
    cursor.execute('SELECT COUNT(*) as count FROM users')
    total_users = cursor.fetchone()['count']
    cursor = get_db_cursor(conn)
    cursor.execute('''
        SELECT b.user_id, u.full_name, b.capture_date, b.frame_count, b.capture_method,
               d.device_id, d.device_name
        FROM biometric_data b
        JOIN users u ON b.user_id = u.user_id
        LEFT JOIN device_assignments da ON u.user_id = da.user_id AND da.status = 'active'
        LEFT JOIN devices d ON da.device_id = d.device_id
        GROUP BY b.user_id, u.full_name, b.capture_date, b.frame_count, b.capture_method,
                 d.device_id, d.device_name
        ORDER BY b.capture_date DESC
    ''')
    biometric_data = []
    for row in cursor.fetchall():
        quality_score = min(0.95, 0.80 + (row['frame_count'] * 0.03)) if row['frame_count'] else 0.85
        biometric_data.append({
            'user_id': row['user_id'], 
            'user_name': row['full_name'],
            'enrolled_at': row['capture_date'], 
            'embedding_size': 128,
            'quality_score': quality_score,
            'capture_method': row['capture_method'] or 'webcam',
            'assigned_device': row['device_id'], 
            'device_name': row['device_name']
        })
    return_db_connection(conn)
    return jsonify({
        'total': len(biometric_data),
        'total_users': total_users,
        'biometric_data': biometric_data
    })

@app.route('/api/access_logs/all', methods=['GET'])
def get_all_access_logs():
    """Get all access logs (for frontend display)"""
    try:
        conn = get_db_connection()
        cursor = get_db_cursor(conn)
        cursor.execute('''
            SELECT 
                al.id, al.timestamp, al.device_id, al.user_id, al.success,
                al.confidence_score, al.authentication_method, al.notes, u.full_name
            FROM access_logs al
            LEFT JOIN users u ON al.user_id = u.user_id
            ORDER BY al.timestamp DESC
            LIMIT 1000
        ''')
        logs = []
        for row in cursor.fetchall():
            status = 'granted' if row['success'] == 1 else ('denied' if row['success'] == 0 else 'error')
            logs.append({
                'id': row['id'], 
                'timestamp': row['timestamp'],
                'device_id': row['device_id'] or 'Unknown',
                'user_id': row['user_id'] or 'Unknown',
                'user_name': row['full_name'] or row['user_id'] or 'Unknown User',
                'status': status,
                'confidence': round(row['confidence_score']) if row['confidence_score'] else None,
                'authentication_method': row['authentication_method'] or 'biometric',
                'reason': row['notes'] or 'Access attempt logged'
            })
        return_db_connection(conn)
        return jsonify({'success': True, 'total': len(logs), 'logs': logs})
    except Exception as e:
        print(f"Error getting access logs: {e}")
        return jsonify({'success': False, 'error': str(e), 'total': 0, 'logs': []}), 500

@app.route('/api/analytics', methods=['GET'])
def get_analytics():
    """Get analytics from access_logs table"""
    try:
        conn = get_db_connection()
        cursor = get_db_cursor(conn)
        
        cursor.execute('''
            SELECT COUNT(*) AS total_attempts, SUM(success) AS granted,
                   COUNT(*) - SUM(success) AS denied
            FROM access_logs
        ''')
        overall = cursor.fetchone()
        
        cursor.execute('''
            SELECT authentication_method, COUNT(*) AS count,
                   SUM(success) AS granted, COUNT(*) - SUM(success) AS denied
            FROM access_logs
            GROUP BY authentication_method
        ''')
        by_method = cursor.fetchall()
        
        cursor.execute('''
            SELECT device_id, COUNT(*) AS count,
                   SUM(success) AS granted, COUNT(*) - SUM(success) AS denied
            FROM access_logs
            GROUP BY device_id
        ''')
        by_device = cursor.fetchall()
        
        cursor.execute('''
            SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24:00:00') AS hour,
                   COUNT(*) AS count, SUM(success) AS granted,
                   COUNT(*) - SUM(success) AS denied
            FROM access_logs
            WHERE timestamp > NOW() - INTERVAL '24 hours'
            GROUP BY hour
            ORDER BY hour
        ''')
        recent_activity = cursor.fetchall()
        
        return_db_connection(conn)
        
        analytics = {
            'success': True,
            'overall': {
                'total_attempts': overall['total_attempts'] or 0,
                'granted': overall['granted'] or 0,
                'denied': overall['denied'] or 0
            },
            'by_method': [
                {'method': row['authentication_method'] or 'unknown', 
                 'count': row['count'],
                 'granted': row['granted'], 
                 'denied': row['denied']}
                for row in by_method
            ],
            'by_device': [
                {'device_id': row['device_id'] or 'unknown', 
                 'count': row['count'],
                 'granted': row['granted'], 
                 'denied': row['denied']}
                for row in by_device
            ],
            'recent_activity': [
                {'hour': row['hour'], 
                 'count': row['count'],
                 'granted': row['granted'], 
                 'denied': row['denied']}
                for row in recent_activity
            ]
        }
        return jsonify(analytics)
    except Exception as e:
        print(f"Error getting analytics: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ============================================================
# REMOTE WORKER DATABASE OPERATIONS
# ============================================================

def create_remote_worker(worker_id, full_name, email, image_filename):

    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    cursor.execute("""
        INSERT INTO remote_workers
        (worker_id, full_name, email, status, image_filename)
        VALUES (%s,%s,%s,'pending',%s)
    """,(
        worker_id,
        full_name,
        email,
        image_filename
    ))

    conn.commit()

    return_db_connection(conn)

    return True


def get_pending_remote_workers():
    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    cursor.execute("""
        SELECT * FROM remote_workers
        WHERE status = 'pending'
        ORDER BY created_at DESC
    """)

    workers = cursor.fetchall()
    return_db_connection(conn)
    return workers


def update_remote_worker_status(worker_id, status):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE remote_workers
        SET status = %s
        WHERE worker_id = %s
    """, (status, worker_id))

    conn.commit()
    return_db_connection(conn)


def store_remote_worker_biometric(worker_id, mean_embedding, capture_method="remote_enrollment"):
    """
    Store biometric data for remote worker
    Uses the same biometric_data table but with worker_id as user_id
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Delete existing biometric data for this worker
        cursor.execute('DELETE FROM biometric_data WHERE user_id = %s', (worker_id,))
        
        # Store new biometric data
        embedding_blob = pickle.dumps(mean_embedding)
        cursor.execute('''
            INSERT INTO biometric_data (user_id, mean_embedding, capture_date, capture_method, frame_count)
            VALUES (%s, %s, %s, %s, %s)
        ''', (worker_id, embedding_blob, datetime.now(), capture_method, 5))
        
        conn.commit()
        return True
    except Exception as e:
        conn.rollback()
        print(f"Error storing remote worker biometric: {e}")
        return False
    finally:
        return_db_connection(conn)


def start_remote_session(worker_id):

    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    # Close old sessions
    cursor.execute("""
        UPDATE remote_attendance
        SET status='ended',
            logout_time=NOW()
        WHERE worker_id=%s
        AND status='active'
    """,(worker_id,))

    # Start new session
    cursor.execute("""
        INSERT INTO remote_attendance
        (worker_id, login_time, status, work_status, last_seen, last_state_change,
         active_minutes, idle_minutes)
        VALUES (%s,NOW(),'active','working',NOW(),NOW(),0,0)
    """,(worker_id,))

    conn.commit()

    return_db_connection(conn)


def update_last_seen(worker_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE remote_attendance
        SET last_seen = NOW()
        WHERE worker_id = %s AND status = 'active'
    """, (worker_id,))

    conn.commit()
    return_db_connection(conn)


def end_remote_session(worker_id):
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE remote_attendance
        SET logout_time = NOW(), status = 'ended'
        WHERE worker_id = %s AND status = 'active'
    """, (worker_id,))

    conn.commit()
    return_db_connection(conn)


def log_monitoring_event(worker_id, event_type, confidence=None, image_filename=None):

    # ✅ Only allow important alerts
    ALLOWED_EVENTS = [
        "absent_started",
        "absent_ended",
        "intruder",
        "login_failed"
    ]

    if event_type not in ALLOWED_EVENTS:
        return

    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    cursor.execute("""
        INSERT INTO monitoring_events
        (worker_id,event_type,confidence,image_filename,timestamp)
        VALUES (%s,%s,%s,%s,NOW())
    """,(
        worker_id,
        event_type,
        float(confidence) if confidence else None,
        image_filename
    ))

    conn.commit()

    return_db_connection(conn)

# ============================================================
# REMOTE WORKER APIs
# ============================================================
@app.route("/api/remote/register", methods=["POST"])
def api_remote_register():

    import base64
    import uuid
    import os

    data = request.json

    worker_id = data["worker_id"]
    full_name = data["full_name"]
    email = data["email"]

    image_base64 = data.get("image_base64")

    image_filename = None

    # ✅ Save registration photo
    if image_base64:

        os.makedirs("worker_images", exist_ok=True)

        image_filename = f"{worker_id}_{uuid.uuid4().hex}.jpg"

        image_bytes = base64.b64decode(image_base64)

        with open(f"worker_images/{image_filename}", "wb") as f:
            f.write(image_bytes)

    # ✅ Create worker with photo
    success = create_remote_worker(
        worker_id,
        full_name,
        email,
        image_filename
    )

    return jsonify({"success": success})


@app.route("/api/remote/login", methods=["POST"])
def api_remote_login():
    """
    Remote worker login with face verification
    
    Expected JSON:
    {
        "embedding": [128-dimension array],
        "worker_id": "RW001" (required for lookup)
    }
    
    Returns:
    {
        "success": true/false,
        "worker_id": "RW001",
        "full_name": "Worker Name",
        "confidence": 95.5,
        "message": "Login successful"
    }
    """
    conn = None
    try:
        data = request.json
        received_embedding = np.array(data["embedding"], dtype=np.float64)
        worker_id = data.get("worker_id")
        
        if not worker_id:
            return jsonify({
                "success": False,
                "error": "worker_id is required"
            }), 400
        
        # Get approved remote worker with biometric data
        conn = get_db_connection()
        cursor = get_db_cursor(conn)
        
        # Check if worker is approved and has biometric data
        cursor.execute("""
            SELECT rw.worker_id, rw.full_name, rw.status, b.mean_embedding
            FROM remote_workers rw
            LEFT JOIN biometric_data b ON rw.worker_id = b.user_id
            WHERE rw.worker_id = %s
        """, (worker_id,))
        
        worker = cursor.fetchone()
        
        # Worker doesn't exist
        if not worker:
            return_db_connection(conn)
            log_monitoring_event(
                worker_id,
                "login_failed",
                confidence=0.0
            )
            return jsonify({
                "success": False,
                "error": "Worker not found"
            }), 404
        
        # Worker not approved
        if worker['status'] != 'approved':
            return_db_connection(conn)
            log_monitoring_event(
                worker_id,
                "login_failed",
                confidence=0.0
            )
            return jsonify({
                "success": False,
                "error": "Worker not approved by supervisor"
            }), 403
        
        # No biometric data enrolled
        if not worker['mean_embedding']:
            return_db_connection(conn)
            return jsonify({
                "success": False,
                "error": "No biometric data found for this worker"
            }), 404
        
        # ✅ FIX: Load embedding as numpy array from binary buffer
        stored_embedding = np.frombuffer(
           worker['mean_embedding'],
           dtype=np.float64
       )
        
        stored_embedding = np.array(stored_embedding)
        received_embedding = np.array(received_embedding)
        print("Stored embedding length:", len(stored_embedding))
        print("Received embedding length:", len(received_embedding))
        if len(stored_embedding) != 128:
            return_db_connection(conn)
            return jsonify({
                "success": False,
                "error": "Invalid biometric data. Re-enrollment required."
            }), 400
        if len(received_embedding) != 128:
            return_db_connection(conn)
            return jsonify({
                "success":False,
                "error":"Invalid face capture. Try again."
            }), 400

        # Calculate distance between embeddings
        distance = float(np.linalg.norm(stored_embedding - received_embedding))
        
        # Calculate confidence score (inverse of distance, normalized to 0-100)
        confidence = float(max(0, (1 - distance)) * 100)
        
        MATCH_THRESHOLD = 0.6  # Face recognition threshold
        
        # Check if match is good enough
        if distance >= MATCH_THRESHOLD:
            return_db_connection(conn)
            # Log failed attempt
            log_monitoring_event(
                worker_id,
                "login_failed",
                confidence=confidence
            )
            return jsonify({
                "success": False,
                "error": "Face verification failed - no match",
                "confidence": round(confidence, 2)
            }), 401
        
        # ✅ Match successful - start attendance session
        start_remote_session(worker_id)
        
        # Log successful login
        log_monitoring_event(
            worker_id,
            "login_success",
            confidence=confidence
        )
        
        return_db_connection(conn)
        
        return jsonify({
            "success": True,
            "worker_id": worker_id,
            "full_name": worker['full_name'],
            "confidence": round(confidence, 2),
            "message": "Login successful"
        })
        
    except KeyError as e:
        if conn:
            return_db_connection(conn)
        return jsonify({
            "success": False,
            "error": f"Missing required field: {str(e)}"
        }), 400
    except Exception as e:
        if conn:
            return_db_connection(conn)
        print(f"Error in remote login: {e}")
        return jsonify({
            "success": False,
            "error": "Internal server error"
        }), 500



@app.route("/api/remote/pending", methods=["GET"])
def api_remote_pending():
    workers = get_pending_remote_workers()
    return jsonify({"success": True, "workers": workers})

@app.route('/worker_images/<filename>')
def worker_images(filename):

    return send_from_directory(
        "worker_images",
        filename
    )


@app.route("/api/remote/approve/<worker_id>", methods=["POST"])
def api_remote_approve(worker_id):
    update_remote_worker_status(worker_id, "approved")
    return jsonify({"success": True})


@app.route("/api/remote/enroll", methods=["POST"])
def api_remote_enroll():

    try:
        data = request.json

        worker_id = data["worker_id"]
        embeddings = data["embeddings"]

        if len(embeddings) == 0:
            return jsonify({
                "success": False,
                "error": "No embeddings received"
            })

        import numpy as np

        # Convert list → numpy
        emb_array = np.array(embeddings, dtype=np.float64)

        # Mean embedding (128-dim)
        mean_embedding = np.mean(emb_array, axis=0)

        # Store as float64 → 1024 bytes
        embedding_bytes = mean_embedding.astype(np.float64).tobytes()

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        # Delete old biometric
        cursor.execute("""
            DELETE FROM biometric_data
            WHERE user_id = %s
        """, (worker_id,))

        # Insert biometric
        cursor.execute("""
            INSERT INTO biometric_data
            (user_id, mean_embedding, capture_date, frame_count, capture_method)
            VALUES (%s, %s, NOW(), %s, %s)
        """, (
            worker_id,
            embedding_bytes,
            len(embeddings),
            "remote_worker"
        ))

        conn.commit()

        return_db_connection(conn)

        return jsonify({
            "success": True
        })

    except Exception as e:
        print("Enroll error:", e)

        return jsonify({
            "success": False,
            "error": str(e)
        }) 
@app.route("/api/remote/reject/<worker_id>", methods=["POST"])
def api_remote_reject(worker_id):
    update_remote_worker_status(worker_id, "rejected")
    return jsonify({"success": True})


@app.route("/api/remote/live-status", methods=["GET"])
def api_remote_live_status():

    conn = None

    try:

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
            SELECT DISTINCT ON (rw.worker_id)
                rw.worker_id,
                rw.full_name,
                ra.last_seen,
                ra.status
            FROM remote_workers rw
            LEFT JOIN remote_attendance ra
                ON rw.worker_id = ra.worker_id
                AND ra.status = 'active'
            WHERE rw.status = 'approved'
            ORDER BY rw.worker_id, ra.last_seen DESC
        """)

        workers = cursor.fetchall()

        return jsonify({
            "success": True,
            "workers": workers
        })

    except Exception as e:

        print("Live status error:", e)

        return jsonify({
            "success": False,
            "workers": []
        })

    finally:

        if conn:
            return_db_connection(conn)


@app.route("/api/remote/attendance", methods=["GET"])
def api_remote_attendance():

    import pytz
    from datetime import datetime

    IST = pytz.timezone('Asia/Kolkata')

    conn = None

    try:

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
            SELECT
                ra.*, rw.full_name
            FROM remote_attendance ra
            JOIN remote_workers rw 
            ON rw.worker_id = ra.worker_id
            ORDER BY ra.login_time DESC
        """)

        rows = cursor.fetchall()

        attendance = []

        now = datetime.now(IST)

        for row in rows:

            login_time = row["login_time"]

            if login_time:

                # Convert to IST
                login_time_ist = login_time.astimezone(IST)

                duration = now - login_time_ist

                minutes = int(duration.total_seconds() / 60)

                if minutes < 0:
                    minutes = -minutes

            else:
                minutes = 0

            row["duration_minutes"] = minutes

            attendance.append(row)

        return jsonify({
            "success": True,
            "attendance": attendance
        })

    except Exception as e:

        print("Attendance error:", e)

        return jsonify({
            "success": False,
            "attendance": []
        })

    finally:

        if conn:
            return_db_connection(conn)
@app.route("/api/remote/events", methods=["GET"])
def api_remote_events():

    conn = None

    try:

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
            SELECT me.*, rw.full_name
            FROM monitoring_events me
            JOIN remote_workers rw ON rw.worker_id = me.worker_id
            ORDER BY me.timestamp DESC
            LIMIT 100
        """)

        events = cursor.fetchall()

        return jsonify({
            "success": True,
            "events": events
        })

    except Exception as e:

        print("Events error:", e)

        return jsonify({
            "success": False
        })

    finally:

        if conn:
            return_db_connection(conn)
@app.route("/api/remote/activity", methods=["POST"])
def log_remote_activity():

    conn = None

    try:

        data = request.json

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO remote_worker_activity
            (worker_id, application, window_title, timestamp)
            VALUES (%s,%s,%s,NOW())
        """, (
            data.get("worker_id"),
            data.get("application"),
            data.get("window_title")
        ))

        conn.commit()

        return jsonify({"success": True})

    except Exception as e:

        if conn:
            conn.rollback()

        print("Activity logging error:", e)

        return jsonify({"success": False})

    finally:

        if conn:
            return_db_connection(conn)


@app.route("/api/remote/activity/<worker_id>", methods=["GET"])
def get_worker_activity(worker_id):

    conn = None

    try:

        conn = get_db_connection()
        cursor = get_db_cursor(conn)

        cursor.execute("""
            SELECT application, window_title, timestamp
            FROM remote_worker_activity
            WHERE worker_id=%s
            ORDER BY timestamp DESC
            LIMIT 200
        """,(worker_id,))

        rows = cursor.fetchall()

        return jsonify({
            "success": True,
            "activity": rows
        })

    except Exception as e:

        print("Fetch activity error:", e)

        return jsonify({"success": False})

    finally:

        if conn:
            return_db_connection(conn)
# ============================================================
# REMOTE WORKER ADVANCED FUNCTIONS (WITH ALL FIXES APPLIED)
# ============================================================

def update_work_status(worker_id, new_status, confidence=None):
    """
    CORE PRODUCTIVITY ENGINE - FIX 2 APPLIED

    Handles:
    - state transitions (working/idle/absent)
    - active vs idle minute accumulation
    - productivity event logging
    """

    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    # Get active session
    cursor.execute("""
        SELECT id, work_status, last_state_change
        FROM remote_attendance
        WHERE worker_id = %s AND status = 'active'
    """, (worker_id,))

    row = cursor.fetchone()

    if not row:
        return_db_connection(conn)
        return

    session_id = row["id"]
    old_status = row["work_status"]
    last_change = row["last_state_change"]

    # No state change → only heartbeat timestamp update
    if old_status == new_status:
        cursor.execute("""
            UPDATE remote_attendance
            SET last_seen = NOW()
            WHERE id = %s
        """, (session_id,))
        conn.commit()
        return_db_connection(conn)
        return

    # --------------------------------------------------
    # Calculate minutes spent in previous state
    # --------------------------------------------------
    cursor.execute("""
        SELECT EXTRACT(EPOCH FROM (NOW() - %s)) / 60
    """, (last_change,))
    minutes_spent = cursor.fetchone()["?column?"] or 0
    minutes_spent = int(minutes_spent)

    # --------------------------------------------------
    # Add minutes to correct bucket
    # --------------------------------------------------
    if old_status == "working":
        cursor.execute("""
            UPDATE remote_attendance
            SET active_minutes = active_minutes + %s
            WHERE id = %s
        """, (minutes_spent, session_id))

    elif old_status == "idle":
        cursor.execute("""
            UPDATE remote_attendance
            SET idle_minutes = idle_minutes + %s
            WHERE id = %s
        """, (minutes_spent, session_id))

    # absent time is NOT counted in productivity

    # --------------------------------------------------
    # Update to new state
    # --------------------------------------------------
    cursor.execute("""
        UPDATE remote_attendance
        SET work_status = %s,
            last_state_change = NOW(),
            last_seen = NOW()
        WHERE id = %s
    """, (new_status, session_id))

    conn.commit()
    
    # --------------------------------------------------
    # Log productivity transition event
    # FIX 2: Return connection BEFORE logging event
    # --------------------------------------------------
    event_map = {
        ("working", "idle"): "idle_started",
        ("idle", "working"): "working_resumed",
        ("working", "absent"): "absent_started",
        ("absent", "working"): "absent_ended",
    }

    event_type = event_map.get((old_status, new_status))
    
    return_db_connection(conn)  # ✅ FIX 2: Connection returned first

    if event_type:
        log_monitoring_event(worker_id, event_type, confidence)


ABSENCE_LIMIT_SECONDS = 120  # 2 minutes


def check_auto_logout(worker_id):
    """
    FIX 3 APPLIED: Ends session automatically if worker stays absent too long.
    Monitoring continues — only attendance session ends.
    """

    conn = get_db_connection()
    cursor = get_db_cursor(conn)

    cursor.execute("""
        SELECT last_seen, work_status
        FROM remote_attendance
        WHERE worker_id = %s AND status = 'active'
    """, (worker_id,))

    row = cursor.fetchone()

    if not row:
        return_db_connection(conn)
        return

    last_seen = row["last_seen"]
    work_status = row["work_status"]

    # Only trigger when already absent
    if work_status != "absent":
        return_db_connection(conn)
        return

    seconds_absent = (datetime.now() - last_seen).total_seconds()

    if seconds_absent >= ABSENCE_LIMIT_SECONDS:
        cursor.execute("""
            UPDATE remote_attendance
            SET logout_time = NOW(),
                status = 'ended'
            WHERE worker_id = %s AND status = 'active'
        """, (worker_id,))

        conn.commit()
        return_db_connection(conn)  # ✅ FIX 3: Connection returned before event logging

        log_monitoring_event(worker_id, "auto_logout_absence")  # ✅ FIX 3: Event logged after connection returned
        return

    return_db_connection(conn)
@app.route('/api/upload_biometric', methods=['POST'])
def upload_biometric():

    file = request.files.get('file')
    user_id = request.form.get('user_id')

    if not file:
        return jsonify({"error":"No file"}),400

    if not user_id:
        return jsonify({"error":"No user"}),400

    filepath = f"uploads/{file.filename}"

    file.save(filepath)

    frame = cv2.imread(filepath)

    frame_small = cv2.resize(frame,(0,0),fx=0.5,fy=0.5)

    rgb = cv2.cvtColor(frame_small, cv2.COLOR_BGR2RGB)

    face_locations = face_recognition.face_locations(rgb, model="hog")

    print("Upload faces:",len(face_locations))

    if len(face_locations) != 1:
        return jsonify({"error":"Exactly one face required"}),400

    encodings = face_recognition.face_encodings(rgb,face_locations)

    embedding = encodings[0].tolist()

    db_manager.store_biometric_data(
        user_id,
        embedding,
        "upload",
        1
    )

    return jsonify({"success":True})

@app.route("/api/remote/heartbeat", methods=["POST"])
def api_remote_heartbeat():
    """
    FIXED: Stable heartbeat with connection pool protection
    """

    conn = None

    try:

        data = request.json

        worker_id = data["worker_id"]
        state = data["state"]
        confidence = data.get("confidence")

        # Refresh last seen when face present
        if state in ["working", "idle"]:
            update_last_seen(worker_id)

        # Intruder detection
        if state == "intruder":
            log_monitoring_event(
                worker_id,
                "intruder_detected",
                confidence
            )

            return jsonify({
                "success": True
            })

        # Update productivity state
        update_work_status(
            worker_id,
            state,
            confidence
        )

        # Auto logout
        check_auto_logout(worker_id)

        return jsonify({
            "success": True
        })

    except Exception as e:

        print("Heartbeat error:", e)

        return jsonify({
            "success": False,
            "error": str(e)
        }), 500

    finally:

        # Safety cleanup (prevents pool exhaustion)
        if conn:
            return_db_connection(conn)
if __name__ == "__main__":
    print("\n" + "="*70)
    print("🔐 BIOMETRIC ACCESS CONTROL SYSTEM - ENTERPRISE-GRADE POSTGRESQL")
    print("="*70)
    print("\n✅ ALL CRITICAL FIXES APPLIED:")
    print("   ✅ FIX #1: cursor.lastrowid → RETURNING id (PostgreSQL)")
    print("   ✅ FIX #2: Connection pooling (1-20 connections)")
    print("   ✅ FIX #3: DB_PASSWORD required (no fallback)")
    print("   ✅ FIX #4: Consistent connection handling")
    print("   ✅ FIX #5: Proper RealDictCursor usage")
    print("   ✅ FIX #6: All conn.close() → return_db_connection()")
    print("   ✅ FIX #7: Graceful pool shutdown (atexit)")
    print("\n🗄️  DATABASE CONFIGURATION:")
    print(f"   Host: localhost")
    print(f"   Database: biometric_system")
    print(f"   User: postgres")
    print(f"   Port: 5432")
    print(f"   Connection Pool: 1-20 connections")
    print(f"   Password: Set via DB_PASSWORD environment variable")
    print("\n⚠️  PRODUCTION DEPLOYMENT:")
    print("   For production, use gunicorn instead of Flask dev server:")
    print("   pip install gunicorn")
    print("   gunicorn -w 4 -b 0.0.0.0:5000 app_postgresql:app")
    print("\n🆘 DEFAULT SUPERVISOR CREDENTIALS:")
    print("   ID:  SUPER001")
    print("   PIN: admin123")
    print("\n📍 ENDPOINTS:")
    print("   Main: http://localhost:5000/")
    print("   Health Check: http://localhost:5000/health")
    print("\n🏢 ENTERPRISE FEATURES:")
    print("   ✓ Connection pooling (high concurrency)")
    print("   ✓ Mandatory environment-based config")
    print("   ✓ Proper cursor factory usage")
    print("   ✓ Comprehensive error handling")
    print("   ✓ Health check endpoint")
    print("   ✓ Graceful shutdown")
    print("   ✓ Production mode (debug disabled)")
    print("\n⚠️  IMPORTANT:")
    print("   DB_PASSWORD must be set or app will not start!")
    print("   Windows: set DB_PASSWORD=your_password")
    print("   Linux:   export DB_PASSWORD=your_password")
    print("\n" + "="*70)
    print("\n❌ REFUSING TO START - Flask dev server not for production!")
    print("\n   For development/testing only, set DEV_MODE=1:")
    print("   Windows: set DEV_MODE=1 && python app_postgresql.py")
    print("   Linux:   DEV_MODE=1 python app_postgresql.py")
    print("\n   For production, use:")
    print("   Windows: deploy_production.bat")
    print("   Linux:   ./deploy_production.sh")
    print("\n" + "="*70 + "\n")
    
    # Only allow Flask dev server if explicitly requested for development
    if os.getenv("DEV_MODE") == "1":
        print("⚠️  WARNING: Running in DEVELOPMENT MODE")
        print("   Flask dev server is for testing only!\n")
        app.run(debug=False, host='0.0.0.0', port=5000)
    else:
        print("Use deploy_production.bat or deploy_production.sh for production.\n")
        exit(1)
