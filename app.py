from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import threading
from typing import Any, Dict, Tuple

from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
from werkzeug.security import check_password_hash, generate_password_hash
from web3 import Web3
try:
    from web3.exceptions import TimeExhausted
except Exception:  # web3 version fallback
    TimeExhausted = TimeoutError

load_dotenv()

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("dm4farm")

VN_TZ = dt.timezone(dt.timedelta(hours=7))
SORT_LATEST = [("recorded_at", DESCENDING), ("_id", DESCENDING)]
APP_VERSION = "2026-05-20-backend-optimized-v5"


def env_int(name: str, default: int, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        logger.warning("Invalid integer env %s=%r. Using default %s.", name, raw, default)
        value = default
    if min_value is not None:
        value = max(value, min_value)
    if max_value is not None:
        value = min(value, max_value)
    return value


def env_float(name: str, default: float, min_value: float | None = None, max_value: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        logger.warning("Invalid float env %s=%r. Using default %s.", name, raw, default)
        value = default
    if min_value is not None:
        value = max(value, min_value)
    if max_value is not None:
        value = min(value, max_value)
    return value


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = env_int("MAX_UPLOAD_MB", 8, 1, 50) * 1024 * 1024

frontend_origin = os.getenv("FRONTEND_ORIGIN", "*").strip()
allowed_origins = "*" if frontend_origin == "*" else [x.strip() for x in frontend_origin.split(",") if x.strip()]
CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

tx_lock = threading.Lock()

MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
DB_NAME = os.getenv("MONGODB_DB", "PBL5_Farm").strip() or "PBL5_Farm"
WEB3_RPC_URL = os.getenv("WEB3_RPC_URL", "https://sepolia.drpc.org").strip()
CONTRACT_ADDRESS = os.getenv("CONTRACT_ADDRESS", "").strip()
PRIVATE_KEY = os.getenv("PRIVATE_KEY", "").strip()
BLOCKCHAIN_MODE = os.getenv("BLOCKCHAIN_MODE", "auto").strip().lower()  # auto | real | mock
if BLOCKCHAIN_MODE not in {"auto", "real", "mock"}:
    logger.warning("BLOCKCHAIN_MODE=%r không hợp lệ. Tự chuyển về auto.", BLOCKCHAIN_MODE)
    BLOCKCHAIN_MODE = "auto"

client = None
_db = None
harvest_collection = None
users_collection = None


def safe_create_index(collection, keys, **kwargs) -> None:
    try:
        collection.create_index(keys, **kwargs)
    except Exception as exc:
        logger.warning("Không tạo được index %s trên %s: %s", keys, collection.name, exc)


if MONGODB_URI:
    try:
        client = MongoClient(
            MONGODB_URI,
            serverSelectionTimeoutMS=env_int("MONGO_SERVER_SELECTION_TIMEOUT_MS", 5000, 1000, 30000),
            connectTimeoutMS=env_int("MONGO_CONNECT_TIMEOUT_MS", 8000, 1000, 30000),
            socketTimeoutMS=env_int("MONGO_SOCKET_TIMEOUT_MS", 20000, 1000, 60000),
            retryWrites=True,
        )
        client.admin.command("ping")
        _db = client[DB_NAME]
        harvest_collection = _db["harvest_records"]
        users_collection = _db["users_account"]
        safe_create_index(users_collection, [("username", ASCENDING)], unique=True, background=True, name="uniq_username")
        safe_create_index(users_collection, [("phone", ASCENDING)], unique=True, background=True, name="uniq_phone")
        safe_create_index(users_collection, [("farmer_id", ASCENDING)], unique=True, sparse=True, background=True, name="uniq_farmer_id")
        safe_create_index(harvest_collection, [("farmer", ASCENDING), ("recorded_at", DESCENDING)], background=True, name="farmer_recorded_at")
        safe_create_index(harvest_collection, [("farmer_display", ASCENDING), ("recorded_at", DESCENDING)], background=True, name="farmer_display_recorded_at")
        safe_create_index(harvest_collection, [("username", ASCENDING), ("recorded_at", DESCENDING)], background=True, name="username_recorded_at")
        safe_create_index(harvest_collection, [("ma_lo", ASCENDING), ("farmer", ASCENDING)], background=True, name="ma_lo_farmer")
        safe_create_index(harvest_collection, [("tx_hash", ASCENDING)], sparse=True, background=True, name="tx_hash_lookup")
        logger.info("Đã kết nối MongoDB Cloud: db=%s", DB_NAME)
    except Exception as exc:
        logger.error("Lỗi MongoDB: %s", exc)
else:
    logger.warning("Chưa cấu hình MONGODB_URI. Các API cần database sẽ báo lỗi rõ ràng.")

w3 = None
if WEB3_RPC_URL:
    try:
        w3 = Web3(Web3.HTTPProvider(WEB3_RPC_URL, request_kwargs={"timeout": env_int("WEB3_TIMEOUT", 15, 3, 60)}))
        if w3.is_connected():
            logger.info("Đã kết nối Web3 RPC")
        else:
            logger.warning("Không kết nối được Web3 RPC.")
    except Exception as exc:
        logger.error("Lỗi Web3: %s", exc)

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


def json_error(message: str, code: int = 400, **extra):
    payload = {"status": "error", "message": message}
    payload.update(extra)
    return jsonify(payload), code


def get_json_body() -> Dict[str, Any]:
    if request.is_json:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}
    if request.form:
        return request.form.to_dict()
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def clean_str(value: Any, default: str = "", max_len: int | None = 255) -> str:
    if value is None:
        return default
    text = str(value).strip()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    if max_len is not None:
        text = text[:max_len]
    return text


def normalize_username(value: Any) -> str:
    return clean_str(value, max_len=64).lower()


def normalize_phone(value: Any) -> str:
    return re.sub(r"\D+", "", clean_str(value, max_len=32))


def vn_now() -> dt.datetime:
    return dt.datetime.now(VN_TZ)


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def vn_time_display(now: dt.datetime | None = None) -> str:
    now = now or vn_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(VN_TZ).strftime("%d/%m/%Y %H:%M")


def utc_iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def display_from_iso(value: Any) -> str:
    text = clean_str(value, max_len=80)
    if not text:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        return vn_time_display(parsed)
    except Exception:
        return text


def parse_positive_int(value: Any, field_name: str) -> int:
    text = clean_str(value, max_len=64).replace(",", "").replace(".", "")
    try:
        number = int(text)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} phải là số nguyên hợp lệ.")
    if number <= 0:
        raise ValueError(f"{field_name} phải lớn hơn 0.")
    return number


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_limit(raw: Any, default: int = 500, max_value: int = 2000) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max_value))


