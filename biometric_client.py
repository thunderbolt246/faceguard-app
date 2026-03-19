import cv2
import numpy as np
import face_recognition
import time
import os
import sys
import requests
import subprocess
import base64
import ctypes
import threading
import win32api
import win32con
import win32process
import atexit
import signal
from datetime import datetime
from pathlib import Path

# ============================================================
# CONFIG
# ============================================================

DEVICE_ID = "LOCAL_LAPTOP_001"
SERVER_URL = "http://192.168.29.226:5000"
CAMERA_INDEX = 0

MATCH_THRESHOLD = 0.45
EMERGENCY_WAIT_TIME = 300
EMERGENCY_CHECK_INTERVAL = 2
MISMATCH_TOLERANCE = 3

CAPTURE_ATTEMPTS = 3
FRAMES_PER_ATTEMPT = 3
TOTAL_FRAMES = CAPTURE_ATTEMPTS * FRAMES_PER_ATTEMPT

GRACE_PERIOD_SECONDS = 7 * 60

ALERT_DIR = Path("security_alerts")
ALERT_DIR.mkdir(exist_ok=True)

ASSIGNED_USER = None

system_state = "HARD_LOCK"
last_seen_time = None
last_lock_time = 0
hard_lock_engaged = False
# ============================================================
# PROCESS PROTECTION
# ============================================================

def protect_process():

    try:

        handle = win32api.GetCurrentProcess()

        win32process.SetPriorityClass(
            handle,
            win32process.REALTIME_PRIORITY_CLASS
        )

        print("✓ Process protection enabled")

    except Exception as e:

        print("Process protection failed:", e)
# ============================================================
# STATE TRANSITION HELPER
# ============================================================

def set_state(new_state):
    global system_state
    print(f"[STATE] {system_state} → {new_state}")
    system_state = new_state

# ============================================================
# INPUT BLOCKER
# ============================================================

INPUT_BLOCKED = False
input_lock = threading.Lock()

def emergency_unblock():
    """Emergency input unblock - crash-safe, shutdown-safe"""
    try:
        ctypes.windll.user32.BlockInput(False)
        print("🔓 EMERGENCY UNBLOCK")
    except:
        pass

atexit.register(emergency_unblock)
signal.signal(signal.SIGTERM, lambda a, b: emergency_unblock())
signal.signal(signal.SIGINT, lambda a, b: emergency_unblock())

def block_input():
    global INPUT_BLOCKED

    with input_lock:
        if INPUT_BLOCKED:
            return

        try:
            ctypes.windll.user32.BlockInput(True)
            INPUT_BLOCKED = True
            print("🔒 INPUT BLOCKED")
        except Exception as e:
            print("⚠ Failed to block input:", e)

def unblock_input():
    global INPUT_BLOCKED

    with input_lock:
        if not INPUT_BLOCKED:
            return

        try:
            ctypes.windll.user32.BlockInput(False)
            INPUT_BLOCKED = False
            print("🔓 INPUT UNBLOCKED")
        except Exception as e:
            print("⚠ Failed to unblock input:", e)
       
        time.sleep(0.1)

# ============================================================
# SERVER API HELPERS
# ============================================================

def get_assigned_user():
    """Get the user assigned to this device"""
    try:
        users = get_authorized_users()
        return users[0] if users else None
    except Exception as e:
        print(f"⚠ Error getting assigned user: {e}")
        return None

def get_authorized_users():
    try:
        r = requests.get(f"{SERVER_URL}/api/device/{DEVICE_ID}/users", timeout=5)
        if r.status_code == 200:
            return r.json()["users"]
        else:
            print(f"⚠ Server returned status {r.status_code}")
            return []
    except requests.exceptions.RequestException as e:
        print(f"⚠ Network error getting users: {e}")
        return []
    except Exception as e:
        print(f"⚠ Unexpected error getting users: {e}")
        return []

def log_attempt(payload):
    try:
        requests.post(f"{SERVER_URL}/api/log/access", json=payload, timeout=2)
    except Exception as e:
        print(f"⚠ Failed to log attempt: {e}")

