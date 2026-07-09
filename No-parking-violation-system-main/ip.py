import cv2
import numpy as np
from ultralytics import YOLO
import easyocr
from pymongo import MongoClient
from datetime import datetime, timedelta
from urllib.parse import quote_plus
import certifi
import os
import time
from twilio.rest import Client
from collections import defaultdict
import re
import requests
from pymongo.errors import NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError
import backoff
from decouple import config

# Add vehicle tracking variables
vehicle_tracks = {}  # Store vehicle tracking data
STATIONARY_THRESHOLD = 5  # Number of frames a vehicle must be stationary
MIN_STATIONARY_TIME = 30  # Minimum seconds to consider as parked
PARKING_ZONE_COORDS = [(100, 100), (500, 100), (500, 400), (100, 400)]  # Define no-parking zone coordinates

def is_in_no_parking_zone(bbox):
    """Check if vehicle is in no-parking zone"""
    x1, y1, x2, y2 = bbox
    center_x = (x1 + x2) // 2
    center_y = (y1 + y2) // 2
    
    # Create polygon from parking zone coordinates
    polygon = np.array(PARKING_ZONE_COORDS, np.int32)
    
    # Check if center point is inside polygon
    return cv2.pointPolygonTest(polygon, (center_x, center_y), False) >= 0

def track_vehicle(plate_number, bbox, frame_number):
    """Track vehicle movement and detect if stationary"""
    if plate_number not in vehicle_tracks:
        vehicle_tracks[plate_number] = {
            'bboxes': [],
            'frame_numbers': [],
            'first_detected': frame_number,
            'last_movement': frame_number,
            'is_stationary': False
        }
    
    # Store current position
    vehicle_tracks[plate_number]['bboxes'].append(bbox)
    vehicle_tracks[plate_number]['frame_numbers'].append(frame_number)
    
    # Keep only last N positions
    if len(vehicle_tracks[plate_number]['bboxes']) > STATIONARY_THRESHOLD:
        vehicle_tracks[plate_number]['bboxes'].pop(0)
        vehicle_tracks[plate_number]['frame_numbers'].pop(0)
    
    # Check if vehicle is stationary
    if len(vehicle_tracks[plate_number]['bboxes']) >= STATIONARY_THRESHOLD:
        # Calculate movement between positions
        movements = []
        for i in range(1, len(vehicle_tracks[plate_number]['bboxes'])):
            prev_bbox = vehicle_tracks[plate_number]['bboxes'][i-1]
            curr_bbox = vehicle_tracks[plate_number]['bboxes'][i]
            movement = np.sqrt((curr_bbox[0] - prev_bbox[0])**2 + (curr_bbox[1] - prev_bbox[1])**2)
            movements.append(movement)
        
        # If all movements are small, vehicle is stationary
        if all(m < 10 for m in movements):  # 10 pixels threshold
            vehicle_tracks[plate_number]['is_stationary'] = True
        else:
            vehicle_tracks[plate_number]['is_stationary'] = False
            vehicle_tracks[plate_number]['last_movement'] = frame_number
    
    return vehicle_tracks[plate_number]

def is_parked(plate_number, current_frame):
    """Check if vehicle is parked based on stationary time"""
    if plate_number not in vehicle_tracks:
        return False
    
    track = vehicle_tracks[plate_number]
    if not track['is_stationary']:
        return False
    
    # Calculate stationary time in seconds (assuming 30 FPS)
    stationary_time = (current_frame - track['last_movement']) / 30.0
    
    return stationary_time >= MIN_STATIONARY_TIME

# MongoDB connection with retry
@backoff.on_exception(backoff.expo, 
                     (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError),
                     max_tries=3)
def connect_to_mongodb():
    """Establish MongoDB connection with retry logic"""
    try:
        print("🔄 Connecting to MongoDB...")
        
        # Get MongoDB URI from environment variables
        connection_string = config('MONGODB_URI')
        
        client = MongoClient(
            connection_string,
            serverSelectionTimeoutMS=30000,  # Increased timeout
            connectTimeoutMS=30000,
            socketTimeoutMS=30000,
            maxPoolSize=1,
            retryWrites=True,
            tlsAllowInvalidCertificates=True
        )
        
        # Test connection
        client.admin.command('ping')
        print("✅ MongoDB connected successfully")
        
        return client
    except Exception as e:
        print(f"❌ MongoDB connection failed: {str(e)}")
        raise

