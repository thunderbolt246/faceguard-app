import sys
import subprocess

def install(package):
    subprocess.check_call([sys.executable, "-m", "pip", "install", package])

try:
    import psutil
except ImportError:
    install("psutil")
    import psutil

try:
    import win32gui
    import win32process
except ImportError:
    install("pywin32")
    import win32gui
    import win32process

import cv2
import numpy as np
import face_recognition
import requests
import threading
import time
import base64
import subprocess
import webbrowser
from datetime import datetime
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton,
    QVBoxLayout, QLabel, QLineEdit, QMessageBox
)
from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QFont
from pynput import keyboard, mouse

SERVER_URL = "http://localhost:5000"

HEARTBEAT_INTERVAL = 3  # seconds
IDLE_THRESHOLD = 20     # seconds


def get_active_application():
    try:
        window = win32gui.GetForegroundWindow()
        title = win32gui.GetWindowText(window)
        _, pid = win32process.GetWindowThreadProcessId(window)
        process = psutil.Process(pid)
        return process.name(), title
    except:
        return "unknown", "unknown"


class RemoteWorkerApp(QWidget):
    def __init__(self):
        super().__init__()

        self.worker_id = None
        self.logged_in = False
        self.current_state = "absent"
        self.last_activity_time = time.time()
        self.last_intruder_alert = 0 
        self.login_encoding = None # Cooldown for intruder alerts
        self.cap = None  # ✅ FIX: Camera initialized lazily, not at startup
        self.camera_in_use = False  # ✅ FIX 1: Prevent camera conflicts during enrollment

        self.init_ui()

        # Heartbeat timer
        self.timer = QTimer()
        self.timer.timeout.connect(self.send_heartbeat)
        self.timer.start(HEARTBEAT_INTERVAL * 1000)

        # Start activity monitoring
        self.start_activity_listener()

        # Start monitoring thread
        threading.Thread(target=self.monitor_loop, daemon=True).start()
    def open_chrome(self):
        try:
            # Option 1: Open a specific website
            webbrowser.open("https://www.google.com")

            # Option 2: Force open Google Chrome (Windows)
            # subprocess.Popen(["C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"])
            
        except Exception as e:
            print("Failed to open Chrome:", e)
    # ============================================================
    # UI Setup
    # ============================================================

    def init_ui(self):
        self.setWindowTitle("🧑‍💻 Remote Worker Desktop")
        self.setStyleSheet("""
            QWidget {
                background-color: #f0f2f5;
            }
            QLabel {
                font-size: 12px;
                color: #374151;
            }
            QLineEdit {
                padding: 10px;
                border: 2px solid #d1d5db;
                border-radius: 6px;
                font-size: 13px;
                background: white;
            }
            QLineEdit:focus {
                border-color: #2563eb;
            }
            QPushButton {
                padding: 12px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #2c4a9e, stop:1 #3d5caf);
                color: white;
                border: none;
                border-radius: 6px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                                            stop:0 #1e3a8a, stop:1 #2c4a9e);
            }
            QPushButton:pressed {
                background: #1e3a8a;
            }
            QPushButton:disabled {
                background: #9ca3af;
            }
        """)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(20, 20, 20, 20)

        # Title
        title = QLabel("Remote Worker Login")
        title.setStyleSheet("font-size: 20px; font-weight: bold; color: #111827; margin-bottom: 10px;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        # Worker ID input
        self.worker_input = QLineEdit()
        self.worker_input.setPlaceholderText("Worker ID (e.g., RW001)")
        layout.addWidget(self.worker_input)

        # Full Name input
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("Full Name")
        layout.addWidget(self.name_input)

        # Email input
        self.email_input = QLineEdit()
        self.email_input.setPlaceholderText("Email Address")
        layout.addWidget(self.email_input)

        # Register button
        self.register_button = QPushButton("📝 Register New Worker")
        self.register_button.clicked.connect(self.register_worker)
        layout.addWidget(self.register_button)

        # Separator
        separator = QLabel("─" * 40)
        separator.setAlignment(Qt.AlignCenter)
        separator.setStyleSheet("color: #d1d5db; margin: 10px 0;")
        layout.addWidget(separator)

        # Login button
        self.login_button = QPushButton("🔐 Login with Face Recognition")
        self.login_button.clicked.connect(self.face_login)
        layout.addWidget(self.login_button)

        # Status label
        self.status_label = QLabel("Status: Not logged in")
        self.status_label.setStyleSheet("""
            padding: 12px;
            background: white;
            border: 2px solid #e5e7eb;
            border-radius: 6px;
            font-size: 13px;
            font-weight: bold;
            color: #6b7280;
        """)
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

        # Info label
        info = QLabel("📹 Camera must be enabled\n⚠️ Stay visible during work session")
        info.setStyleSheet("font-size: 11px; color: #9ca3af; margin-top: 10px;")
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        self.setLayout(layout)
        self.resize(400, 450)

    # ============================================================
    # Camera Management (PRODUCTION-GRADE)
    # ============================================================

    def open_camera(self):
        """
        Lazy camera initialization - only opens when needed
        ✅ FIX: Camera opens on-demand, not at startup
        """
        # Camera already open and working
        if hasattr(self, "cap") and self.cap and self.cap.isOpened():
            return True

        # Try to open camera with DirectShow backend (Windows optimization)
        try:
            self.cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            
            if not self.cap.isOpened():
                # Fallback: try default backend
                self.cap = cv2.VideoCapture(0)
            
            if not self.cap.isOpened():
                QMessageBox.critical(
                    self,
                    "Camera Error",
                    "Could not access webcam.\n\n"
                    "Possible causes:\n"
                    "• Camera is being used by another application\n"
                    "• Camera permissions not granted\n"
                    "• No camera detected\n\n"
                    "Close other applications and try again."
                )
                return False
            
            # Set camera properties for better performance
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.cap.set(cv2.CAP_PROP_FPS, 30)
            
            return True
            
        except Exception as e:
            QMessageBox.critical(
                self,
                "Camera Error",
                f"Failed to initialize camera: {str(e)}"
            )
            return False

    # ============================================================
    # Registration Flow
    # ============================================================

    def register_worker(self):
        """Register new worker with face enrollment"""
        # ✅ FIX: Open camera on-demand
        if not self.open_camera():
            return
        
        worker_id = self.worker_input.text().strip()
        full_name = self.name_input.text().strip()
        email = self.email_input.text().strip()

        # Validation
        if not worker_id or not full_name or not email:
            self.status_label.setText("❌ Please fill all fields")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
            return

        self.register_button.setEnabled(False)
        self.status_label.setText("📡 Registering worker...")
        self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#dc2626", "#6b7280"))
        # ✅ Capture registration photo for supervisor approval
        ret, frame = self.cap.read()

        if not ret:
         self.status_label.setText("❌ Camera capture failed")
         self.register_button.setEnabled(True)
         return

        _, buffer = cv2.imencode('.jpg', frame)

        img_base64 = base64.b64encode(buffer).decode('utf-8')
        try:
            # Step 1: Register basic details
            register_response = requests.post(
                f"{SERVER_URL}/api/remote/register",
                json={
                    "worker_id": worker_id,
                    "full_name": full_name,
                    "email": email,
                    "image_base64": img_base64
                },
                timeout=10
            )

            if not register_response.json().get("success"):
                self.status_label.setText("❌ Registration failed (ID may exist)")
                self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
                self.register_button.setEnabled(True)
                return

            # ✅ FIX 1: Updated message - enrollment happens after approval
            self.status_label.setText(
                "✅ Registered successfully!\n"
                "Supervisor must approve before you can login.\n"
                "After approval, click Login to enroll biometrics."
            )
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#dc2626", "#059669"))
            
            # Clear input fields
            self.worker_input.clear()
            self.name_input.clear()
            self.email_input.clear()

        except requests.exceptions.RequestException as e:
            self.status_label.setText(f"❌ Connection error: {str(e)}")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
        except Exception as e:
            self.status_label.setText(f"❌ Error: {str(e)}")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
        finally:
            self.register_button.setEnabled(True)

    # ============================================================
    # Face Login
    # ============================================================

    def face_login(self):
        """Login using face recognition"""
        # ✅ FIX 1: Lock camera for enrollment/login
        self.camera_in_use = True
        
        # ✅ FIX: Open camera on-demand
        if not self.open_camera():
            self.camera_in_use = False
            return
        
        worker_id = self.worker_input.text().strip()
        if not worker_id:
            self.status_label.setText("❌ Enter Worker ID first")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#059669", "#dc2626"))
            self.camera_in_use = False
            return

        self.login_button.setEnabled(False)
        self.status_label.setText("📷 Capturing face...")

        try:
            # CHANGE 4: Show camera preview during login
            cv2.namedWindow("🔐 Login Camera - Look at camera", cv2.WINDOW_NORMAL)
            cv2.resizeWindow("🔐 Login Camera - Look at camera", 640, 480)
            
            # Capture face with preview
            ret, frame = self.cap.read()
            if not ret:
                cv2.destroyAllWindows()
                self.status_label.setText("❌ Camera error")
                self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
                self.login_button.setEnabled(True)
                self.camera_in_use = False
                return

            # Show preview frame
            display_frame = frame.copy()
            cv2.putText(display_frame, "Verifying identity...", 
                       (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 2)
            cv2.imshow("🔐 Login Camera - Look at camera", display_frame)
            cv2.waitKey(500)  # Show preview briefly

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb)

            if len(locations) != 1:
                cv2.destroyAllWindows()
                self.status_label.setText("❌ Face not detected properly")
                self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
                self.login_button.setEnabled(True)
                self.camera_in_use = False
                return

            encoding = face_recognition.face_encodings(rgb, locations)[0]
            self.login_encoding = np.array(encoding)
            
            # Close preview window before server call
            cv2.destroyAllWindows()

            # Send to server for verification
            self.status_label.setText("🔐 Verifying identity...")
            response = requests.post(
                f"{SERVER_URL}/api/remote/login",
                json={
                    "worker_id": worker_id,
                    "embedding": encoding.tolist()
                },
                timeout=10
            )

            data = response.json()

            # ✅ FIX 2: Improved enrollment detection - only trigger for biometric issues
            error_text = str(data.get("error", "")).lower()
            
            if (not data.get("success") and 
                ("biometric" in error_text or 
                 "no approved workers with biometric" in error_text)):
                cv2.destroyAllWindows()
                self.status_label.setText("📷 First-time setup: capturing biometrics...")
                QApplication.processEvents()
                
                embeddings = []
                
                # Create enrollment preview window
                cv2.namedWindow("📷 First-Time Enrollment - Position your face", cv2.WINDOW_NORMAL)
                cv2.resizeWindow("📷 First-Time Enrollment - Position your face", 640, 480)
                
                for i in range(5):
                    attempts = 0
                    while attempts < 10:
                        ret, frame = self.cap.read()
                        if not ret:
                            time.sleep(0.5)
                            attempts += 1
                            continue
                        
                        # Show preview with progress
                        display_frame = frame.copy()
                        cv2.putText(display_frame, f"Frame {i+1}/5 - Hold still...", 
                                   (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
                        cv2.imshow("📷 First-Time Enrollment - Position your face", display_frame)
                        cv2.waitKey(1)

                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        faces = face_recognition.face_locations(rgb)

                        if len(faces) == 1:
                            enc = face_recognition.face_encodings(rgb, faces)[0]
                            embeddings.append(enc.tolist())
                            
                            # Success flash
                            cv2.putText(display_frame, "✓ CAPTURED!", 
                                       (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 3)
                            cv2.imshow("📷 First-Time Enrollment - Position your face", display_frame)
                            cv2.waitKey(500)
                            print(f"✓ Enrollment frame {i+1}/5 captured")
                            break
                        
                        time.sleep(0.5)
                        attempts += 1
                    
                    if len(embeddings) != i + 1:
                        cv2.destroyAllWindows()
                        self.status_label.setText("❌ Face detection failed during enrollment")
                        self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
                        self.login_button.setEnabled(True)
                        self.camera_in_use = False
                        return
                    
                    time.sleep(1)
                
                cv2.destroyAllWindows()
                
                # Send enrollment data
                self.status_label.setText("📤 Uploading biometric data...")
                QApplication.processEvents()
                
                enroll_response = requests.post(
                    f"{SERVER_URL}/api/remote/enroll",
                    json={
                        "worker_id": worker_id,
                        "embeddings": embeddings
                    },
                    timeout=10
                )

                if enroll_response.json().get("success"):
                    self.status_label.setText("✅ Enrollment complete! Click Login again to continue.")
                    self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#dc2626", "#059669"))
                    self.login_button.setEnabled(True)
                    self.camera_in_use = False
                    return
                else:
                    error_msg = enroll_response.json().get("error", "Unknown error")
                    self.status_label.setText(f"❌ Enrollment failed: {error_msg}")
                    self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
                    self.login_button.setEnabled(True)
                    self.camera_in_use = False
                    return

            # Normal login flow
            if data.get("success"):
                self.worker_id = data["worker_id"]
                self.logged_in = True

                threading.Thread(
                    target=self.track_application_usage,
                    daemon=True
                ).start()

                self.open_chrome()
                # ✅ FIX 3: Initialize monitoring state immediately after login
                self.current_state = "working"
                self.last_activity_time = time.time()
                confidence = data.get("confidence", 0)
                
                self.status_label.setText(f"✅ Login successful! ({confidence:.1f}% match)")
                self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#dc2626", "#059669"))
                
                # Disable inputs after login
                self.worker_input.setEnabled(False)
                self.name_input.setEnabled(False)
                self.email_input.setEnabled(False)
                self.register_button.setEnabled(False)
                self.login_button.setText("🟢 Logged In")
            else:
                error = data.get("error", "Unknown error")
                self.status_label.setText(f"❌ Login failed: {error}")
                self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#059669", "#dc2626"))
                self.login_button.setEnabled(True)

        except requests.exceptions.RequestException as e:
            self.status_label.setText(f"❌ Connection error: {str(e)}")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
            self.login_button.setEnabled(True)
        except Exception as e:
            self.status_label.setText(f"❌ Error: {str(e)}")
            self.status_label.setStyleSheet(self.status_label.styleSheet().replace("#6b7280", "#dc2626"))
            self.login_button.setEnabled(True)
        finally:
            # ✅ FIX 1: Always release camera lock
            self.camera_in_use = False

    def track_application_usage(self):
        while True:
            if not self.logged_in:
                time.sleep(5)
                continue

            try:
                app_name, window_title = get_active_application()

                requests.post(
                    f"{SERVER_URL}/api/remote/activity",
                    json={
                        "worker_id": self.worker_id,
                        "application": app_name,
                        "window_title": window_title
                    },
                    timeout=3
                )

            except Exception as e:
                print("Activity tracking error:", e)

            time.sleep(5)

    # ============================================================
    # Activity Detection
    # ============================================================

    def start_activity_listener(self):
        """Monitor keyboard and mouse activity"""
        def on_activity(*args):
            self.last_activity_time = time.time()

        # Start listeners in non-blocking mode
        keyboard.Listener(on_press=on_activity).start()
        mouse.Listener(on_move=on_activity, on_click=on_activity, on_scroll=on_activity).start()

    # ============================================================
    # Monitoring Loop with Intruder Detection
    # ============================================================

    def monitor_loop(self):
        """Continuous face monitoring with state detection"""
        while True:
            # ✅ FIX 1: Skip monitoring if camera is locked for enrollment/login
            if not self.logged_in or self.camera_in_use:
                time.sleep(1)
                continue

            # ✅ FIX: Ensure camera is open before monitoring
            if not self.open_camera():
                time.sleep(5)  # Wait longer before retry if camera fails
                continue

            try:
                ret, frame = self.cap.read()
                if not ret:
                    time.sleep(1)
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                locations = face_recognition.face_locations(rgb)

                # No face detected → absent
                if len(locations) == 0:
                    self.current_state = "absent"

                # Multiple faces detected → INTRUDER ALERT
                elif len(locations) > 1:
                    self.current_state = "intruder"
                    
                    # Rate limit intruder alerts (max 1 per 30 seconds)
                    current_time = time.time()
                    if current_time - self.last_intruder_alert > 30:
                        self.capture_and_send_intruder_alert(frame)
                        self.last_intruder_alert = current_time

                else:
                    encoding = face_recognition.face_encodings(rgb, locations)[0]
                    if hasattr(self, "login_encoding"):
                        distance = np.linalg.norm(self.login_encoding - encoding)
                        if distance > 0.6:
                            print("🚨 Unauthorized person detected")
                            self.current_state = "intruder"
                            current_time = time.time()
                            if current_time - self.last_intruder_alert > 30:
                                self.capture_and_send_intruder_alert(frame)
                                self.last_intruder_alert = current_time
                            time.sleep(2)
                            continue
                    # Single face detected - check if idle
                    idle_time = time.time() - self.last_activity_time

                    if idle_time > IDLE_THRESHOLD:
                        self.current_state = "idle"
                    else:
                        self.current_state = "working"

            except Exception as e:
                print(f"Monitor loop error: {e}")

            time.sleep(1)

    def capture_and_send_intruder_alert(self, frame):
        """Capture screenshot and send intruder alert to server"""
        try:
            # Encode frame to JPEG
            _, buffer = cv2.imencode('.jpg', frame)
            img_base64 = base64.b64encode(buffer).decode('utf-8')

            # Send alert to server
            requests.post(
                f"{SERVER_URL}/api/security_alerts/create",
                json={
                    "alert_type": "remote_intruder",
                    "device_id": f"REMOTE_{self.worker_id}",
                    "image_base64": img_base64,
                    "description": f"Multiple faces detected - Worker {self.worker_id}",
                    "severity": "high",
                    "confidence_score": 95.0
                },
                timeout=5
            )
            print(f"Intruder alert sent for worker {self.worker_id}")

        except Exception as e:
            print(f"Failed to send intruder alert: {e}")

    # ============================================================
    # Heartbeat Sender
    # ============================================================

    def send_heartbeat(self):
        """Send periodic heartbeat with current state"""
        if not self.logged_in:
            return

        try:
            requests.post(
                f"{SERVER_URL}/api/remote/heartbeat",
                json={
                    "worker_id": self.worker_id,
                    "state": self.current_state,
                    "confidence": 95.0
                },
                timeout=2
            )
        except Exception:
            # ✅ FIX: Log server failures so monitoring issues are visible
            print("⚠️ Server unreachable — monitoring offline")

    # ============================================================
    # Cleanup
    # ============================================================

    def closeEvent(self, event):
        """Clean up resources on app close - PRODUCTION-GRADE"""
        # ✅ FIX 3: End attendance session on close
        try:
            if self.logged_in and self.worker_id:
                requests.post(
                    f"{SERVER_URL}/api/remote/logout",
                    json={"worker_id": self.worker_id},
                    timeout=3
                )
        except:
            pass
        
        try:
            if hasattr(self, "cap") and self.cap:
                if self.cap.isOpened():
                    self.cap.release()
                self.cap = None
        except Exception as e:
            print(f"Camera cleanup error: {e}")
        
        # Destroy all OpenCV windows if any
        try:
            cv2.destroyAllWindows()
        except:
            pass
        
        event.accept()


# ============================================================
# Main Entry Point
# ============================================================

if __name__ == "__main__":
    app = QApplication(sys.argv)
    
    # Set application-wide font
    font = QFont("Segoe UI", 10)
    app.setFont(font)
    
    window = RemoteWorkerApp()
    window.show()

    sys.exit(app.exec_())