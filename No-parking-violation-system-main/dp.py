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
#from twilio.rest import Client
from collections import defaultdict
import re
import requests
from pymongo.errors import NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError
import backoff
import json

# Get the directory where this script is located
script_dir = os.path.dirname(os.path.abspath(__file__))

# MongoDB connection with retry
@backoff.on_exception(backoff.expo, 
                     (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError),
                     max_tries=3)
def connect_to_mongodb():
    """Establish MongoDB connection with retry logic"""
    try:
        print("🔄 Connecting to MongoDB...")
        
        # Try connection with DNS resolution fix
        connection_string = "mongodb+srv://004shivasiva:PEyGIcAQDAl7A3dt@noparking.ixrjn.mongodb.net/?retryWrites=true&w=majority&appName=noparking"
        
        import dns.resolver
        dns.resolver.default_resolver = dns.resolver.Resolver(configure=False)
        dns.resolver.default_resolver.nameservers = ['8.8.8.8', '8.8.4.4']
        
        client = MongoClient(
            connection_string,
            serverSelectionTimeoutMS=30000,  # Increased timeout
            connectTimeoutMS=30000,
            socketTimeoutMS=30000,
            maxPoolSize=1,
            retryWrites=True,
            tlsAllowInvalidCertificates=True,
            tlsCAFile=certifi.where()
        )
        
        # Test connection
        client.admin.command('ping')
        print("✅ MongoDB connected successfully")
        
        return client
    except Exception as e:
        print(f"❌ MongoDB connection failed: {str(e)}")
        print("💡 Trying alternative connection method...")
        raise

# Initialize MongoDB connection with better error handling
USE_LOCAL_MODE = False
client = None
db = None
plate_collection = None
fastag_collection = None
transactions_collection = None

try:
    client = connect_to_mongodb()
    db = client["no_parking_db"]
    plate_collection = db["detected_plates"]
    fastag_collection = db["fastag_accounts"]
    transactions_collection = db["fine_transactions"]
    print("✅ Using MongoDB for storage")
except Exception as e:
    print(f"⚠️ MongoDB unavailable: {str(e)}")
    print("💡 Switching to LOCAL MODE with JSON storage")
    USE_LOCAL_MODE = True
    
    # Create local storage directory
    local_storage_dir = os.path.join(script_dir, "local_storage")
    if not os.path.exists(local_storage_dir):
        os.makedirs(local_storage_dir)
    
    # Initialize local JSON files
    for filename in ["detected_plates.json", "fastag_accounts.json", "fine_transactions.json"]:
        filepath = os.path.join(local_storage_dir, filename)
        if not os.path.exists(filepath):
            with open(filepath, 'w') as f:
                json.dump([], f)

# Add function to handle MongoDB operations with retry
@backoff.on_exception(backoff.expo, 
                     (NetworkTimeout, ConnectionFailure, ServerSelectionTimeoutError),
                     max_tries=3)
def mongodb_operation(operation_type, collection, *args, **kwargs):
    """Execute MongoDB operations with retry logic or use local storage"""
    if USE_LOCAL_MODE:
        # Local JSON storage fallback
        collection_name = kwargs.get('collection_name', 'unknown')
        filepath = os.path.join(script_dir, "local_storage", f"{collection_name}.json")
        
        if operation_type == "find_one":
            with open(filepath, 'r') as f:
                data = json.load(f)
            query = args[0] if args else kwargs.get('filter', {})
            for item in data:
                if all(item.get(k) == v for k, v in query.items()):
                    return item
            return None
            
        elif operation_type == "insert_one":
            with open(filepath, 'r') as f:
                data = json.load(f)
            doc = args[0] if args else kwargs.get('document', {})
            data.append(doc)
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            return type('obj', (object,), {'inserted_id': len(data)})()
            
        elif operation_type == "update_one":
            with open(filepath, 'r') as f:
                data = json.load(f)
            query = args[0] if args else kwargs.get('filter', {})
            update = args[1] if len(args) > 1 else kwargs.get('update', {})
            for i, item in enumerate(data):
                if all(item.get(k) == v for k, v in query.items()):
                    if '$set' in update:
                        item.update(update['$set'])
                    data[i] = item
                    break
            with open(filepath, 'w') as f:
                json.dump(data, f, indent=2, default=str)
            return None
    else:
        # MongoDB operations
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
        if USE_LOCAL_MODE:
            return mongodb_operation("find_one", None, {"plate_number": plate_number}, 
                                   collection_name="fastag_accounts")
        else:
            return mongodb_operation("find_one", fastag_collection, {"plate_number": plate_number})
    except Exception as e:
        print(f"❌ Error checking FASTag account: {str(e)}")
        return None