# Initialize MongoDB connection with better error handling
try:
    client = connect_to_mongodb()
    db = client["no_parking_db"]
    plate_collection = db["detected_plates"]
    fastag_collection = db["fastag_accounts"]
    transactions_collection = db["fine_transactions"]
except Exception as e:
    print(f"❌ Fatal error: Could not connect to MongoDB: {str(e)}")
    print("Please check your internet connection and MongoDB credentials")
    exit(1)

# Add function to handle MongoDB operations with retry
@backoff.on_exception(backoff.expo, 
                     (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError),
                     max_tries=3)
def mongodb_operation(operation_type, collection, *args, **kwargs):
    """Execute MongoDB operations with retry logic"""
    try:
        if operation_type == "find_one":
            return collection.find_one(*args, **kwargs)
        elif operation_type == "insert_one":
            return collection.insert_one(*args, **kwargs)
        elif operation_type == "update_one":
            return collection.update_one(*args, **kwargs)
    except Exception as e:
        print(f"❌ MongoDB operation failed: {str(e)}")
        raise

# Update the plate checking code to use retry function
def check_fastag_account(plate_number):
    """Check FASTag account with retry logic"""
    try:
        return mongodb_operation("find_one", fastag_collection, {"plate_number": plate_number})
    except Exception as e:
        print(f"❌ Error checking FASTag account: {str(e)}")
        return None

# Track processed plates and captures
processed_plates = set()  # Track processed plates
plate_captures = defaultdict(list)  # Store captures per vehicle
MAX_CAPTURES = 2  # Maximum captures per plate

# Create directory for captured images
save_dir = "captured_violations"
if not os.path.exists(save_dir):
    os.makedirs(save_dir)

# Create directory for car images
car_images_dir = "car_images"
if not os.path.exists(car_images_dir):
    os.makedirs(car_images_dir)