def safe_web3_connected() -> bool:
    try:
        return bool(w3 and w3.is_connected())
    except Exception:
        return False


def blockchain_is_configured() -> bool:
    return bool(w3 and safe_web3_connected() and PRIVATE_KEY and CONTRACT_ADDRESS)


def effective_blockchain_mode() -> str:
    if BLOCKCHAIN_MODE == "mock":
        return "mock"
    if BLOCKCHAIN_MODE == "auto" and not blockchain_is_configured():
        return "mock"
    return "real"


def make_mock_tx_hash(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True) + str(utc_now().timestamp()) + secrets.token_hex(8)
    return "0xmock" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


def get_user_aliases(username: str, users=None) -> list[str]:
    """Return all possible farmer keys used by older and newer frontend versions."""
    aliases: list[str] = []
    username = normalize_username(username)
    if username:
        aliases.append(username)

    user = None
    try:
        if users is not None and username:
            user = users.find_one({"username": username}, {"fullname": 1, "username": 1, "farmer_id": 1})
    except Exception:
        user = None

    if user:
        for key in ("fullname", "username", "farmer_id"):
            value = clean_str(user.get(key))
            if value:
                aliases.extend([value, value.lower()])

    output: list[str] = []
    seen = set()
    for item in aliases:
        key = item.casefold()
        if item and key not in seen:
            output.append(item)
            seen.add(key)
    return output


def build_farmer_query(username: str, users=None) -> Dict[str, Any]:
    aliases = get_user_aliases(username, users)
    if not aliases:
        return {}
    return {
        "$or": [
            {"farmer": {"$in": aliases}},
            {"farmer_display": {"$in": aliases}},
            {"username": {"$in": aliases}},
        ]
    }


def object_id_iso_value(oid: Any) -> str:
    try:
        return oid.generation_time.isoformat()
    except Exception:
        return ""


def normalize_quality(value: Any, flower_type: str = "") -> str:
    quality = clean_str(value, max_len=32)
    if quality in {"Loại 1", "Loại 2", "Loại 3"}:
        return quality
    if "Loại 1" in flower_type:
        return "Loại 1"
    if "Loại 2" in flower_type:
        return "Loại 2"
    if "Loại 3" in flower_type:
        return "Loại 3"
    return quality or "Loại 3"


def normalize_harvest_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """Make legacy blockchain-only records and new cloud records render the same way."""
    record = dict(record)
    created_from_oid = object_id_iso_value(record.get("_id"))
    record["_id"] = str(record.get("_id", ""))

    flower_type = clean_str(record.get("flower_type"), max_len=512)
    flower_name = clean_str(record.get("flower_name"), max_len=128)
    if flower_type and not flower_name:
        if "|" in flower_type:
            parts = [p.strip() for p in flower_type.split("|")]
            flower_name = parts[1] if len(parts) > 1 and parts[1] else parts[0]
        else:
            flower_name = flower_type.split(" - ")[0].strip()
    if not flower_name:
        flower_name = "Cúc Đại Đóa"

    quality = normalize_quality(record.get("quality"), flower_type)
    if not flower_type:
        flower_type = f"{flower_name} - {quality}"

    recorded_at = clean_str(record.get("recorded_at"), max_len=80) or created_from_oid or utc_iso_now()
    tx_hash = clean_str(record.get("tx_hash"), max_len=120)
    blockchain_status = clean_str(record.get("blockchain_status"), max_len=32)
    if not blockchain_status:
        blockchain_status = "confirmed" if tx_hash else "old"

    blockchain_mode = clean_str(record.get("blockchain_mode"), max_len=16)
    if not blockchain_mode:
        blockchain_mode = "mock" if tx_hash.startswith("0xmock") else ("real" if tx_hash else "unknown")

    record.setdefault("ma_lo", "LOT_" + str(record["_id"])[-6:].upper())
    record.setdefault("ten_vuon", "Vườn cục bộ")
    record.setdefault("ngay_thu", "")
    record.setdefault("khu_vuc", "Chưa xác định")
    record.setdefault("gia_ban", "")
    record.setdefault("ghi_chu", "")
    record["flower_name"] = flower_name
    record["quality"] = quality
    record["flower_type"] = flower_type
    record["weight"] = to_int(record.get("weight"), 0)
    record["tx_hash"] = tx_hash
    record["blockchain_status"] = blockchain_status
    record["blockchain_mode"] = blockchain_mode
    record.setdefault("block_number", None)
    record["recorded_at"] = recorded_at
    record["date"] = clean_str(record.get("date"), max_len=80) or display_from_iso(recorded_at) or "Hệ thống cũ"
    record.setdefault("schema_version", "legacy-normalized")
    return record


