"""Model USER — định nghĩa bảng người dùng.
role là nhãn tự khai (local/traveler/creator/owner/user), KHÔNG dùng để phân quyền.
phone_number là thông tin liên hệ tự nhập, KHÔNG xác thực OTP (ngoài phạm vi đồ án).
"""

from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from app.extensions import db


class User(UserMixin, db.Model):
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    full_name = db.Column(db.String(150), nullable=True)
    secondary_email = db.Column(db.String(120), unique=True, nullable=True)
    google_id = db.Column(db.String(64), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)

    role = db.Column(db.String(20), default="user")
    is_admin = db.Column(db.Boolean, nullable=False, default=False)  # QUYỀN admin thật — khác "role" (chỉ là nhãn tự khai, xem đầu file). Không có form tự đăng ký; chỉ gán qua DB/script.
    # is_banned: admin khóa tài khoản (module bảo mật - admin panel). nullable=True để lưới an toàn
    # trong app/__init__.py tự ALTER TABLE thêm cột cho DB SQLite cũ; None được coi như False ở mọi nơi dùng.
    is_banned = db.Column(db.Boolean, nullable=True, default=False)
    is_watched = db.Column(db.Boolean, nullable=True, default=False)
    avatar_url = db.Column(db.String(255), nullable=True)
    profile_bg = db.Column(db.String(255), nullable=True)   # ảnh nền khung trang cá nhân (/static/uploads/backgrounds/...); NULL = nền mặc định

    @property
    def profile_bg_safe(self):
        """Ảnh nền để in ra trang: chỉ trả về khi đúng khuôn mẫu an toàn (DB có thể bị sửa tay); ngược lại None = nền mặc định."""
        from app.profile_bg import is_safe_bg_url
        return self.profile_bg if is_safe_bg_url(self.profile_bg) else None

    @property
    def profile_bg_original(self):
        """URL ảnh gốc (chưa cắt) đi kèm ảnh nền để căn chỉnh lại; None nếu không có."""
        from app.profile_bg import original_url_for
        return original_url_for(self.profile_bg)
    bio = db.Column(db.String(255), nullable=True)
    phone_number = db.Column(db.String(20), nullable=True)
    nickname = db.Column(db.String(64), nullable=True)
    profile_tag = db.Column(db.String(64), default="#nguoidulich", nullable=True)
    tags = db.Column(db.String(255), nullable=True)
    profile_completed = db.Column(db.Boolean, nullable=False, default=False)
    is_private = db.Column(db.Boolean, nullable=False, default=False)
    last_seen = db.Column(db.DateTime, nullable=True)  # dấu hiệu Online/Offline (cập nhật qua /chat/api/poll)
    show_activity = db.Column(db.Boolean, nullable=False, default=True)  # False = ẩn chấm Online/Offline của mình

    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("google_id", name="uq_users_google_id"),
    )


    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'