#Telegram configuration
TELEGRAM_BOT_TOKEN = config('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = config('TELEGRAM_CHAT_ID')

# Function to clean license plate numbers
def clean_plate_number(plate_number):
    """Remove special characters from plate number and standardize format"""
    # Keep only alphanumeric characters
    cleaned = re.sub(r'[^A-Za-z0-9]', '', plate_number)
    # Convert to uppercase
    return cleaned.upper()

def verify_plate_captures(plate_number, captures):
    """Verify if we have two matching captures"""
    print(f"🔄 Verifying plate {plate_number} - Captures: {len(captures)}")
    
    if len(captures) < MAX_CAPTURES:
        print(f"⏳ Capture #{len(captures)} for {plate_number}")
        return False
        
    if len(set(captures)) == 1:  # All captures match
        print(f"✅ Verified plate number: {plate_number}")
        return True
    
    # Clear invalid captures
    captures.clear()
    print("❌ Captures don't match, clearing buffer")
    return False

def send_violation_notification(plate_number, fine_amount, new_balance=None, chat_id=None, 
                             image_info=None, has_fastag=True, payment_required=False, 
                             payment_link=None):
    """Send notification via Telegram bot"""
    try:
        chat_id = TELEGRAM_CHAT_ID
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        due_date = (datetime.now() + timedelta(hours=24)).strftime('%Y-%m-%d %H:%M:%S')
        late_penalty = 100  # Late payment penalty amount
        
        # Get previous unpaid fines from MongoDB
        previous_fines = mongodb_operation("find_one", transactions_collection, 
            {"plate_number": plate_number, "payment_status": "Pending"},
            sort=[("timestamp", -1)]
        )
        
        previous_unpaid = previous_fines.get("fine_amount", 0) if previous_fines else 0
        total_due = fine_amount + previous_unpaid
        
        if has_fastag:
            if not payment_required:  # Sufficient balance
                message = (
                    "🚫 No Parking Violation Notice\n\n"
                    f"🚗 Vehicle: {plate_number}\n"
                    f"💰 Fine Amount: ₹{fine_amount}\n"
                    f"💳 New Balance: ₹{new_balance}\n"
                    f"🕒 Time: {timestamp}"
                )
            else:  # Insufficient balance
                message = (
                    "⚠️ INSUFFICIENT BALANCE - Payment Required\n\n"
                    f"🚗 Vehicle: {plate_number}\n"
                    f"💰 Current Fine: ₹{fine_amount}\n"
                    f"💰 Previous Unpaid: ₹{previous_unpaid}\n"
                    f"💰 Total Due: ₹{total_due}\n"
                    f"⚠️ Payment Due By: {due_date}\n"
                    f"❗️ Late Payment Penalty: ₹{late_penalty}\n\n"
                    "💳 Click here to pay now:\n"
                    f"👉 <a href='{payment_link}'>Pay Fine Online</a> 👈\n\n"
                    "⚠️ Additional penalties apply for late payment"
                )
        else:
            message = (
                "⚠️ WARNING: No Parking Violation\n\n"
                f"🚗 Vehicle: {plate_number}\n"
                f"💰 Current Fine: ₹{fine_amount}\n"
                f"💰 Previous Unpaid: ₹{previous_unpaid}\n"
                f"💰 Total Due: ₹{total_due}\n"
                f"⚠️ Payment Due By: {due_date}\n"
                f"❗️ Late Payment Penalty: ₹{late_penalty}\n\n"
                "💳 Click here to pay now:\n"
                f"👉 <a href='{payment_link}'>Pay Fine Online</a> 👈\n\n"
                "⚠️ Additional penalties apply for late payment\n"
                "💡 Register for FASTag to avoid manual payments"
            )

        # Send text message
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }
        
        print(f"\n📤 Sending notification for plate {plate_number}")
        response = requests.post(url, json=payload)
        
        if response.status_code != 200:
            raise Exception(f"Failed to send message: {response.text}")

        # Send images if available
        if image_info:
            for img_type, path in [('Full frame', image_info.get('frame_path')), 
                                 ('Plate crop', image_info.get('plate_path'))]:
                if path and os.path.exists(path):
                    with open(path, 'rb') as photo:
                        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
                        files = {'photo': photo}
                        data = {
                            'chat_id': TELEGRAM_CHAT_ID,
                            'caption': f"{img_type} - {plate_number}"
                        }
                        response = requests.post(url, files=files, data=data)
                        if response.status_code != 200:
                            print(f"⚠️ Failed to send {img_type}: {response.text}")

        print(f"✅ Notification sent successfully for {plate_number}")
        return True

    except Exception as e:
        print(f"❌ Notification error: {str(e)}")
        return False

def enhance_image(img):
    """Enhanced preprocessing for better OCR accuracy"""
    try:
        # Convert to grayscale
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        
        # Apply bilateral filter to preserve edges while reducing noise
        denoised = cv2.bilateralFilter(gray, 9, 75, 75)
        
        # Enhance contrast using CLAHE
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
        enhanced = clahe.apply(denoised)
        
        # Apply adaptive thresholding
        binary = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        
        # Apply morphological operations to clean the image
        kernel = np.ones((1,1), np.uint8)
        processed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        
        # Additional noise removal
        processed = cv2.medianBlur(processed, 3)
        
        # Sharpen the image
        kernel = np.array([[-1,-1,-1], [-1,9,-1], [-1,-1,-1]])
        processed = cv2.filter2D(processed, -1, kernel)
        
        return processed
    except Exception as e:
        print(f"Error in image preprocessing: {str(e)}")
        return img

def handle_error(error, context=""):
    """Improved error handling with better logging"""
    print(f"\n{'='*50}")
    print(f"❌ Error in {context}")
    print(f"Type: {type(error).__name__}")
    print(f"Details: {str(error)}")
    print('='*50)
    
    # Log error for debugging
    with open("error_log.txt", "a") as f:
        f.write(f"\n[{datetime.now()}] {context}: {str(error)}")
        f.write(f"\nTraceback: {error.__traceback__}")

# 🟢 Load YOLO model with optimized parameters
plate_model = YOLO("license_plate_detector.pt")
plate_model.conf = 0.25  # Lower confidence threshold for detection
plate_model.iou = 0.45  # Lower IoU threshold for better detection

# 🟢 Initialize EasyOCR with optimized parameters
reader = easyocr.Reader(['en'], gpu=False)  # Set gpu=True if you have GPU support

