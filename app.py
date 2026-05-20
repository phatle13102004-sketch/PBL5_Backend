from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import random
import re
import threading
from typing import Any, Dict, Tuple

import cv2
import numpy as np
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from werkzeug.security import check_password_hash, generate_password_hash
from web3 import Web3

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = int(os.getenv("MAX_UPLOAD_MB", "8")) * 1024 * 1024

frontend_origin = os.getenv("FRONTEND_ORIGIN", "*").strip()
allowed_origins = "*" if frontend_origin == "*" else [x.strip() for x in frontend_origin.split(",") if x.strip()]
CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

tx_lock = threading.Lock()

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
DB_NAME = os.getenv("MONGODB_DB", "PBL5_Farm")
WEB3_RPC_URL = os.getenv("WEB3_RPC_URL", "https://sepolia.drpc.org").strip()
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
BLOCKCHAIN_MODE = os.getenv("BLOCKCHAIN_MODE", "auto").strip().lower()  # auto | real | mock

client = None
db = None
harvest_collection = None
users_collection = None

if MONGODB_URI:
    try:
        client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        db = client[DB_NAME]
        harvest_collection = db["harvest_records"]
        users_collection = db["users_account"]
        users_collection.create_index([("username", ASCENDING)], unique=True)
        users_collection.create_index([("phone", ASCENDING)], unique=True)
        harvest_collection.create_index([("farmer", ASCENDING), ("recorded_at", DESCENDING)])
        print("✅ Đã kết nối MongoDB Cloud!")
    except Exception as exc:
        print("❌ Lỗi MongoDB:", exc)
else:
    print("⚠️ Chưa cấu hình MONGODB_URI. Các API cần database sẽ báo lỗi rõ ràng.")

w3 = None
if WEB3_RPC_URL:
    try:
        w3 = Web3(Web3.HTTPProvider(WEB3_RPC_URL, request_kwargs={"timeout": 15}))
        if w3.is_connected():
            print("✅ Đã kết nối Web3 (Sepolia)")
        else:
            print("⚠️ Không kết nối được Web3 RPC.")
    except Exception as exc:
        print("❌ Lỗi Web3:", exc)

contract_abi = [
    {
        "inputs": [
            {"internalType": "string", "name": "_farmer", "type": "string"},
            {"internalType": "string", "name": "_flowerType", "type": "string"},
            {"internalType": "uint256", "name": "_weight", "type": "uint256"},
        ],
        "name": "addHarvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


def require_db() -> Tuple[Any, Any]:
    if harvest_collection is None or users_collection is None:
        raise RuntimeError("Database chưa sẵn sàng. Kiểm tra MONGODB_URI trên Render/local .env.")
    return harvest_collection, users_collection


def json_ok(payload: Dict[str, Any], code: int = 200):
    payload.setdefault("status", "success")
    return jsonify(payload), code


def json_error(message: str, code: int = 400):
    return jsonify({"status": "error", "message": message}), code


def get_json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


def clean_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def normalize_username(value: Any) -> str:
    return clean_str(value).lower()


def vn_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=7)


def vn_time_display(now: dt.datetime | None = None) -> str:
    now = now or vn_now()
    return now.strftime("%d/%m/%Y %H:%M")


def utc_iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_positive_int(value: Any, field_name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} phải là số nguyên hợp lệ.")
    if number <= 0:
        raise ValueError(f"{field_name} phải lớn hơn 0.")
    return number


def blockchain_is_configured() -> bool:
    return bool(w3 and w3.is_connected() and PRIVATE_KEY and CONTRACT_ADDRESS)


def make_mock_tx_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True) + str(dt.datetime.now(dt.timezone.utc).timestamp())
    return "0x" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def send_to_blockchain(farmer: str, combined_flower_type: str, weight: int) -> Tuple[str, str]:
    payload = {"farmer": farmer, "flower_type": combined_flower_type, "weight": weight}

    if BLOCKCHAIN_MODE == "mock" or (BLOCKCHAIN_MODE == "auto" and not blockchain_is_configured()):
        return make_mock_tx_hash(payload), "mock"

    if not blockchain_is_configured():
        raise RuntimeError("Blockchain chưa cấu hình đủ PRIVATE_KEY, CONTRACT_ADDRESS hoặc WEB3_RPC_URL.")

    contract_address = Web3.to_checksum_address(CONTRACT_ADDRESS)
    account = w3.eth.account.from_key(PRIVATE_KEY)
    contract = w3.eth.contract(address=contract_address, abi=contract_abi)

    with tx_lock:
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx = contract.functions.addHarvest(farmer, combined_flower_type, weight).build_transaction(
            {
                "chainId": 11155111,
                "gas": 3000000,
                "gasPrice": w3.eth.gas_price,
                "nonce": nonce,
            }
        )
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        raw_tx = getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction", None)
        if raw_tx is None:
            raise RuntimeError("Không lấy được raw transaction từ Web3 signed transaction.")
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        return w3.to_hex(tx_hash), "real"


