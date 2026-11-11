import os
import secrets
import warnings
from dotenv import load_dotenv

load_dotenv()


def _secret(name):
    """Đọc secret từ .env. Nếu thiếu thì sinh khóa ngẫu nhiên cho phiên chạy này
    (chỉ phù hợp khi dev: session sẽ mất mỗi lần khởi động lại) thay vì dùng một
    giá trị cố định nằm trong mã nguồn công khai."""
    value = os.environ.get(name)
    if not value:
        warnings.warn(f"{name} chưa được đặt trong .env — đang dùng khóa ngẫu nhiên tạm thời.")
        value = secrets.token_hex(32)
    return value


class Config:
    APP_NAME = "GetApps"
    SECRET_KEY = _secret("SECRET_KEY")
    JWT_SECRET_KEY = _secret("JWT_SECRET_KEY")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL") or "sqlite:///app.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    TEMPLATES_AUTO_RELOAD = True
    GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
    GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

    # ==================== CHỈ DÙNG KHI QUAY DEMO KỊCH BẢN BROKEN ACCESS CONTROL ====================
    # Bật = True (qua biến môi trường DEMO_BAC_VULNERABLE=1 trong .env) để admin_required
    # "giả vờ quên" kiểm tra quyền, tái hiện đúng lỗi Missing Function Level Access Control
    # (OWASP A01:2021) cho báo cáo/live demo. PHẢI để False (mặc định) khi nộp bài / chạy thật —
    # xem app/admin/decorators.py và README_SECURITY.md.
    DEMO_BAC_VULNERABLE = os.environ.get("DEMO_BAC_VULNERABLE", "0") == "1"

    # Rate Limiting (Phòng chống DDoS / Brute-force) — Phát (Dev commit ad98923)
    RATELIMIT_ENABLED = os.environ.get("RATELIMIT_ENABLED", "true").lower() in ("true", "1", "yes")
    RATELIMIT_DEFAULT = os.environ.get("RATELIMIT_DEFAULT", "200 per minute; 2000 per hour")
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_STRATEGY = "fixed-window"


"""
Config — đọc secret key và cấu hình từ file .env, tập trung 1 chỗ.
KHÔNG hardcode giá trị thật ở đây, luôn lấy qua os.environ.get().
"""