# 🟢 Load Video Source
video_source = "http://192.168.164.186:6677/videofeed?username=&password="

# Set buffer size to minimize delay
cv2.setUseOptimized(True)
cv2.setNumThreads(4)  # Use multiple threads for processing

# Initialize video capture with optimized parameters
cap = cv2.VideoCapture(video_source)

# Set buffer size to minimize delay
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # Minimize buffer size
cap.set(cv2.CAP_PROP_FPS, 30)  # Set desired FPS
cap.set(cv2.CAP_PROP_FRAME_WIDTH, 320)  # Lower resolution
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 240)

# Add stream status check
if not cap.isOpened():
    print("Error: Could not open video stream.")
    exit()

# Print stream information
print(f"Stream FPS: {cap.get(cv2.CAP_PROP_FPS)}")
print(f"Stream Resolution: {cap.get(cv2.CAP_PROP_FRAME_WIDTH)}x{cap.get(cv2.CAP_PROP_FRAME_HEIGHT)}")

# Adjust frame processing parameters for IP camera
frame_skip = 1  # Process every frame
MIN_FRAME_DELAY = 0.03  # Reduced minimum delay
MAX_FRAME_DELAY = 0.05  # Reduced maximum delay

# Add debug mode
DEBUG_MODE = True

def debug_print(message):
    if DEBUG_MODE:
        print(message)

# Get video properties for overlay text
frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

# Font settings for better overlay
font = cv2.FONT_HERSHEY_SIMPLEX
font_scale = 0.7
font_thickness = 2
font_color = (255, 255, 255)  # White
font_bg_color = (0, 0, 255)  # Red

def validate_plate_number(plate_number):
    # Add state-wise validation patterns
    state_patterns = {
        "TN": r"^TN\d{2}[A-Z]{2}\d{4}$",
        "KA": r"^KA\d{2}[A-Z]{2}\d{4}$",
        # Add more states
    }
    return validate_against_patterns(plate_number, state_patterns)

def adaptive_frame_skip(frame_rate, processing_load):
    # Dynamically adjust frame skip based on system load
    if processing_load > 80:
        return frame_rate // 10
    return frame_rate // 20

class ViolationArchive:
    def archive_old_violations(self):
        threshold_date = datetime.now() - timedelta(days=30)
        old_violations = db.violations.find({"timestamp": {"$lt": threshold_date}})
        db.archived_violations.insert_many(old_violations)

class UserDashboard:
    def get_violation_history(self, plate_number):
        return db.violations.find({"plate_number": plate_number})
    
    def submit_appeal(self, violation_id, reason):
        return db.appeals.insert_one({"violation_id": violation_id, "reason": reason})

class SystemMonitor:
    def check_system_health(self):
        metrics = {
            "cpu_usage": get_cpu_usage(),
            "memory_usage": get_memory_usage(),
            "storage_space": get_storage_space(),
            "processing_rate": get_processing_rate()
        }
        alert_if_threshold_exceeded(metrics)

# Add frame rate control
def get_frame_delay(frame_rate):
    """Calculate appropriate delay based on frame rate"""
    target_delay = 1.0 / frame_rate
    return max(MIN_FRAME_DELAY, min(target_delay, MAX_FRAME_DELAY))

# Add stream health monitoring
def check_stream_health():
    """Check if stream is healthy and adjust parameters if needed"""
    global frame_skip, MIN_FRAME_DELAY, MAX_FRAME_DELAY
    
    # Get current FPS
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps > 0:
        # Adjust parameters based on FPS
        if fps < 15:
            frame_skip = 2
            MIN_FRAME_DELAY = 0.03
            MAX_FRAME_DELAY = 0.08
        elif fps < 25:
            frame_skip = 3
            MIN_FRAME_DELAY = 0.05
            MAX_FRAME_DELAY = 0.1
        else:
            frame_skip = 4
            MIN_FRAME_DELAY = 0.05
            MAX_FRAME_DELAY = 0.1

# Initialize frame counter
frame_count = 0