def check_emergency_unlock():
    try:
        r = requests.get(
            f"{SERVER_URL}/api/emergency/check/{DEVICE_ID}",
            timeout=2
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("success") and data.get("unlock"):
                return data["unlock"]["target_user_id"] or "EMERGENCY_ACCESS"
    except requests.exceptions.RequestException:
        pass
    except Exception as e:
        print(f"⚠ Emergency check error: {e}")
    return None

# ============================================================
# SECURITY ALERT SYSTEM
# ============================================================

def send_security_alert(frame, confidence, alert_type, expected_user="Unknown"):
    try:
        if frame is None:
            img_base64 = None
        else:
            _, buffer = cv2.imencode(".jpg", frame)
            img_base64 = base64.b64encode(buffer).decode("utf-8")

        payload = {
            "device_id": DEVICE_ID,
            "alert_type": alert_type,
            "expected_user": expected_user,
            "confidence_score": int(confidence * 100),
            "severity": "high",
            "description": "Unauthorized access attempt",
            "image_base64": img_base64
        }

        requests.post(
            f"{SERVER_URL}/api/security_alerts/create",
            json=payload,
            timeout=3
        )
    except Exception as e:
        print(f"⚠ Alert send failed: {e}")

# ============================================================
# SYSTEM LOCK
# ============================================================

def lock_system():
    try:
        if os.name == "nt":
            subprocess.run("rundll32.exe user32.dll,LockWorkStation", shell=True)
        else:
            subprocess.run("loginctl lock-session", shell=True)
    except Exception as e:
        print(f"⚠ Lock system failed: {e}")

# ============================================================
# CAMERA SAFE CAPTURE
# ============================================================

def get_camera():
    try:
        cap = cv2.VideoCapture(CAMERA_INDEX, cv2.CAP_DSHOW)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
       
        if not cap.isOpened():
            print("⚠ Camera failed to open")
            return None
       
        return cap
    except Exception as e:
        print(f"⚠ Camera initialization error: {e}")
        return None

def safe_frame(cap):
    try:
        if cap is None or not cap.isOpened():
            return None
       
        # Flush buffer
        for _ in range(2):
            cap.grab()
       
        ret, frame = cap.read()
        if not ret or frame is None:
            return None

        if frame.dtype != np.uint8:
            frame = frame.astype(np.uint8)

        if len(frame.shape) != 3 or frame.shape[2] != 3:
            return None

        return frame
    except Exception as e:
        print(f"⚠ Frame capture error: {e}")
        return None

# ============================================================
# FULLSCREEN CAMERA PREVIEW
# ============================================================

def show_camera_preview(frame):
    """Show fullscreen camera preview during authentication"""
    try:
        cv2.namedWindow("AUTH_CAMERA", cv2.WINDOW_NORMAL)
        cv2.setWindowProperty("AUTH_CAMERA", cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)
        cv2.setWindowProperty("AUTH_CAMERA", cv2.WND_PROP_TOPMOST, 1)
        cv2.imshow("AUTH_CAMERA", frame)
        cv2.waitKey(1)
    except Exception as e:
        print(f"⚠ Camera preview error: {e}")

def close_camera_preview():
    """Close camera preview window"""
    try:
        cv2.destroyWindow("AUTH_CAMERA")
        cv2.waitKey(1)
        print("📷 Camera preview closed")
    except:
        pass

# ============================================================
# FACE AUTHENTICATION
# ============================================================

def face_authentication():
    global system_state, hard_lock_engaged
   
    print("\n🔐 FACE AUTHENTICATION STARTED")

    if not ASSIGNED_USER:
        print("❌ No user assigned to this device")
        return False, None

    cap = get_camera()
    if cap is None:
        print("❌ Camera initialization failed")
        return False, None

    all_embeddings = []
    last_frame = None

    try:
        for attempt in range(1, CAPTURE_ATTEMPTS + 1):
            print(f"\n📸 Capture attempt {attempt}/{CAPTURE_ATTEMPTS}")
            attempt_embeddings = []
            start_time = time.time()

            while len(attempt_embeddings) < FRAMES_PER_ATTEMPT:
                if time.time() - start_time > 6:
                    print("⚠ Capture timeout, retrying attempt")
                    break

                frame = safe_frame(cap)
                if frame is None:
                    continue

                # SHOW CAMERA PREVIEW
                show_camera_preview(frame)

                try:
                    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                    faces = face_recognition.face_locations(rgb, model="hog")

                    if len(faces) == 1:
                        encodings = face_recognition.face_encodings(rgb, faces)
                        if len(encodings) > 0:
                            enc = encodings[0]
                            attempt_embeddings.append(enc)
                            last_frame = frame.copy()
                            print(f"✓ Frame {len(attempt_embeddings)}/{FRAMES_PER_ATTEMPT}")
                            time.sleep(0.3)
                except Exception as e:
                    print(f"⚠ Face detection error: {e}")
                    continue

            if len(attempt_embeddings) == FRAMES_PER_ATTEMPT:
                all_embeddings.extend(attempt_embeddings)
            else:
                print("❌ Incomplete capture attempt")

        # Release camera
        cap.release()

        if len(all_embeddings) < TOTAL_FRAMES:
            send_security_alert(
                frame=last_frame,
                confidence=0,
                alert_type="FACE_CAPTURE_INCOMPLETE",
                expected_user=ASSIGNED_USER["full_name"]
            )
            log_attempt({
                "user_id": ASSIGNED_USER["user_id"],
                "device_id": DEVICE_ID,
                "method": "face",
                "success": False,
                "confidence": 0,
                "notes": "Face capture incomplete"
            })
            return False, None

        mean_emb = np.mean(all_embeddings, axis=0)
        dist = np.linalg.norm(mean_emb - np.array(ASSIGNED_USER["embedding"]))
       
        confidence = max(0.0, min(1.0, 1 - (dist / MATCH_THRESHOLD)))

        if dist < MATCH_THRESHOLD:
            print(f"\n✅ FACE AUTH SUCCESS: {ASSIGNED_USER['full_name']}")
           
            set_state("AUTHENTICATED")
            hard_lock_engaged = False
           
            log_attempt({
                "user_id": ASSIGNED_USER["user_id"],
                "device_id": DEVICE_ID,
                "method": "face",
                "success": True,
                "confidence": confidence,
                "notes": "Face authentication successful"
            })
            return True, ASSIGNED_USER["user_id"]

        print("\n❌ FACE AUTH FAILED")
        send_security_alert(
            frame=last_frame,
            confidence=confidence,
            alert_type="FACE_AUTH_FAILED",
            expected_user=ASSIGNED_USER["full_name"]
        )
        log_attempt({
            "user_id": ASSIGNED_USER["user_id"],
            "device_id": DEVICE_ID,
            "method": "face",
            "success": False,
            "confidence": confidence,
            "notes": "Face mismatch"
        })
        return False, None
   
    except Exception as e:
        print(f"⚠ Authentication error: {e}")
        try:
            cap.release()
        except:
            pass
        return False, None

# ============================================================
# USER ABSENT HANDLER
# ============================================================

def handle_user_absent(frame):
    global last_seen_time, last_lock_time

    try:
        if system_state == "AUTHENTICATED":
            set_state("TEMP_LOCK")
            last_seen_time = time.time()

            if time.time() - last_lock_time > 10:
                lock_system()
                last_lock_time = time.time()
           
            block_input()

            send_security_alert(
                frame,
                0,
                "USER_LEFT_WORKSTATION",
                ASSIGNED_USER["full_name"]
            )

            log_attempt({
                "user_id": ASSIGNED_USER["user_id"],
                "device_id": DEVICE_ID,
                "method": "presence",
                "success": False,
                "confidence": 0,
                "notes": "User left workstation — grace period started"
            })

            print("\n⏳ USER ABSENT — Grace period started (7 minutes)")

        elif system_state == "TEMP_LOCK":
            elapsed = time.time() - last_seen_time

            if elapsed > GRACE_PERIOD_SECONDS:
                set_state("HARD_LOCK")
               
                global hard_lock_engaged
                hard_lock_engaged = False

                send_security_alert(
                    frame,
                    0,
                    "GRACE_EXPIRED",
                    ASSIGNED_USER["full_name"]
                )

                log_attempt({
                    "user_id": ASSIGNED_USER["user_id"],
                    "device_id": DEVICE_ID,
                    "method": "presence",
                    "success": False,
                    "confidence": 0,
                    "notes": "Grace expired — emergency unlock required"
                })

                print("\n🔒 GRACE EXPIRED — Emergency unlock required")
    except Exception as e:
        print(f"⚠ Handle absent error: {e}")

# ============================================================
# CONTINUOUS MONITORING
# ============================================================

def continuous_monitoring(authorized_user_id):
    global system_state, last_seen_time, last_lock_time, hard_lock_engaged
   
    cap = get_camera()
    if cap is None:
        print("❌ Cannot start monitoring - camera unavailable")
        return

    print("👁 Continuous monitoring active")

    if not ASSIGNED_USER:
        cap.release()
        return

    mismatch_count = 0
    frame_count = 0
    camera_failures = 0

    try:
        while True:
            frame_count += 1
           
            try:
                if not cap.isOpened():
                    camera_failures += 1
                    print("⚠ Camera disconnected")
                    if camera_failures > 5:
                        print("🔒 Camera missing — HARD LOCK")
                        set_state("HARD_LOCK")
                    cap.release()
                    cap = get_camera()
                    time.sleep(3)
                    continue

                if system_state == "HARD_LOCK":
                    global INPUT_BLOCKED
                    INPUT_BLOCKED = False
                    block_input()
                    subprocess.Popen("shutdown /a", shell=True)
                   
                    if not hard_lock_engaged:
                        lock_system()
                        hard_lock_engaged = True
                   
                    uid = check_emergency_unlock()
                    if uid:
                        set_state("AUTHENTICATED")
                        last_seen_time = None
                        hard_lock_engaged = False
                        unblock_input()

                        log_attempt({
                            "user_id": uid,
                            "device_id": DEVICE_ID,
                            "method": "emergency",
                            "success": True,
                            "confidence": None,
                            "notes": "Emergency unlock granted"
                        })

                        print("\n✅ EMERGENCY UNLOCK RECEIVED")

                    time.sleep(2)
                    continue

                frame = safe_frame(cap)
                if frame is None:
                    time.sleep(0.5)
                    continue

                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                faces = face_recognition.face_locations(rgb)

                if len(faces) != 1:
                    handle_user_absent(frame)
                    time.sleep(1)
                    continue

                if len(faces) == 1:
                    # Only encode every 3rd frame
                    if frame_count % 3 == 0:
                        try:
                            encodings = face_recognition.face_encodings(rgb, faces)
                            if len(encodings) == 0:
                                continue
                           
                            enc = encodings[0]
                            dist = np.linalg.norm(enc - np.array(ASSIGNED_USER["embedding"]))
                            confidence = max(0.0, min(1.0, 1 - (dist / MATCH_THRESHOLD)))

                            if system_state == "TEMP_LOCK":
                                if dist < MATCH_THRESHOLD:
                                    set_state("AUTHENTICATED")
                                    last_seen_time = None
                                    mismatch_count = 0
                                    unblock_input()

                                    log_attempt({
                                        "user_id": ASSIGNED_USER["user_id"],
                                        "device_id": DEVICE_ID,
                                        "method": "presence",
                                        "success": True,
                                        "confidence": confidence,
                                        "notes": "User returned within grace period — auto unlock"
                                    })

                                    print("\n✅ USER RETURNED — Auto unlock granted")
                                else:
                                    mismatch_count += 1
                                    if mismatch_count >= MISMATCH_TOLERANCE:
                                        set_state("HARD_LOCK")
                                        hard_lock_engaged = False
                                        mismatch_count = 0
                                       
                                        send_security_alert(
                                            frame,
                                            confidence,
                                            "WRONG_USER_DURING_GRACE",
                                            ASSIGNED_USER["full_name"]
                                        )
                                       
                                        log_attempt({
                                            "user_id": ASSIGNED_USER["user_id"],
                                            "device_id": DEVICE_ID,
                                            "method": "presence",
                                            "success": False,
                                            "confidence": confidence,
                                            "notes": "Wrong user detected during grace period"
                                        })
                                       
                                        print("\n🔒 WRONG USER DETECTED — Hard lock engaged")
                                        continue

                            elif system_state == "AUTHENTICATED":
                                if dist > MATCH_THRESHOLD:
                                    mismatch_count += 1
                                    if mismatch_count >= MISMATCH_TOLERANCE:
                                        log_attempt({
                                            "user_id": authorized_user_id,
                                            "device_id": DEVICE_ID,
                                            "method": "continuous_monitoring",
                                            "success": False,
                                            "confidence": confidence,
                                            "notes": "Post-auth face mismatch"
                                        })
                                       
                                        send_security_alert(
                                            frame,
                                            confidence,
                                            "POST_AUTH_FACE_MISMATCH",
                                            ASSIGNED_USER["full_name"]
                                        )
                                       
                                        set_state("TEMP_LOCK")
                                        last_seen_time = time.time()
                                        mismatch_count = 0
                                       
                                        if time.time() - last_lock_time > 10:
                                            lock_system()
                                            last_lock_time = time.time()
                                       
                                        block_input()
                                       
                                        print("\n⚠️ FACE MISMATCH — Grace period started")
                                        continue
                                else:
                                    mismatch_count = 0
                       
                        except Exception as e:
                            print(f"⚠ Encoding error: {e}")
                            continue

                time.sleep(2)
           
            except Exception as e:
                print(f"⚠ Monitoring loop error: {e}")
                time.sleep(2)
                continue
   
    except KeyboardInterrupt:
        print("\n🛑 Monitoring stopped by user")
    except Exception as e:
        print(f"⚠ Fatal monitoring error: {e}")
    finally:
        try:
            cap.release()
            print("📷 Camera released")
        except:
            pass

# ============================================================
# MAIN - EXACT BOOT SEQUENCE
# ============================================================

def main():
    global ASSIGNED_USER
   
    try:
        print("="*60)
        print("🔐 BIOMETRIC ACCESS CLIENT - BOOT SEQUENCE")
        print("="*60)
        protect_process()

        # STEP 1: Block input immediately
        block_input()
        subprocess.run("taskkill /f /im explorer.exe", shell=True)
        time.sleep(0.1)
        print("\n[1/5] ✓ Input blocked")

        # STEP 2: Get assigned user
        ASSIGNED_USER = get_assigned_user()
        if not ASSIGNED_USER:
            print("\n❌ No user assigned to this device")
            print("⚠ System will remain locked")
            time.sleep(5)
            return
        print(f"[2/5] ✓ User loaded: {ASSIGNED_USER['full_name']}")

        # STEP 3: Show camera preview & perform authentication
        print("[3/5] 📷 Starting camera preview...")
        auth_success, user_id = face_authentication()

        # STEP 4: Close camera preview
        close_camera_preview()
        print("[4/5] ✓ Camera preview closed")

        # STEP 5: Handle authentication result
        if auth_success:
            # SUCCESS PATH
            print("[5/5] ✅ Authentication successful")
            unblock_input()
            print(f"\n✅ ACCESS GRANTED: {user_id}")
            print("🖥️ Desktop unlocked - starting monitoring...\n")
            subprocess.Popen("explorer.exe")
            continuous_monitoring(user_id)
        else:
            # FAILURE PATH
            print("[5/5] ❌ Authentication failed")
            print("\n🔒 LOCKING WORKSTATION")
            lock_system()
            set_state("HARD_LOCK")
            hard_lock_engaged = False
           
            log_attempt({
                "user_id": ASSIGNED_USER["user_id"],
                "device_id": DEVICE_ID,
                "method": "system",
                "success": False,
                "confidence": None,
                "notes": "Authentication failed - awaiting emergency unlock"
            })
           
            print("⏳ Waiting for emergency unlock...\n")
            continuous_monitoring(ASSIGNED_USER["user_id"])
   
    except KeyboardInterrupt:
        print("\n🛑 System stopped by user")
        emergency_unblock()
    except Exception as e:
        print(f"\n⚠ FATAL ERROR: {e}")
        print("🔓 Emergency unblock activated")
        emergency_unblock()
        import traceback
        traceback.print_exc()

if __name__ == "__main__":

    if sys.stdout:
        sys.stdout = open(os.devnull, 'w')

    if sys.stderr:
        sys.stderr = open(os.devnull, 'w')

    main()