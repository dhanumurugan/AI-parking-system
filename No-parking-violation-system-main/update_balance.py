from pymongo import MongoClient
from urllib.parse import quote_plus

# MongoDB connection
username = quote_plus("Arunkarthik")
password = quote_plus("arunkarthik@111")
connection_string = f"mongodb+srv://{username}:{password}@arun.qpjio.mongodb.net/?retryWrites=true&w=majority&appName=Arun"
client = MongoClient(connection_string)

db = client["no_parking_db"]
fastag_collection = db["fastag_accounts"]

# Update balance
plate_number = "TN37CS2765"
new_balance = 1000

result = fastag_collection.update_one(
    {"plate_number": plate_number},
    {"$set": {"balance": new_balance}},
    upsert=True
)

if result.modified_count > 0:
    print(f"Updated balance for {plate_number} to ₹{new_balance}")
elif result.upserted_id:
    print(f"Created new account for {plate_number} with balance ₹{new_balance}")
else:
    print("No changes made")