while True:
    ret, frame = cap.read()
    if not ret:
        print("Error reading frame from stream. Retrying...")
        # Try to reconnect
        cap.release()
        cap = cv2.VideoCapture(video_source)
        time.sleep(1)  # Wait before retrying
        continue
        
    frame_count += 1
    
    # Check stream health periodically
    if frame_count % 30 == 0:  # Check every 30 frames
        check_stream_health()
    
    # Get current frame rate
    fps = cap.get(cv2.CAP_PROP_FPS)
    if fps > 0:
        frame_delay = get_frame_delay(fps)
    else:
        frame_delay = MAX_FRAME_DELAY
    
    # Draw no-parking zone
    cv2.polylines(frame, [np.array(PARKING_ZONE_COORDS, np.int32)], True, (0, 0, 255), 2)
    cv2.putText(frame, "NO PARKING ZONE", (100, 90), font, 0.7, (0, 0, 255), 2)
    
    # Process every frame
    if frame_count % frame_skip == 0:
        debug_print(f"Processing frame {frame_count}")
        
        # Detect license plates with optimized parameters
        results = plate_model(frame, conf=0.25, iou=0.45, agnostic_nms=True)

        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0])  # Bounding box coordinates
                conf = float(box.conf[0])  # Get confidence score
                
                # Skip if box is too small or confidence is too low
                if (x2 - x1) < 30 or (y2 - y1) < 10 or conf < 0.25:
                    continue
                    
                # Check if vehicle is in no-parking zone
                if not is_in_no_parking_zone((x1, y1, x2, y2)):
                    continue
                    
                plate_crop = frame[y1:y2, x1:x2]
                
                # Apply enhanced preprocessing for better OCR
                processed_img = enhance_image(plate_crop)
                
                # Store the original crop for display
                display_crop = plate_crop.copy()
                
                # Perform OCR with optimized parameters
                text_results = reader.readtext(
                    processed_img,
                    detail=1,
                    paragraph=False,
                    decoder='beamsearch',
                    beamWidth=10,  # Increased for better accuracy
                    batch_size=1,
                    allowlist='ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789',
                    min_size=6,  # Reduced minimum character size
                    text_threshold=0.1,  # Lower threshold for more detections
                    link_threshold=0.1,  # Lower threshold for character linking
                    width_ths=0.2,  # Reduced width threshold
                    height_ths=0.2  # Reduced height threshold
                )

                # Initialize variables
                max_prob = 0
                detected_parts = []

                for (bbox, text, prob) in text_results:
                    if prob > 0.1:  # Lower confidence threshold
                        # Update max probability
                        max_prob = max(max_prob, prob)
                        # Clean and normalize the text
                        clean_text = text.replace(" ", "").upper()
                        detected_parts.append(clean_text)

                # Combine all parts into one plate number
                plate_text = "".join(detected_parts)
                
                # Clean special characters from plate number
                plate_number = clean_plate_number(plate_text)
                
                if len(plate_number) < 3:  # Reduced minimum length
                    continue
                    
                debug_print(f"Detected Plate: {plate_number} (Confidence: {max_prob:.2f})")
                
                # Track vehicle movement
                track_data = track_vehicle(plate_number, (x1, y1, x2, y2), frame_count)
                
                # Check if vehicle is parked
                if is_parked(plate_number, frame_count):
                    print(f"⚠️ Vehicle {plate_number} is parked in no-parking zone!")
                    
                    # Store capture for verification
                    if len(plate_captures[plate_number]) < MAX_CAPTURES:
                        plate_captures[plate_number].append(plate_number)
                        print(f"📸 Capture #{len(plate_captures[plate_number])} stored")
                        
                        # Capture and save car image
                        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        car_image_path = f"{car_images_dir}/car_{plate_number}_{timestamp}.jpg"
                        
                        # Get a slightly larger region around the plate for the car image
                        margin = 50  # pixels to add around the plate
                        car_x1 = max(0, x1 - margin)
                        car_y1 = max(0, y1 - margin)
                        car_x2 = min(frame.shape[1], x2 + margin)
                        car_y2 = min(frame.shape[0], y2 + margin)
                        
                        car_image = frame[car_y1:car_y2, car_x1:car_x2]
                        cv2.imwrite(car_image_path, car_image)
                        print(f"📸 Car image saved: {car_image_path}")
                    
                    # Verify captures
                    if not verify_plate_captures(plate_number, plate_captures[plate_number]):
                        continue
                    
                    # Save verified images
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_path = f"{save_dir}/plate_{plate_number}_{timestamp}"
                    
                    cv2.imwrite(f"{save_path}_full.jpg", frame)
                    cv2.imwrite(f"{save_path}_crop.jpg", display_crop)
                    cv2.imwrite(f"{save_path}_processed.jpg", processed_img)
                    
                    print(f"✅ Verified plate: {plate_number}")
                    
                    # Create image info dictionary only if files exist
                    image_info = None
                    if os.path.exists(f"{save_path}_full.jpg") and os.path.exists(f"{save_path}_crop.jpg") and os.path.exists(f"{save_path}_processed.jpg"):
                        image_info = {
                            'frame_path': f"{save_path}_full.jpg",
                            'plate_path': f"{save_path}_crop.jpg",
                            'processed_path': f"{save_path}_processed.jpg",
                            'car_path': car_image_path,  # Add car image path
                            'coordinates': (x1, y1, x2, y2)
                        }
                        print(f"✅ Images saved for {plate_number}")
                    else:
                        print(f"❌ Failed to save all images for {plate_number}")
                        continue  # Skip processing if images weren't saved

                    # Only process new plates
                    if plate_number not in processed_plates:
                        # 🔴 Check FASTag account
                        fastag_data = check_fastag_account(plate_number)
                        
                        # Determine if FASTag exists
                        has_fastag = fastag_data is not None
                        
                        # 🟢 Save to MongoDB with additional details
                        mongodb_operation("insert_one", plate_collection, {
                            "number_plate": plate_number,
                            "vehicle_type": fastag_data.get("vehicle_type", "Unknown") if fastag_data else "Unknown",
                            "owner_name": fastag_data.get("owner_name", "Unknown") if fastag_data else "Unknown",
                            "phone_number": fastag_data.get("phone_number", "Unknown") if fastag_data else "Unknown",
                            "fastag_id": fastag_data.get("fastag_id", "Unknown") if fastag_data else "Unknown",
                            "has_fastag": has_fastag,
                            "balance": fastag_data.get("balance", 0.0) if fastag_data else 0.0,
                            "fine_amount": fine_amount,
                            "last_detected": datetime.now().isoformat() + "Z",
                            "confidence": max_prob,
                            "image_paths": {
                                "full_frame": f"{save_path}_full.jpg",
                                "plate_crop": f"{save_path}_crop.jpg",
                                "processed": f"{save_path}_processed.jpg"
                            }
                        })

                        phone_number = None
                        
                        if has_fastag:
                            # Process with FASTag
                            balance = fastag_data["balance"]
                            phone_number = fastag_data.get("phone_number", "")
                            
                            if balance >= fine_amount:
                                # Deduct fine amount and process as before
                                new_balance = balance - fine_amount
                                mongodb_operation("update_one", fastag_collection,
                                    {"plate_number": plate_number},
                                    {"$set": {"balance": new_balance}}
                                )
                                
                                # Record successful transaction
                                mongodb_operation("insert_one", transactions_collection, {
                                    "plate_number": plate_number,
                                    "fine_amount": fine_amount,
                                    "timestamp": datetime.now(),
                                    "previous_balance": balance,
                                    "new_balance": new_balance,
                                    "payment_status": "Paid",
                                    "payment_method": "FASTag",
                                    "image_paths": {
                                        "full_frame": f"{save_path}_full.jpg",
                                        "plate_crop": f"{save_path}_crop.jpg",
                                        "processed": f"{save_path}_processed.jpg"
                                    }
                                })

                                print(f"✅ Fine of ₹{fine_amount} deducted from {plate_number}. New balance: ₹{new_balance}")
                                    
                            else:
                                # Insufficient balance case
                                print(f"❌ Insufficient FASTag balance for {plate_number} (₹{balance})")
                                payment_link = "https://noparking--detection.up.railway.app/"
                                
                                # Record pending transaction
                                mongodb_operation("insert_one", transactions_collection, {
                                    "plate_number": plate_number,
                                    "fine_amount": fine_amount,
                                    "timestamp": datetime.now(),
                                    "previous_balance": balance,
                                    "new_balance": balance,  # No change
                                    "payment_status": "Pending",
                                    "payment_method": "Online Payment Required",
                                    "payment_link": payment_link,
                                    "payment_due": datetime.now() + timedelta(hours=24),
                                    "due_date": datetime.now() + timedelta(hours=24),
                                    "image_paths": {
                                        "full_frame": f"{save_path}_full.jpg",
                                        "plate_crop": f"{save_path}_crop.jpg",
                                        "processed": f"{save_path}_processed.jpg"
                                    }
                                })
                                
                                # Send insufficient balance notification with payment link
                                send_violation_notification(
                                    plate_number=plate_number,
                                    fine_amount=fine_amount,
                                    new_balance=balance,
                                    chat_id=phone_number,
                                    image_info=image_info,
                                    has_fastag=True,
                                    payment_required=True,
                                    payment_link=payment_link
                                )
                        else:
                            # No FASTag found, send warning notification
                            print(f"❌ No FASTag account found for {plate_number}")
                            
                            # Check if we can find a phone number in our database for this vehicle
                            vehicle_data = mongodb_operation("find_one", plate_collection, {"number_plate": plate_number})
                            if vehicle_data and "phone_number" in vehicle_data and vehicle_data["phone_number"] != "Unknown":
                                phone_number = vehicle_data["phone_number"]
                            
                            # Record violation without FASTag
                            mongodb_operation("insert_one", transactions_collection, {
                                "plate_number": plate_number,
                                "fine_amount": fine_amount,
                                "timestamp": datetime.now(),
                                "payment_method": "No FASTag",
                                "payment_status": "Pending",
                                "payment_due": datetime.now() + timedelta(hours=24),
                                "due_date": datetime.now() + timedelta(hours=24),
                                "image_paths": {
                                    "full_frame": f"{save_path}_full.jpg",
                                    "plate_crop": f"{save_path}_crop.jpg",
                                    "processed": f"{save_path}_processed.jpg"
                                }
                            })
                            
                            # Send warning notification only if images exist
                            send_violation_notification(
                                plate_number=plate_number,
                                fine_amount=fine_amount,
                                chat_id=phone_number,
                                image_info=image_info,
                                has_fastag=False
                            )
                        
                        # Mark as processed
                        processed_plates.add(plate_number)

                # Show plate number and confidence on display
                status_text = f"{plate_number} ({max_prob:.2f})"
                
                # Calculate text background
                text_size = cv2.getTextSize(status_text, font, font_scale, font_thickness)[0]
                
                # Draw text background
                cv2.rectangle(frame, 
                             (x1, y1 - text_size[1] - 10),
                             (x1 + text_size[0] + 10, y1),
                             (0, 0, 255), -1)
                             
                # Draw text
                cv2.putText(frame, status_text, (x1 + 5, y1 - 5), 
                            font, font_scale, (255, 255, 255), font_thickness)
                
                # Draw bounding box
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
                
                # Show processed image in small window for debugging
                resized_processed = cv2.resize(processed_img, (200, 100))
                frame[10:110, 10:210] = cv2.cvtColor(resized_processed, cv2.COLOR_GRAY2BGR)
                
                # Add "Processed" label
                cv2.putText(frame, "Processed", (10, 130), 
                            font, 0.5, (255, 255, 255), 1)

    # Add frame rate information to display
    cv2.putText(frame, f"FPS: {fps:.1f}", (10, 60), font, 0.7, (255, 255, 255), 2)
    cv2.putText(frame, f"Frame: {frame_count}", (10, 90), font, 0.7, (255, 255, 255), 2)
    
    # Add system status overlay at the bottom
    status_bg = np.zeros((80, frame_width, 3), dtype=np.uint8)
    status_bg[:, :] = (0, 0, 0)  # Black background
    
    # Add status text
    cv2.putText(status_bg, f"No Parking Violation Detection | Processed: {len(processed_plates)} vehicles", 
                (10, 30), font, 0.7, (255, 255, 255), 1)
                
    cv2.putText(status_bg, f"Press 'q' to quit | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", 
                (10, 60), font, 0.7, (255, 255, 255), 1)
    
    # Combine with main frame
    frame[-80:, :] = status_bg
    
    # Display video feed
    cv2.imshow("No Parking Violation Detection", frame)
    
    # Add controlled delay
    key = cv2.waitKey(int(frame_delay * 1000)) & 0xFF
    if key == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()