# Track processed plates and captures
processed_plates = set()  # Track processed plates
plate_captures = defaultdict(list)  # Store captures per vehicle
MAX_CAPTURES = 2  # Maximum captures per plate

# Create directory for captured images
save_dir = os.path.join(script_dir, "captured_violations")
if not os.path.exists(save_dir):
    os.makedirs(save_dir)

#Telegram configuration
TELEGRAM_BOT_TOKEN = "7823328180:AAH5MJ1u07KsUm_9RT10xvjuWTfrX7HCTok"
TELEGRAM_CHAT_ID = "2081618868"  # Replace with the chat ID you just received

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
        payment_link = "https://noparking--detection.up.railway.app/"  # Set default payment link
        
        # Get previous unpaid fines from MongoDB
        if USE_LOCAL_MODE:
            previous_fines = mongodb_operation("find_one", None, 
                {"plate_number": plate_number, "payment_status": "Pending"},
                collection_name="fine_transactions"
            )
        else:
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
                    "💳 Pay Now:\n"
                    f"🔗 {payment_link}\n\n"
                    "⚠️ Additional penalties apply for late payment"
                )
        else:  # No FASTag
            message = (
                "⚠️ NO FASTAG DETECTED - Payment Required\n\n"
                f"🚗 Vehicle: {plate_number}\n"
                f"💰 Current Fine: ₹{fine_amount}\n"
                f"💰 Previous Unpaid: ₹{previous_unpaid}\n"
                f"💰 Total Due: ₹{total_due}\n"
                f"⚠️ Payment Due By: {due_date}\n"
                f"❗️ Late Payment Penalty: ₹{late_penalty}\n\n"
                "💳 Pay Now:\n"
                f"🔗 {payment_link}\n\n"
                "⚠️ Additional penalties apply for late payment\n"
                "💡 Register for FASTag to avoid manual payments"
            )

        # Send text message
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
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
    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    
    # Apply bilateral filter to preserve edges while reducing noise
    denoised = cv2.bilateralFilter(gray, 11, 17, 17)
    
    # Enhance contrast using CLAHE
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
    enhanced = clahe.apply(denoised)
    
    # Threshold to get binary image - adaptive thresholding works better for varying lighting
    binary = cv2.adaptiveThreshold(
        enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    
    # Optional: morphological operations to further clean the image
    kernel = np.ones((2,2), np.uint8)
    processed = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
    
    return processed

def handle_error(error, context=""):
    """Improved error handling with better logging"""
    print(f"\n{'='*50}")
    print(f"❌ Error in {context}")
    print(f"Type: {type(error).__name__}")
    print(f"Details: {str(error)}")
    print('='*50)
    
    # Log error for debugging
    error_log_path = os.path.join(script_dir, "error_log.txt")
    with open(error_log_path, "a") as f:
        f.write(f"\n[{datetime.now()}] {context}: {str(error)}")
        f.write(f"\nTraceback: {error.__traceback__}")

# 🟢 Load YOLO model
plate_model = YOLO(os.path.join(script_dir, "license_plate_detector.pt"))

# 🟢 Initialize EasyOCR with additional languages that might help with character recognition
reader = easyocr.Reader(['en'], gpu=False)  # Set gpu=True if you have GPU support

# 🟢 Load Video Source
video_source = os.path.join(script_dir, "test_video/testcar5.mp4")
cap = cv2.VideoCapture(video_source)

if not cap.isOpened():
    print("Error: Could not open video stream.")
    exit()

fine_amount = 200  # 🛑 Fine amount for parking violation
frame_skip = 15  # Process every 15th frame
frame_count = 0

# Get video properties for overlay text
frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

# Font settings for better overlay
font = cv2.FONT_HERSHEY_SIMPLEX
font_scale = 0.7
font_thickness = 2
font_color = (255, 255, 255)  # White
font_bg_color = (0, 0, 255)  # Red

# Add accuracy tracking variables
class AccuracyMetrics:
    def __init__(self):
        self.total_detections = 0
        self.successful_detections = 0
        self.successful_ocr = 0
        self.successful_validations = 0
        self.detection_times = []
        self.confidence_scores = []
        self.daily_stats = defaultdict(lambda: {
            'total': 0,
            'successful': 0,
            'avg_confidence': 0.0
        })
    
    def update_detection(self, success, confidence=None):
        self.total_detections += 1
        if success:
            self.successful_detections += 1
        if confidence:
            self.confidence_scores.append(confidence)
    
    def update_ocr(self, success):
        if success:
            self.successful_ocr += 1
    
    def update_validation(self, success):
        if success:
            self.successful_validations += 1
    
    def add_detection_time(self, time_ms):
        self.detection_times.append(time_ms)
    
    def get_detection_accuracy(self):
        if self.total_detections == 0:
            return 0
        return (self.successful_detections / self.total_detections) * 100
    
    def get_ocr_accuracy(self):
        if self.successful_detections == 0:
            return 0
        return (self.successful_ocr / self.successful_detections) * 100
    
    def get_validation_accuracy(self):
        if self.successful_ocr == 0:
            return 0
        return (self.successful_validations / self.successful_ocr) * 100
    
    def get_avg_confidence(self):
        if not self.confidence_scores:
            return 0
        return sum(self.confidence_scores) / len(self.confidence_scores)
    
    def get_avg_detection_time(self):
        if not self.detection_times:
            return 0
        return sum(self.detection_times) / len(self.detection_times)
    
    def save_metrics(self):
        metrics = {
            'timestamp': datetime.now().isoformat(),
            'total_detections': self.total_detections,
            'detection_accuracy': self.get_detection_accuracy(),
            'ocr_accuracy': self.get_ocr_accuracy(),
            'validation_accuracy': self.get_validation_accuracy(),
            'avg_confidence': self.get_avg_confidence(),
            'avg_detection_time_ms': self.get_avg_detection_time()
        }
        
        metrics_path = os.path.join(script_dir, 'accuracy_metrics.json')
        with open(metrics_path, 'w') as f:
            json.dump(metrics, f, indent=4)
        return metrics

# Initialize accuracy metrics
metrics = AccuracyMetrics()

while True:
    ret, frame = cap.read()
    if not ret:
        print("End of video stream.")
        break
        
    frame_count += 1
    
    # Process every nth frame
    if frame_count % frame_skip != 0:
        continue

    # Detect license plates
    start_time = time.time()
    results = plate_model(frame)
    detection_time = (time.time() - start_time) * 1000  # Convert to milliseconds
    metrics.add_detection_time(detection_time)

    for r in results:
        for box in r.boxes:
            x1, y1, x2, y2 = map(int, box.xyxy[0])  # Bounding box coordinates
            conf = float(box.conf[0])
            
            metrics.update_detection(True, conf)
            
            # Skip if box is too small (likely false positive)
            if (x2 - x1) < 40 or (y2 - y1) < 15:
                continue
                
            plate_crop = frame[y1:y2, x1:x2]
            
            # Apply enhanced preprocessing for better OCR
            processed_img = enhance_image(plate_crop)
            
            # Store the original crop for display
            display_crop = plate_crop.copy()
            
            # Draw bounding box immediately after detection
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 0), 2)
            
            # Perform OCR with better parameters for license plates
            text_results = reader.readtext(
                processed_img,
                detail=1,
                paragraph=False,
                decoder='beamsearch',
                beamWidth=5,
                batch_size=1,
                allowlist='ABCDEFGIJKLMNOPQRSTUVWXYZ0123456789'
            )

            # Initialize variables
            max_prob = 0
            detected_parts = []

            for (bbox, text, prob) in text_results:
                if prob > 0.45:  # Filter by confidence
                    # Update max probability
                    max_prob = max(max_prob, prob)
                    # Clean and normalize the text
                    clean_text = text.replace(" ", "").upper()
                    detected_parts.append(clean_text)

            # Combine all parts into one plate number
            plate_text = "".join(detected_parts)
            
            # Clean special characters from plate number
            plate_number = clean_plate_number(plate_text)
            
            if len(plate_number) < 4:  # Skip if plate number is too short
                continue
                
            print(f"Detected Plate: {plate_number} (Confidence: {max_prob:.2f})")
            
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
            
            # Store capture for verification
            if len(plate_captures[plate_number]) < MAX_CAPTURES:
                plate_captures[plate_number].append(plate_number)
                print(f"📸 Capture #{len(plate_captures[plate_number])} stored")
            
            # Verify captures
            if not verify_plate_captures(plate_number, plate_captures[plate_number]):
                continue
            
            # Save verified images
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            save_path = f"{save_dir}/plate_{plate_number}_{timestamp}"
            
            # Draw detection box in red when processing violation
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 3)  # Thicker red box for violation
            
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
                    'coordinates': (x1, y1, x2, y2)
                }
                print(f"✅ Images saved for {plate_number}")
            else:
                print(f"❌ Failed to save all images for {plate_number}")
                continue  # Skip processing if images weren't saved

            # Show processed image in small window for debugging
            resized_processed = cv2.resize(processed_img, (200, 100))
            frame[10:110, 10:210] = cv2.cvtColor(resized_processed, cv2.COLOR_GRAY2BGR)
            
            # Add "Processed" label
            cv2.putText(frame, "Processed", (10, 130), 
                        font, 0.5, (255, 255, 255), 1)

            # Process the plate if not already processed
            if plate_number not in processed_plates:
                # 🔴 Check FASTag account
                fastag_data = check_fastag_account(plate_number)
                
                # Determine if FASTag exists
                has_fastag = fastag_data is not None
                
                # 🟢 Save to MongoDB with additional details
                doc = {
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
                }
                
                if USE_LOCAL_MODE:
                    mongodb_operation("insert_one", None, doc, collection_name="detected_plates")
                else:
                    mongodb_operation("insert_one", plate_collection, doc)

                phone_number = None
                
                if has_fastag:
                    # Process with FASTag
                    balance = fastag_data["balance"]
                    phone_number = fastag_data.get("phone_number", "")
                    
                    if balance >= fine_amount:
                        # Deduct fine amount and process as before
                        new_balance = balance - fine_amount
                        if USE_LOCAL_MODE:
                            mongodb_operation("update_one", None,
                                {"plate_number": plate_number},
                                {"$set": {"balance": new_balance}},
                                collection_name="fastag_accounts"
                            )
                        else:
                            mongodb_operation("update_one", fastag_collection,
                                {"plate_number": plate_number},
                                {"$set": {"balance": new_balance}}
                            )
                        
                        # Record successful transaction
                        trans_doc = {
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
                        }
                        if USE_LOCAL_MODE:
                            mongodb_operation("insert_one", None, trans_doc, collection_name="fine_transactions")
                        else:
                            mongodb_operation("insert_one", transactions_collection, trans_doc)

                        print(f"✅ Fine of ₹{fine_amount} deducted from {plate_number}. New balance: ₹{new_balance}")
                            
                    else:
                        # Insufficient balance case
                        print(f"❌ Insufficient FASTag balance for {plate_number} (₹{balance})")
                        payment_link = "https://noparking--detection.up.railway.app/"
                        
                        # Record pending transaction
                        trans_doc = {
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
                        }
                        if USE_LOCAL_MODE:
                            mongodb_operation("insert_one", None, trans_doc, collection_name="fine_transactions")
                        else:
                            mongodb_operation("insert_one", transactions_collection, trans_doc)
                        
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
                    if USE_LOCAL_MODE:
                        vehicle_data = mongodb_operation("find_one", None, {"number_plate": plate_number}, collection_name="detected_plates")
                    else:
                        vehicle_data = mongodb_operation("find_one", plate_collection, {"number_plate": plate_number})
                    if vehicle_data and "phone_number" in vehicle_data and vehicle_data["phone_number"] != "Unknown":
                        phone_number = vehicle_data["phone_number"]
                    
                    # Record violation without FASTag
                    trans_doc = {
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
                    }
                    if USE_LOCAL_MODE:
                        mongodb_operation("insert_one", None, trans_doc, collection_name="fine_transactions")
                    else:
                        mongodb_operation("insert_one", transactions_collection, trans_doc)
                    
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

    # Add accuracy overlay to frame
    accuracy_text = [
        f"Detection Accuracy: {metrics.get_detection_accuracy():.1f}%",
        f"OCR Accuracy: {metrics.get_ocr_accuracy():.1f}%",
        f"Validation Rate: {metrics.get_validation_accuracy():.1f}%",
        f"Avg Confidence: {metrics.get_avg_confidence():.2f}",
        f"Avg Detection Time: {metrics.get_avg_detection_time():.1f}ms"
    ]
    
    y_offset = 30
    for text in accuracy_text:
        cv2.putText(frame, text, (frame_width - 300, y_offset), 
                   font, 0.5, (255, 255, 255), 1)
        y_offset += 20

    # Save metrics every 100 frames
    if frame_count % 100 == 0:
        current_metrics = metrics.save_metrics()
        print("\nCurrent System Accuracy Metrics:")
        print(f"Detection Accuracy: {current_metrics['detection_accuracy']:.1f}%")
        print(f"OCR Accuracy: {current_metrics['ocr_accuracy']:.1f}%")
        print(f"Validation Rate: {current_metrics['validation_accuracy']:.1f}%")
        print(f"Average Confidence: {current_metrics['avg_confidence']:.2f}")
        print(f"Average Detection Time: {current_metrics['avg_detection_time_ms']:.1f}ms\n")

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

    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()