def harvest_sort_key(record: Dict[str, Any]) -> str:
    return clean_str(record.get("recorded_at"), max_len=80) or object_id_iso_value(record.get("_id"))


def send_to_blockchain(farmer: str, combined_flower_type: str, weight: int) -> Tuple[str, str, str, Any, str]:
    """Send harvest data to Sepolia and wait shortly for the receipt.

    Returns: tx_hash, chain_mode, tx_status, block_number, error_message
    tx_status is one of: confirmed, pending, failed.
    """
    payload = {"farmer": farmer, "flower_type": combined_flower_type, "weight": weight}

    if BLOCKCHAIN_MODE == "mock" or (BLOCKCHAIN_MODE == "auto" and not blockchain_is_configured()):
        return make_mock_tx_hash(payload), "mock", "confirmed", None, ""

    if not blockchain_is_configured():
        raise RuntimeError("Blockchain chưa cấu hình đủ PRIVATE_KEY, CONTRACT_ADDRESS hoặc WEB3_RPC_URL.")

    if w3 is None or not safe_web3_connected():
        raise RuntimeError("Không kết nối được Web3 RPC. Kiểm tra WEB3_RPC_URL.")

    try:
        contract_address = Web3.to_checksum_address(CONTRACT_ADDRESS)
    except Exception:
        raise RuntimeError("CONTRACT_ADDRESS không hợp lệ.")

    account = w3.eth.account.from_key(PRIVATE_KEY)
    contract = w3.eth.contract(address=contract_address, abi=contract_abi)
    function_call = contract.functions.addHarvest(farmer, combined_flower_type, weight)

    with tx_lock:
        nonce = w3.eth.get_transaction_count(account.address, "pending")
        tx_params = {
            "chainId": env_int("CHAIN_ID", 11155111, 1),
            "from": account.address,
            "nonce": nonce,
            "value": 0,
        }

        try:
            estimated_gas = function_call.estimate_gas({"from": account.address})
            tx_params["gas"] = min(max(int(estimated_gas * 1.3), 120_000), env_int("MAX_GAS_LIMIT", 3_000_000, 100_000, 8_000_000))
        except Exception as exc:
            logger.warning("Không estimate được gas, dùng fallback: %s", exc)
            tx_params["gas"] = env_int("FALLBACK_GAS_LIMIT", 300_000, 100_000, 3_000_000)

        try:
            latest_block = w3.eth.get_block("latest")
            base_fee = latest_block.get("baseFeePerGas")
            if base_fee:
                priority_gwei = env_float("MAX_PRIORITY_FEE_GWEI", 2.0, 0.1, 50.0)
                priority_fee = w3.to_wei(priority_gwei, "gwei")
                tx_params["maxPriorityFeePerGas"] = int(priority_fee)
                tx_params["maxFeePerGas"] = int(base_fee * 2 + priority_fee)
            else:
                tx_params["gasPrice"] = int(w3.eth.gas_price * 1.35)
        except Exception:
            tx_params["gasPrice"] = int(w3.eth.gas_price * 1.35)

        tx = function_call.build_transaction(tx_params)
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=PRIVATE_KEY)
        raw_tx = getattr(signed_tx, "raw_transaction", None) or getattr(signed_tx, "rawTransaction", None)
        if raw_tx is None:
            raise RuntimeError("Không lấy được raw transaction từ Web3 signed transaction.")

        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        tx_hash_hex = w3.to_hex(tx_hash)

    timeout_seconds = env_int("TX_WAIT_TIMEOUT", 30, 5, 180)
    try:
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash_hex, timeout=timeout_seconds, poll_latency=3)
        block_number = receipt.get("blockNumber")
        if int(receipt.get("status", 0)) == 1:
            return tx_hash_hex, "real", "confirmed", block_number, ""
        return tx_hash_hex, "real", "failed", block_number, "Transaction đã mined nhưng status = 0."
    except TimeExhausted:
        return tx_hash_hex, "real", "pending", None, f"Transaction đã gửi nhưng chưa được mined sau {timeout_seconds}s."