def classify_flower_image(image_bytes: bytes) -> Dict[str, Any]:
    np_arr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Không đọc được ảnh. Vui lòng gửi ảnh JPG/PNG hợp lệ.")

    frame = cv2.resize(frame, (640, 720))
    result = frame.copy()
    h, w = frame.shape[:2]

    roi_x1, roi_x2 = int(w * 0.20), int(w * 0.80)
    roi_y1, roi_y2 = int(h * 0.15), int(h * 0.85)
    roi = frame[roi_y1:roi_y2, roi_x1:roi_x2]

    cv2.rectangle(result, (roi_x1, roi_y1), (roi_x2, roi_y2), (255, 255, 255), 2)

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    lower_yellow = np.array([15, 60, 60])
    upper_yellow = np.array([40, 255, 255])
    mask = cv2.inRange(hsv, lower_yellow, upper_yellow)

    kernel = np.ones((9, 9), np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.dilate(mask, kernel, iterations=2)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    perimeter = 0.0
    area = 0.0
    if contours:
        largest = max(contours, key=cv2.contourArea)
        area = float(cv2.contourArea(largest))
        perimeter = float(cv2.arcLength(largest, True))
        contour_shifted = largest + np.array([[[roi_x1, roi_y1]]])
        cv2.drawContours(result, [contour_shifted], -1, (0, 255, 0), 4)

    if perimeter > 800:
        flower_type = "TYPE 1 - LARGE"
        quality = "Loại 1"
        price_vnd = 500000
        color = (0, 255, 0)
    elif perimeter > 700:
        flower_type = "TYPE 2 - MEDIUM"
        quality = "Loại 2"
        price_vnd = 400000
        color = (0, 255, 255)
    elif perimeter > 550:
        flower_type = "TYPE 3 - SMALL"
        quality = "Loại 3"
        price_vnd = 300000
        color = (0, 0, 255)
    else:
        flower_type = "UNDETECTED"
        quality = "Không xác định"
        price_vnd = 0
        color = (255, 255, 255)

    cv2.putText(result, flower_type, (20, 50), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 3)
    cv2.putText(result, f"{price_vnd:,} VND", (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 3)
    cv2.putText(result, f"Perimeter: {int(perimeter)}", (20, 150), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    ok, buffer = cv2.imencode(".jpg", result, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    annotated_image = ""
    if ok:
        annotated_image = "data:image/jpeg;base64," + base64.b64encode(buffer).decode("utf-8")

    return {
        "detected": perimeter > 550,
        "flower_name": "Cúc Vàng",
        "flower_type": flower_type,
        "quality": quality,
        "price_vnd": price_vnd,
        "price_display": f"{price_vnd:,}".replace(",", ".") + " VNĐ" if price_vnd else "0 VNĐ",
        "perimeter": round(perimeter, 2),
        "area": round(area, 2),
        "roi": {"x1": roi_x1, "x2": roi_x2, "y1": roi_y1, "y2": roi_y2},
        "annotated_image": annotated_image,
    }


@app.errorhandler(413)
def file_too_large(_):
    return json_error("Ảnh quá lớn. Hãy giảm dung lượng ảnh hoặc tăng MAX_UPLOAD_MB.", 413)


@app.route("/api/health", methods=["GET"])
def health():
    return json_ok(
        {
            "database": harvest_collection is not None and users_collection is not None,
            "web3_connected": bool(w3 and w3.is_connected()),
            "blockchain_mode": BLOCKCHAIN_MODE,
            "blockchain_ready": blockchain_is_configured(),
            "time_vn": vn_time_display(),
        }
    )


@app.route("/api/register", methods=["POST"])
def register():
    try:
        _, users = require_db()
        data = get_json_body()
        username = normalize_username(data.get("username"))
        password = clean_str(data.get("password"))
        fullname = clean_str(data.get("fullname"))
        location = clean_str(data.get("location"))
        phone = clean_str(data.get("phone"))

        if not username or not password or not fullname or not location or not phone:
            return json_error("Vui lòng điền đầy đủ thông tin!", 400)
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            return json_error("Tên tài khoản chỉ gồm chữ thường, số, dấu gạch dưới và dài 3-30 ký tự.", 400)
        if len(password) < 6:
            return json_error("Mật khẩu nên có ít nhất 6 ký tự.", 400)
        if users.find_one({"username": username}):
            return json_error("Tài khoản đăng ký đã tồn tại!", 400)
        if users.find_one({"phone": phone}):
            return json_error("Số điện thoại này đã được sử dụng!", 400)

        for _ in range(10):
            farmer_id = f"FAR_{random.randint(1000, 9999)}"
            if not users.find_one({"farmer_id": farmer_id}):
                break

        users.insert_one(
            {
                "username": username,
                "password": generate_password_hash(password),
                "fullname": fullname,
                "location": location,
                "phone": phone,
                "farmer_id": farmer_id,
                "join_date": vn_now().strftime("%d/%m/%Y"),
                "created_at": utc_iso_now(),
            }
        )
        return json_ok({"message": "Đăng ký thành công!"})
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    try:
        _, users = require_db()
        data = get_json_body()
        phone = clean_str(data.get("phone"))
        new_password = clean_str(data.get("new_password"))
        if not phone or not new_password:
            return json_error("Vui lòng truyền đủ thông tin!", 400)
        if len(new_password) < 6:
            return json_error("Mật khẩu mới nên có ít nhất 6 ký tự.", 400)

        user = users.find_one({"phone": phone})
        if not user:
            return json_error("Số điện thoại chưa được đăng ký!", 404)

        users.update_one({"_id": user["_id"]}, {"$set": {"password": generate_password_hash(new_password)}})
        return json_ok({"message": "Cập nhật mật khẩu thành công!", "username": user.get("username", "")})
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/login", methods=["POST"])
def login():
    try:
        _, users = require_db()
        data = get_json_body()
        username = normalize_username(data.get("username"))
        password = clean_str(data.get("password"))
        if not username or not password:
            return json_error("Vui lòng nhập đủ tài khoản và mật khẩu.", 400)

        user = users.find_one({"username": username})
        if user and check_password_hash(user.get("password", ""), password):
            return json_ok(
                {
                    "username": username,
                    "fullname": user.get("fullname", ""),
                    "location": user.get("location", ""),
                    "farmer_id": user.get("farmer_id", ""),
                    "join_date": user.get("join_date", ""),
                }
            )
        return json_error("Sai tài khoản hoặc mật khẩu!", 401)
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/harvest", methods=["POST"])
def add_harvest():
    try:
        harvests, _ = require_db()
        data = get_json_body()
        farmer = normalize_username(data.get("farmer"))
        ma_lo = clean_str(data.get("ma_lo"))
        flower_name = clean_str(data.get("flower_name"))
        ten_vuon = clean_str(data.get("ten_vuon"))
        ngay_thu = clean_str(data.get("ngay_thu"))
        khu_vuc = clean_str(data.get("khu_vuc"))
        weight = parse_positive_int(data.get("weight", 0), "Sản lượng")
        gia_ban = clean_str(data.get("gia_ban"))
        quality = clean_str(data.get("quality"), "Loại 3")
        ghi_chu = clean_str(data.get("ghi_chu"))
        ai_type = clean_str(data.get("ai_type"))
        ai_price = clean_str(data.get("ai_price"))
        ai_perimeter = data.get("ai_perimeter")

        required = [farmer, ma_lo, flower_name, ten_vuon, ngay_thu, khu_vuc, gia_ban, quality]
        if not all(required):
            return json_error("Vui lòng điền đầy đủ thông tin bắt buộc.", 400)

        combined_flower_type = f"{ma_lo}|{flower_name}|{ten_vuon}|{quality}|{gia_ban}"
        tx_hash_hex, chain_mode = send_to_blockchain(farmer, combined_flower_type, weight)

        now_iso = utc_iso_now()
        now_display = vn_time_display()
        doc = {
            "farmer": farmer,
            "ma_lo": ma_lo,
            "flower_name": flower_name,
            "ten_vuon": ten_vuon,
            "ngay_thu": ngay_thu,
            "khu_vuc": khu_vuc,
            "weight": weight,
            "gia_ban": gia_ban,
            "quality": quality,
            "ghi_chu": ghi_chu,
            "flower_type": f"{flower_name} - {quality}",
            "tx_hash": tx_hash_hex,
            "blockchain_mode": chain_mode,
            "date": now_display,
            "recorded_at": now_iso,
            "ai_type": ai_type,
            "ai_price": ai_price,
            "ai_perimeter": ai_perimeter,
        }
        harvests.insert_one(doc)

        return json_ok({"message": f"TxHash: {tx_hash_hex}", "tx_hash": tx_hash_hex, "blockchain_mode": chain_mode})
    except ValueError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/history", methods=["GET"])
def get_history():
    try:
        harvests, _ = require_db()
        farmer = normalize_username(request.args.get("farmer"))
        if not farmer:
            return json_error("Thiếu farmer.", 400)
        records = list(harvests.find({"farmer": farmer}).sort([("recorded_at", DESCENDING), ("_id", DESCENDING)]))
        for record in records:
            record["_id"] = str(record["_id"])
        return json_ok({"records": records})
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        harvests, _ = require_db()
        farmer = normalize_username(request.args.get("farmer"))
        if not farmer:
            return json_error("Thiếu farmer.", 400)
        user_records = list(harvests.find({"farmer": farmer}))
        total_weight = sum(int(record.get("weight", 0) or 0) for record in user_records)
        loai1_weight = sum(
            int(record.get("weight", 0) or 0)
            for record in user_records
            if record.get("quality") == "Loại 1" or "Loại 1" in record.get("flower_type", "")
        )
        return json_ok({"total_weight": total_weight, "loai1_weight": loai1_weight})
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/classify-flower", methods=["POST"])
def classify_flower():
    try:
        image_bytes = b""
        if "image" in request.files:
            image_bytes = request.files["image"].read()
        else:
            data = get_json_body()
            image_base64 = clean_str(data.get("image_base64"))
            if "," in image_base64:
                image_base64 = image_base64.split(",", 1)[1]
            if image_base64:
                image_bytes = base64.b64decode(image_base64)

        if not image_bytes:
            return json_error("Thiếu ảnh. Gửi multipart field 'image' hoặc JSON image_base64.", 400)

        result = classify_flower_image(image_bytes)
        return json_ok(result)
    except Exception as exc:
        return json_error(str(exc), 500)


@app.route("/api/weather", methods=["GET"])
def get_weather():
    weather_conditions = [
        {"day": "Hôm nay", "temp": "28°C", "humidity": "65%", "status": "Trời nắng đẹp", "recommendation": "Rất thuận lợi để thu hoạch cúc đại đóa. Hoa sẽ đạt phẩm chất màu sắc tốt nhất.", "icon": "☀️", "color": "#059669"},
        {"day": "Ngày mai", "temp": "33°C", "humidity": "50%", "status": "Nắng gắt", "recommendation": "Nên thu hoạch vào sáng sớm hoặc chiều mát. Tránh khung giờ trưa để hoa không bị héo nát.", "icon": "🌤️", "color": "#d97706"},
        {"day": "Ngày kia", "temp": "24°C", "humidity": "88%", "status": "Mưa rào rải rác", "recommendation": "Cân nhắc hoãn thu hoạch. Hoa dính nước mưa dễ bị úng và nấm mốc khi đóng gói vận chuyển.", "icon": "🌧️", "color": "#ef4444"},
    ]
    return json_ok({"forecast": weather_conditions})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