def classify_flower_image(image_bytes: bytes) -> Dict[str, Any]:
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        raise RuntimeError("Thiếu thư viện AI trên backend. Cập nhật requirements.txt, deploy lại backend, rồi kiểm tra log Render. Cần có: opencv-python-headless và numpy. Chi tiết: " + str(exc))

    """Detect and classify a chrysanthemum bundle from an uploaded image.

    The first integrated version was too strict: it only accepted a narrow yellow
    HSV range inside a small center ROI and required perimeter > 550. In real
    camera/upload images, the flower may be off-center, too small, white/pink,
    or affected by lighting. This version keeps the same HSV/contour idea but
    makes it robust enough for demo use.
    """
    np_arr = np.frombuffer(image_bytes, np.uint8)
    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError("Không đọc được ảnh. Vui lòng gửi ảnh JPG/PNG hợp lệ.")

    # Keep aspect ratio instead of forcing 640x720, because forced stretching can
    # change contour perimeter and make classification unstable.
    h0, w0 = frame.shape[:2]
    max_side = 900
    scale = min(max_side / max(h0, w0), 1.0)
    if scale < 1.0:
        frame = cv2.resize(frame, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_AREA)

    result = frame.copy()
    h, w = frame.shape[:2]

    # Wider ROI: many phone photos do not place the flower perfectly in center.
    roi_x1, roi_x2 = int(w * 0.05), int(w * 0.95)
    roi_y1, roi_y2 = int(h * 0.05), int(h * 0.95)
    roi = frame[roi_y1:roi_y2, roi_x1:roi_x2]
    roi_h, roi_w = roi.shape[:2]
    roi_area_total = max(roi_h * roi_w, 1)

    cv2.rectangle(result, (roi_x1, roi_y1), (roi_x2, roi_y2), (255, 255, 255), 2)

    # Light smoothing reduces tiny mask noise but keeps the overall flower shape.
    blur = cv2.GaussianBlur(roi, (5, 5), 0)
    hsv = cv2.cvtColor(blur, cv2.COLOR_BGR2HSV)

    # Multiple color masks. Chrysanthemums in the demo may be yellow, white,
    # orange, pink, or red/purple under different lighting.
    yellow = cv2.inRange(hsv, np.array([10, 35, 55]), np.array([48, 255, 255]))
    orange = cv2.inRange(hsv, np.array([5, 45, 55]), np.array([25, 255, 255]))
    white = cv2.inRange(hsv, np.array([0, 0, 145]), np.array([179, 95, 255]))
    pink_purple = cv2.inRange(hsv, np.array([125, 30, 55]), np.array([179, 255, 255]))
    red_low = cv2.inRange(hsv, np.array([0, 40, 55]), np.array([8, 255, 255]))

    color_mask = yellow | orange | white | pink_purple | red_low

    # Remove most green leaves/grass/background from the candidate mask.
    green = cv2.inRange(hsv, np.array([45, 35, 35]), np.array([95, 255, 255]))
    color_mask = cv2.bitwise_and(color_mask, cv2.bitwise_not(green))

    kernel_small = np.ones((5, 5), np.uint8)
    kernel_big = np.ones((11, 11), np.uint8)
    mask = cv2.morphologyEx(color_mask, cv2.MORPH_OPEN, kernel_small, iterations=1)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel_big, iterations=2)
    mask = cv2.dilate(mask, kernel_small, iterations=1)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    # Filter and score candidates. White backgrounds can create huge border-touching
    # contours, so those receive a penalty.
    min_area = roi_area_total * 0.003
    candidates = []
    for contour in contours:
        area = float(cv2.contourArea(contour))
        if area < min_area:
            continue
        x, y, bw, bh = cv2.boundingRect(contour)
        touches_border = x <= 2 or y <= 2 or (x + bw) >= roi_w - 2 or (y + bh) >= roi_h - 2
        area_ratio_tmp = area / roi_area_total
        score = area
        if touches_border and area_ratio_tmp > 0.40:
            score *= 0.20
        elif touches_border:
            score *= 0.65
        candidates.append((score, contour, area, (x, y, bw, bh)))

    # Fallback: if color mask is weak, use saturation/brightness objectness inside ROI.
    if not candidates:
        sat = hsv[:, :, 1]
        val = hsv[:, :, 2]
        object_mask = cv2.inRange(sat, 35, 255) & cv2.inRange(val, 55, 255)
        object_mask = cv2.bitwise_and(object_mask, cv2.bitwise_not(green))
        object_mask = cv2.morphologyEx(object_mask, cv2.MORPH_CLOSE, kernel_big, iterations=2)
        object_mask = cv2.dilate(object_mask, kernel_small, iterations=1)
        contours, _ = cv2.findContours(object_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < min_area:
                continue
            x, y, bw, bh = cv2.boundingRect(contour)
            touches_border = x <= 2 or y <= 2 or (x + bw) >= roi_w - 2 or (y + bh) >= roi_h - 2
            score = area * (0.65 if touches_border else 1.0)
            candidates.append((score, contour, area, (x, y, bw, bh)))

    perimeter = 0.0
    area = 0.0
    bbox = None
    detected = False
    if candidates:
        _, largest, area, bbox = max(candidates, key=lambda item: item[0])
        perimeter = float(cv2.arcLength(largest, True))
        area_ratio = area / roi_area_total
        detected = area_ratio >= 0.010 and perimeter >= 120
        contour_shifted = largest + np.array([[[roi_x1, roi_y1]]])
        cv2.drawContours(result, [contour_shifted], -1, (0, 255, 0) if detected else (0, 255, 255), 4)
        if bbox:
            x, y, bw, bh = bbox
            cv2.rectangle(result, (roi_x1 + x, roi_y1 + y), (roi_x1 + x + bw, roi_y1 + y + bh), (255, 180, 0), 2)
    else:
        area_ratio = 0.0

    mask_pixels = int(cv2.countNonZero(mask))
    mask_ratio = mask_pixels / roi_area_total

    # Classification uses relative size first, then perimeter as a fallback.
    # This is more stable across phone/laptop camera resolutions.
    if detected and (area_ratio >= 0.120 or perimeter >= 900):
        flower_type = "TYPE 1 - LARGE"
        quality = "Loại 1"
        price_vnd = 500000
        color = (0, 255, 0)
    elif detected and (area_ratio >= 0.060 or perimeter >= 620):
        flower_type = "TYPE 2 - MEDIUM"
        quality = "Loại 2"
        price_vnd = 400000
        color = (0, 255, 255)
    elif detected:
        flower_type = "TYPE 3 - SMALL"
        quality = "Loại 3"
        price_vnd = 300000
        color = (0, 0, 255)
    else:
        flower_type = "UNDETECTED"
        quality = "Không xác định"
        price_vnd = 0
        color = (255, 255, 255)

    confidence = 0.0
    if detected:
        confidence = min(0.99, max(0.35, area_ratio * 4.2 + min(perimeter / 2200.0, 0.35)))

    cv2.putText(result, flower_type, (20, 45), cv2.FONT_HERSHEY_SIMPLEX, 0.95, color, 3)
    cv2.putText(result, f"{price_vnd:,} VND", (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.85, color, 2)
    cv2.putText(result, f"Perimeter: {int(perimeter)} | Area: {area_ratio * 100:.1f}%", (20, 132), cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2)

    ok, buffer = cv2.imencode(".jpg", result, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    annotated_image = ""
    if ok:
        annotated_image = "data:image/jpeg;base64," + base64.b64encode(buffer).decode("utf-8")

    return {
        "detected": bool(detected),
        "flower_name": "Cúc Vàng",
        "flower_type": flower_type,
        "quality": quality,
        "price_vnd": price_vnd,
        "price_display": f"{price_vnd:,}".replace(",", ".") + " VNĐ" if price_vnd else "0 VNĐ",
        "perimeter": round(perimeter, 2),
        "area": round(area, 2),
        "area_ratio": round(area_ratio, 4),
        "mask_ratio": round(mask_ratio, 4),
        "confidence": round(confidence, 3),
        "roi": {"x1": roi_x1, "x2": roi_x2, "y1": roi_y1, "y2": roi_y2},
        "annotated_image": annotated_image,
    }




@app.errorhandler(413)
def file_too_large(_):
    return json_error("Ảnh quá lớn. Hãy giảm dung lượng ảnh hoặc tăng MAX_UPLOAD_MB.", 413)


@app.errorhandler(404)
def not_found(_):
    return json_error("Không tìm thấy endpoint API này.", 404)


@app.errorhandler(405)
def method_not_allowed(_):
    return json_error("Phương thức HTTP không hợp lệ cho endpoint này.", 405)


@app.route("/api/health", methods=["GET"])
def health():
    db_ready = False
    if client is not None and harvest_collection is not None and users_collection is not None:
        try:
            client.admin.command("ping")
            db_ready = True
        except Exception:
            db_ready = False

    web3_connected = safe_web3_connected()
    return json_ok(
        {
            "app_version": APP_VERSION,
            "database": db_ready,
            "db_name": DB_NAME,
            "collection": "harvest_records",
            "web3_connected": web3_connected,
            "blockchain_mode": BLOCKCHAIN_MODE,
            "effective_blockchain_mode": effective_blockchain_mode(),
            "blockchain_ready": blockchain_is_configured(),
            "contract_address": CONTRACT_ADDRESS if CONTRACT_ADDRESS else "",
            "time_vn": vn_time_display(),
        }
    )


def generate_farmer_id(users) -> str:
    for _ in range(25):
        farmer_id = f"FAR_{secrets.randbelow(9000) + 1000}"
        if not users.find_one({"farmer_id": farmer_id}, {"_id": 1}):
            return farmer_id
    return "FAR_" + secrets.token_hex(4).upper()


@app.route("/api/register", methods=["POST"])
def register():
    try:
        _, users = require_db()
        data = get_json_body()
        username = normalize_username(data.get("username"))
        password = clean_str(data.get("password"), max_len=128)
        fullname = clean_str(data.get("fullname"), max_len=120)
        location = clean_str(data.get("location"), max_len=180)
        phone_raw = clean_str(data.get("phone"), max_len=32)
        phone = normalize_phone(phone_raw)

        if not username or not password or not fullname or not location or not phone:
            return json_error("Vui lòng điền đầy đủ thông tin!", 400)
        if not re.fullmatch(r"[a-z0-9_]{3,30}", username):
            return json_error("Tên tài khoản chỉ gồm chữ thường, số, dấu gạch dưới và dài 3-30 ký tự.", 400)
        if len(password) < 6:
            return json_error("Mật khẩu nên có ít nhất 6 ký tự.", 400)
        if len(phone) < 9 or len(phone) > 12:
            return json_error("Số điện thoại không hợp lệ.", 400)
        if users.find_one(
            {"$or": [{"username": username}, {"phone": phone}, {"phone": phone_raw}, {"phone_raw": phone_raw}]},
            {"_id": 1, "username": 1, "phone": 1},
        ):
            return json_error("Tài khoản hoặc số điện thoại đã được sử dụng!", 409)

        users.insert_one(
            {
                "username": username,
                "password": generate_password_hash(password),
                "fullname": fullname,
                "location": location,
                "phone": phone,
                "phone_raw": phone_raw,
                "farmer_id": generate_farmer_id(users),
                "join_date": vn_now().strftime("%d/%m/%Y"),
                "created_at": utc_iso_now(),
                "schema_version": APP_VERSION,
            }
        )
        return json_ok({"message": "Đăng ký thành công!"}, 201)
    except DuplicateKeyError:
        return json_error("Tài khoản hoặc số điện thoại đã được sử dụng!", 409)
    except Exception as exc:
        logger.exception("Register error")
        return json_error(str(exc), 500)


@app.route("/api/forgot-password", methods=["POST"])
def forgot_password():
    try:
        _, users = require_db()
        data = get_json_body()
        phone_raw = clean_str(data.get("phone"), max_len=32)
        phone = normalize_phone(phone_raw)
        new_password = clean_str(data.get("new_password"), max_len=128)
        if not phone or not new_password:
            return json_error("Vui lòng truyền đủ thông tin!", 400)
        if len(new_password) < 6:
            return json_error("Mật khẩu mới nên có ít nhất 6 ký tự.", 400)

        user = users.find_one({"$or": [{"phone": phone}, {"phone_raw": phone_raw}]})
        if not user:
            return json_error("Số điện thoại chưa được đăng ký!", 404)

        users.update_one({"_id": user["_id"]}, {"$set": {"password": generate_password_hash(new_password), "password_updated_at": utc_iso_now()}})
        return json_ok({"message": "Cập nhật mật khẩu thành công!", "username": user.get("username", "")})
    except Exception as exc:
        logger.exception("Forgot password error")
        return json_error(str(exc), 500)


@app.route("/api/login", methods=["POST"])
def login():
    try:
        _, users = require_db()
        data = get_json_body()
        username = normalize_username(data.get("username"))
        password = clean_str(data.get("password"), max_len=128)
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
        logger.exception("Login error")
        return json_error(str(exc), 500)


@app.route("/api/harvest", methods=["POST"])
def add_harvest():
    inserted_id = None
    try:
        harvests, users = require_db()
        data = get_json_body()
        farmer = normalize_username(data.get("farmer"))
        ma_lo = clean_str(data.get("ma_lo"), max_len=64)
        flower_name = clean_str(data.get("flower_name"), max_len=128)
        ten_vuon = clean_str(data.get("ten_vuon"), max_len=160)
        ngay_thu = clean_str(data.get("ngay_thu"), max_len=40)
        khu_vuc = clean_str(data.get("khu_vuc"), max_len=160)
        weight = parse_positive_int(data.get("weight", 0), "Sản lượng")
        gia_ban = clean_str(data.get("gia_ban"), max_len=64)
        quality = normalize_quality(data.get("quality"))
        ghi_chu = clean_str(data.get("ghi_chu"), max_len=500)
        ai_type = clean_str(data.get("ai_type"), max_len=80)
        ai_price = clean_str(data.get("ai_price"), max_len=80)
        ai_perimeter = data.get("ai_perimeter")

        if not farmer:
            return json_error("Thiếu thông tin tài khoản farmer.", 400)
        user_doc = users.find_one({"username": farmer})
        if not user_doc:
            return json_error("Tài khoản farmer không tồn tại hoặc phiên đăng nhập đã cũ.", 404)

        farmer_display = (
            clean_str(data.get("farmer_display"), max_len=120)
            or clean_str(data.get("farmer_name"), max_len=120)
            or clean_str(user_doc.get("fullname"), max_len=120)
            or farmer
        )

        required = [ma_lo, flower_name, ten_vuon, ngay_thu, khu_vuc, gia_ban, quality]
        if not all(required):
            return json_error("Vui lòng điền đầy đủ thông tin bắt buộc.", 400)
        if weight > env_int("MAX_HARVEST_WEIGHT", 1_000_000, 1, 100_000_000):
            return json_error("Sản lượng quá lớn, vui lòng kiểm tra lại số bó.", 400)

        aliases = get_user_aliases(farmer, users)
        duplicate_query = {"ma_lo": ma_lo, "$or": [{"farmer": {"$in": aliases}}, {"farmer_display": {"$in": aliases}}, {"username": {"$in": aliases}}]}
        if harvests.find_one(duplicate_query, {"_id": 1}):
            return json_error("Mã lô này đã tồn tại trong tài khoản hiện tại. Hãy dùng mã lô khác để tránh trùng dữ liệu.", 409)

        now_iso = utc_iso_now()
        now_display = vn_time_display()
        chain_mode_initial = effective_blockchain_mode()

        doc = {
            "farmer": farmer,
            "username": farmer,
            "farmer_display": farmer_display,
            "farmer_id": user_doc.get("farmer_id", ""),
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
            "tx_hash": "",
            "blockchain_mode": chain_mode_initial,
            "blockchain_status": "creating",
            "block_number": None,
            "blockchain_error": "",
            "date": now_display,
            "recorded_at": now_iso,
            "ai_type": ai_type,
            "ai_price": ai_price,
            "ai_perimeter": ai_perimeter,
            "schema_version": APP_VERSION,
        }
        insert_result = harvests.insert_one(doc)
        inserted_id = insert_result.inserted_id

        combined_flower_type = f"{ma_lo}|{flower_name}|{ten_vuon}|{quality}|{gia_ban}"
        try:
            tx_hash_hex, chain_mode, tx_status, block_number, chain_error = send_to_blockchain(
                farmer, combined_flower_type, weight
            )
        except Exception as chain_exc:
            chain_error = str(chain_exc)
            update_fields = {
                "blockchain_mode": effective_blockchain_mode(),
                "blockchain_status": "failed",
                "blockchain_error": chain_error,
                "updated_at": utc_iso_now(),
            }
            harvests.update_one({"_id": inserted_id}, {"$set": update_fields})
            doc.update(update_fields)
            doc["_id"] = str(inserted_id)
            doc = normalize_harvest_record(doc)
            return jsonify({
                "status": "error",
                "cloud_saved": True,
                "tx_status": "failed",
                "blockchain_status": "failed",
                "message": "Phiếu đã lưu vào MongoDB nhưng Blockchain lỗi: " + chain_error,
                "record": doc,
            }), 502

        update_fields = {
            "tx_hash": tx_hash_hex,
            "blockchain_mode": chain_mode,
            "blockchain_status": tx_status,
            "block_number": block_number,
            "blockchain_error": chain_error,
            "updated_at": utc_iso_now(),
        }
        harvests.update_one({"_id": inserted_id}, {"$set": update_fields})
        doc.update(update_fields)
        doc["_id"] = str(inserted_id)
        doc = normalize_harvest_record(doc)

        if tx_status == "failed":
            return jsonify({
                "status": "error",
                "cloud_saved": True,
                "message": "Phiếu đã lưu MongoDB nhưng transaction Blockchain thất bại.",
                "tx_hash": tx_hash_hex,
                "tx_status": tx_status,
                "record": doc,
            }), 502

        return json_ok({
            "message": f"TxHash: {tx_hash_hex}",
            "tx_hash": tx_hash_hex,
            "tx_status": tx_status,
            "blockchain_status": tx_status,
            "blockchain_mode": chain_mode,
            "block_number": block_number,
            "cloud_saved": True,
            "record": doc,
        }, 201)
    except DuplicateKeyError:
        return json_error("Dữ liệu bị trùng khóa trong MongoDB. Kiểm tra username, phone hoặc mã lô.", 409)
    except ValueError as exc:
        return json_error(str(exc), 400)
    except Exception as exc:
        logger.exception("Add harvest error. inserted_id=%s", inserted_id)
        return json_error(str(exc), 500)


@app.route("/api/history", methods=["GET"])
def get_history():
    try:
        harvests, users = require_db()
        farmer = normalize_username(request.args.get("farmer"))
        if not farmer:
            return json_error("Thiếu farmer.", 400)

        limit = parse_limit(request.args.get("limit"), default=1000, max_value=3000)
        query = build_farmer_query(farmer, users)
        records = [normalize_harvest_record(r) for r in harvests.find(query).sort(SORT_LATEST).limit(limit)]
        records.sort(key=harvest_sort_key, reverse=True)

        return json_ok({
            "app_version": APP_VERSION,
            "db_name": DB_NAME,
            "collection": "harvest_records",
            "farmer": farmer,
            "aliases": get_user_aliases(farmer, users),
            "count": len(records),
            "limit": limit,
            "records": records,
        })
    except Exception as exc:
        logger.exception("History error")
        return json_error(str(exc), 500)


@app.route("/api/stats", methods=["GET"])
def get_stats():
    try:
        harvests, users = require_db()
        farmer = normalize_username(request.args.get("farmer"))
        if not farmer:
            return json_error("Thiếu farmer.", 400)

        query = build_farmer_query(farmer, users)
        projection = {"weight": 1, "quality": 1, "flower_type": 1}
        total_weight = 0
        loai1_weight = 0
        record_count = 0
        for record in harvests.find(query, projection):
            weight = to_int(record.get("weight"), 0)
            total_weight += weight
            record_count += 1
            quality = normalize_quality(record.get("quality"), clean_str(record.get("flower_type")))
            if quality == "Loại 1":
                loai1_weight += weight

        return json_ok({
            "app_version": APP_VERSION,
            "farmer": farmer,
            "aliases": get_user_aliases(farmer, users),
            "record_count": record_count,
            "total_weight": total_weight,
            "loai1_weight": loai1_weight,
        })
    except Exception as exc:
        logger.exception("Stats error")
        return json_error(str(exc), 500)


@app.route("/api/debug/latest", methods=["GET"])
def debug_latest_records():
    """Small diagnostic endpoint for demo/debugging MongoDB sync issues."""
    try:
        harvests, users = require_db()
        farmer = normalize_username(request.args.get("farmer"))
        limit = parse_limit(request.args.get("limit"), default=10, max_value=50)
        query = build_farmer_query(farmer, users) if farmer else {}
        records = [normalize_harvest_record(r) for r in harvests.find(query).sort(SORT_LATEST).limit(limit)]
        records.sort(key=harvest_sort_key, reverse=True)
        return json_ok({
            "app_version": APP_VERSION,
            "db_name": DB_NAME,
            "collection": "harvest_records",
            "farmer": farmer,
            "aliases": get_user_aliases(farmer, users) if farmer else [],
            "returned_records": len(records),
            "records": records,
        })
    except Exception as exc:
        logger.exception("Debug latest error")
        return json_error(str(exc), 500)


@app.route("/api/debug/find", methods=["GET"])
def debug_find_record():
    try:
        harvests, _ = require_db()
        ma_lo = clean_str(request.args.get("ma_lo"), max_len=64)
        tx_hash = clean_str(request.args.get("tx_hash"), max_len=120)
        if not ma_lo and not tx_hash:
            return json_error("Truyền ma_lo hoặc tx_hash để kiểm tra.", 400)
        query = {"$or": []}
        if ma_lo:
            query["$or"].append({"ma_lo": ma_lo})
        if tx_hash:
            query["$or"].append({"tx_hash": tx_hash})
        records = [normalize_harvest_record(r) for r in harvests.find(query).sort(SORT_LATEST).limit(50)]
        records.sort(key=harvest_sort_key, reverse=True)
        return json_ok({"count": len(records), "records": records})
    except Exception as exc:
        logger.exception("Debug find error")
        return json_error(str(exc), 500)


@app.route("/api/tx-status/<tx_hash>", methods=["GET"])
def get_tx_status(tx_hash):
    try:
        tx_hash = clean_str(tx_hash, max_len=120)
        if not tx_hash:
            return json_error("Thiếu tx_hash.", 400)

        if tx_hash.startswith("0xmock"):
            return json_ok({"tx_hash": tx_hash, "tx_status": "confirmed", "blockchain_status": "confirmed", "blockchain_mode": "mock"})

        if w3 is None or not safe_web3_connected():
            return json_error("Không kết nối được Web3 RPC.", 503)

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return json_ok({"tx_hash": tx_hash, "tx_status": "pending", "blockchain_status": "pending", "blockchain_mode": "real"})

        if receipt is None:
            return json_ok({"tx_hash": tx_hash, "tx_status": "pending", "blockchain_status": "pending", "blockchain_mode": "real"})

        tx_status = "confirmed" if int(receipt.get("status", 0)) == 1 else "failed"
        block_number = receipt.get("blockNumber")

        if harvest_collection is not None:
            harvest_collection.update_one(
                {"tx_hash": tx_hash},
                {"$set": {"blockchain_status": tx_status, "block_number": block_number, "updated_at": utc_iso_now()}},
            )

        return json_ok({
            "tx_hash": tx_hash,
            "tx_status": tx_status,
            "blockchain_status": tx_status,
            "block_number": block_number,
            "blockchain_mode": "real",
        })
    except Exception as exc:
        logger.exception("Tx status error")
        return json_error(str(exc), 500)


def extract_image_bytes_from_request() -> tuple[bytes, str]:
    """Accept image from multipart, JSON base64, raw image body, or any uploaded file field.

    This avoids the common demo bug where frontend sends the image but backend only
    looks for one exact field name.
    """
    if request.files:
        file_storage = request.files.get("image")
        if file_storage is None:
            file_storage = next(iter(request.files.values()))
        image_bytes = file_storage.read()
        return image_bytes or b"", f"multipart:{file_storage.name or 'unknown'}"

    content_type = (request.content_type or "").lower()
    if "application/json" in content_type:
        data = get_json_body()
        image_base64 = ""
        for key in ("image_base64", "image", "imageData", "dataUrl", "data_url"):
            image_base64 = clean_str(data.get(key))
            if image_base64:
                break
        if image_base64:
            if "," in image_base64:
                image_base64 = image_base64.split(",", 1)[1]
            image_base64 = image_base64.strip()
            try:
                return base64.b64decode(image_base64, validate=False), "json-base64"
            except Exception as exc:
                raise ValueError("Chuỗi base64 ảnh không hợp lệ: " + str(exc))

    raw = request.get_data() or b""
    if raw and ("image/" in content_type or raw[:2] == b"\xff\xd8" or raw[:8] == b"\x89PNG\r\n\x1a\n"):
        return raw, "raw-body"

    return b"", "none"




@app.route("/api/classify-flower", methods=["POST", "OPTIONS"])
def classify_flower():
    if request.method == "OPTIONS":
        return json_ok({"message": "preflight ok"})

    try:
        image_bytes, source = extract_image_bytes_from_request()
        if not image_bytes:
            return json_error(
                "Backend không nhận được ảnh. Frontend phải gửi multipart field 'image' hoặc JSON image_base64.",
                400,
            )

        result = classify_flower_image(image_bytes)
        result["input_source"] = source
        result["input_size_bytes"] = len(image_bytes)
        return json_ok(result)
    except ValueError as exc:
        return json_error("AI backend không phân tích được hình ảnh: " + str(exc), 400)
    except Exception as exc:
        logger.exception("Classify flower error")
        return json_error("AI backend không phân tích được hình ảnh: " + str(exc), 500)


@app.route("/api/weather", methods=["GET"])
def get_weather():
    weather_conditions = [
        {"day": "Hôm nay", "temp": "28°C", "humidity": "65%", "status": "Trời nắng đẹp", "recommendation": "Rất thuận lợi để thu hoạch cúc đại đóa. Hoa sẽ đạt phẩm chất màu sắc tốt nhất.", "icon": "☀️", "color": "#059669"},
        {"day": "Ngày mai", "temp": "33°C", "humidity": "50%", "status": "Nắng gắt", "recommendation": "Nên thu hoạch vào sáng sớm hoặc chiều mát. Tránh khung giờ trưa để hoa không bị héo nát.", "icon": "🌤️", "color": "#d97706"},
        {"day": "Ngày kia", "temp": "24°C", "humidity": "88%", "status": "Mưa rào rải rác", "recommendation": "Cân nhắc hoãn thu hoạch. Hoa dính nước mưa dễ bị úng và nấm mốc khi đóng gói vận chuyển.", "icon": "🌧️", "color": "#ef4444"},
    ]
    return json_ok({"forecast": weather_conditions})


if __name__ == "__main__":
    port = env_int("PORT", 5000, 1, 65535)
    app.run(host="0.0.0.0", port=port)
