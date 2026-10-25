"""
Routes khu vực ADMIN.

Mọi route dưới đây đều có cả @login_required (Flask-Login: phải đăng nhập)
và @admin_required (app/admin/decorators.py: phải là admin) — 2 lớp này
chính là phần chống Broken Access Control (BAC) của module bảo mật.

Hành động thay đổi dữ liệu (toggle quyền, ban, xóa) đều dùng POST + CSRF
token (Flask-WTF CSRFProtect đã bật global trong app/extensions.py), KHÔNG
dùng GET — để tránh bị gọi "nhầm" qua link/ảnh (CSRF qua GET là lỗi hay gặp).
"""
import os
import re
from flask import render_template, request, redirect, url_for, flash, jsonify, abort, send_file
from markupsafe import Markup, escape

from flask_login import login_required, current_user

from app.admin import admin_bp
from app.admin.decorators import admin_required
from app.extensions import db
from app.models.user import User
from app.models.post import Post
from app.models.photo import Photo
from app.models.comment import Comment
from app.models.itinerary import Itinerary
from app.models.security_log import SecurityLog
from app.models.chat import Conversation, Message
from app.deletion import delete_post_cascade, delete_itinerary_cascade
from app.notifications import notify_moderation
from app.timeutil import to_vn


def _vn_fmt(dt, fmt="%d/%m/%Y %H:%M"):
    """DB lưu giờ UTC (datetime.utcnow) nhưng admin xem ở giờ Việt Nam (UTC+7) -> LUÔN cộng 7
    tiếng trước khi strftime cho các JSON trả về modal xem nhanh (bảng HTML dùng filter Jinja
    'vn_dt' đăng ký ở app/__init__.py, chỗ này là bản tương đương dùng trong code Python)."""
    return to_vn(dt).strftime(fmt) if dt else "—"


# ---------------------------------------------------------------- Dashboard
@admin_bp.route("/")
@login_required
@admin_required
def dashboard():
    stats = {
        "total_users": User.query.count(),
        "total_admins": User.query.filter_by(is_admin=True).count(),
        "total_banned": User.query.filter_by(is_banned=True).count(),
        "total_posts": Post.query.count(),
    }
    return render_template("admin/dashboard.html", stats=stats)


# -------------------------------------------------------------- Quản lý User
@admin_bp.route("/users")
@login_required
@admin_required
def users():
    q = request.args.get("q", "").strip()
    query = User.query
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(User.username.ilike(like), User.email.ilike(like))
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(User.id.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template("admin/users.html", users=pagination.items, pagination=pagination, q=q)


@admin_bp.route("/users/<int:user_id>/toggle-admin", methods=["POST"])
@login_required
@admin_required
def toggle_admin(user_id):
    target = User.query.get_or_404(user_id)
    if target.id == current_user.id:
        flash("Không thể tự bỏ quyền admin của chính mình (tránh tự khóa quyền truy cập).", "warning")
        return redirect(url_for("admin.users"))

    target.is_admin = not target.is_admin
    db.session.commit()
    flash(
        f"Đã {'cấp' if target.is_admin else 'gỡ'} quyền admin cho {target.username}.",
        "success",
    )
    return redirect(request.referrer or url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/toggle-ban", methods=["POST"])
@login_required
@admin_required
def toggle_ban(user_id):
    target = User.query.get_or_404(user_id)
    if target.id == current_user.id:
        flash("Không thể tự khóa tài khoản của chính mình.", "warning")
        return redirect(url_for("admin.users"))

    target.is_banned = not target.is_banned
    db.session.commit()
    flash(
        f"Đã {'khóa' if target.is_banned else 'mở khóa'} tài khoản {target.username}.",
        "success",
    )
    return redirect(request.referrer or url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id):
    target = User.query.get_or_404(user_id)
    if target.id == current_user.id:
        flash("Không thể tự xóa tài khoản của chính mình.", "warning")
        return redirect(url_for("admin.users"))

    username = target.username
    db.session.delete(target)
    db.session.commit()
    flash(f"Đã xóa tài khoản {username}.", "success")
    return redirect(url_for("admin.users"))


# -------------------------------------------------------------- Quản lý Post
@admin_bp.route("/posts")
@login_required
@admin_required
def posts():
    q = request.args.get("q", "").strip()
    query = Post.query.join(User, Post.user_id == User.id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Post.caption.ilike(like), User.username.ilike(like))
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Post.id.desc()).paginate(page=page, per_page=20, error_out=False)

    # Post không có sẵn relationship tới User (xem app/models/post.py) -> tra cứu
    # username theo user_id bằng 1 dict, tránh N+1 query trong template.
    author_ids = {p.user_id for p in pagination.items}
    authors = {u.id: u for u in User.query.filter(User.id.in_(author_ids)).all()} if author_ids else {}

    post_ids = [p.id for p in pagination.items]
    # Ảnh đại diện (ảnh đầu tiên) của mỗi post -> 1 query, tránh N+1.
    thumbs = {}
    if post_ids:
        for ph in Photo.query.filter(Photo.post_id.in_(post_ids)).order_by(Photo.post_id, Photo.id).all():
            thumbs.setdefault(ph.post_id, ph)
    # Số bình luận của mỗi post -> 1 query GROUP BY, tránh N+1.
    comment_counts = {}
    if post_ids:
        rows = (
            db.session.query(Comment.post_id, db.func.count(Comment.id))
            .filter(Comment.post_id.in_(post_ids))
            .group_by(Comment.post_id)
            .all()
        )
        comment_counts = dict(rows)

    return render_template(
        "admin/posts.html", posts=pagination.items, pagination=pagination, q=q,
        authors=authors, thumbs=thumbs, comment_counts=comment_counts,
    )


@admin_bp.route("/posts/<int:post_id>/toggle-hide", methods=["POST"])
@login_required
@admin_required
def toggle_hide_post(post_id):
    """Khóa/mở khóa bài viết — ẩn khỏi mọi người (trừ admin & chính chủ) mà KHÔNG xóa dữ liệu.
    Dùng khi nghi ngờ vi phạm nhưng chưa cần xóa vĩnh viễn (có thể mở khóa lại nếu xét sai).
    Chỉ báo cho chủ bài khi KHÓA (not khi mở khóa lại)."""
    post = Post.query.get_or_404(post_id)
    post.is_hidden = not post.is_hidden
    if post.is_hidden:
        notify_moderation(post.user_id, current_user.id, "post_locked")
    db.session.commit()
    flash(f"Đã {'khóa' if post.is_hidden else 'mở khóa'} bài viết #{post_id}.", "success")
    return redirect(request.referrer or url_for("admin.posts"))


@admin_bp.route("/posts/<int:post_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_post(post_id):
    post = Post.query.get_or_404(post_id)
    owner_id = post.user_id
    delete_post_cascade(post)
    # post_id=None (bắt buộc) để thông báo không bị cascade "delete-orphan" xóa theo bài vừa xóa.
    notify_moderation(owner_id, current_user.id, "post_removed")
    db.session.commit()
    flash(f"Đã xóa bài viết #{post_id} và thông báo cho người dùng.", "success")
    return redirect(request.referrer or url_for("admin.posts"))


def _photo_url(ph):
    return url_for("static", filename=ph.file_path.replace("static/", ""))


@admin_bp.route("/posts/<int:post_id>/detail")
@login_required
@admin_required
def post_detail(post_id):
    """JSON chi tiết 1 post (ảnh/video đầy đủ + nội dung + danh sách bình luận) để hiện
    trong modal xem nhanh — cho phép admin đọc và xóa từng bình luận ngay tại đây."""
    post = Post.query.get_or_404(post_id)
    author = db.session.get(User, post.user_id)
    photos = Photo.query.filter_by(post_id=post.id).order_by(Photo.id).all()
    cmts = Comment.query.filter_by(post_id=post.id).order_by(Comment.created_at.asc()).all()
    cmt_authors = _users_by_id({c.user_id for c in cmts})
    return jsonify({
        "id": post.id,
        "caption": post.caption or "",
        "caption_html": str(linkify_comment(post.caption)) if post.caption else "",
        "author": author.username if author else f"user#{post.user_id}",
        "visibility": post.visibility,
        "is_hidden": post.is_hidden,
        "created_at": _vn_fmt(post.created_at),
        "comment_count": len(cmts),
        "media": [
            {"url": _photo_url(ph), "type": ph.media_type} for ph in photos
        ],
        "comments": [_comment_json(c, cmt_authors) for c in cmts],
    })


def _users_by_id(ids):
    ids = {i for i in ids if i}
    return {u.id: u for u in User.query.filter(User.id.in_(ids)).all()} if ids else {}


def _comment_json(c, authors_by_id):
    author = authors_by_id.get(c.user_id)
    return {
        "id": c.id,
        "author": author.username if author else f"user#{c.user_id}",
        "content": c.content,
        # HTML đã escape + biến URL thành link bấm được (dùng chung hàm linkify_comment ở dưới) —
        # để admin kiểm tra link spam/độc hại người dùng dán trong bình luận mà không phải đoán.
        "content_html": str(linkify_comment(c.content)) if c.content else "",
        "created_at": _vn_fmt(c.created_at),
        "is_reply": bool(c.parent_id),
    }


# --------------------------------------------------------- Quản lý Lịch trình
@admin_bp.route("/itineraries")
@login_required
@admin_required
def itineraries():
    q = request.args.get("q", "").strip()
    query = Itinerary.query.join(User, Itinerary.user_id == User.id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Itinerary.title.ilike(like), User.username.ilike(like))
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Itinerary.id.desc()).paginate(page=page, per_page=20, error_out=False)

    author_ids = {it.user_id for it in pagination.items}
    authors = {u.id: u for u in User.query.filter(User.id.in_(author_ids)).all()} if author_ids else {}

    it_ids = [it.id for it in pagination.items]
    comment_counts = {}
    if it_ids:
        rows = (
            db.session.query(Comment.itinerary_id, db.func.count(Comment.id))
            .filter(Comment.itinerary_id.in_(it_ids))
            .group_by(Comment.itinerary_id)
            .all()
        )
        comment_counts = dict(rows)

    return render_template(
        "admin/itineraries.html", itineraries=pagination.items, pagination=pagination, q=q,
        authors=authors, comment_counts=comment_counts,
    )


@admin_bp.route("/itineraries/<int:itinerary_id>/toggle-hide", methods=["POST"])
@login_required
@admin_required
def toggle_hide_itinerary(itinerary_id):
    it = Itinerary.query.get_or_404(itinerary_id)
    it.is_hidden = not it.is_hidden
    if it.is_hidden:
        notify_moderation(it.user_id, current_user.id, "itinerary_locked")
    db.session.commit()
    flash(f"Đã {'khóa' if it.is_hidden else 'mở khóa'} lịch trình #{itinerary_id}.", "success")
    return redirect(request.referrer or url_for("admin.itineraries"))


@admin_bp.route("/itineraries/<int:itinerary_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_itinerary(itinerary_id):
    it = Itinerary.query.get_or_404(itinerary_id)
    owner_id = it.user_id
    delete_itinerary_cascade(it)
    notify_moderation(owner_id, current_user.id, "itinerary_removed")
    db.session.commit()
    flash(f"Đã xóa lịch trình #{itinerary_id} và thông báo cho người dùng.", "success")
    return redirect(request.referrer or url_for("admin.itineraries"))


@admin_bp.route("/itineraries/<int:itinerary_id>/detail")
@login_required
@admin_required
def itinerary_detail(itinerary_id):
    """JSON chi tiết 1 lịch trình (ảnh bìa + mô tả + danh sách bình luận) để hiện trong modal
    xem nhanh — cho phép admin đọc và xóa từng bình luận ngay tại đây."""
    it = Itinerary.query.get_or_404(itinerary_id)
    author = db.session.get(User, it.user_id)
    cover_url = None
    if it.cover_image:
        cover_url = url_for("static", filename=it.cover_image.replace("static/", ""))
    cmts = Comment.query.filter_by(itinerary_id=it.id).order_by(Comment.created_at.asc()).all()
    cmt_authors = _users_by_id({c.user_id for c in cmts})

    # Tệp đính kèm (PDF/doc lịch trình người dùng up) — dùng chung hàm kiểm tra đường dẫn hợp lệ
    # của app/plans/routes.py, nhưng tải qua route riêng của admin (admin.itinerary_attachment)
    # vì route công khai plans.attachment đòi quyền "xem được lịch trình" — admin không phải chủ
    # và lịch trình có thể đang bị khóa (is_hidden) nên sẽ bị chặn nếu dùng lại route đó.
    from app.plans.routes import _attachment_path
    attachment_url, attachment_name = None, None
    full_path = _attachment_path(it)
    if full_path:
        attachment_name = re.sub(r"^[0-9a-f]{12}_", "", os.path.basename(full_path)) or os.path.basename(full_path)
        attachment_url = url_for("admin.itinerary_attachment", itinerary_id=it.id)

    return jsonify({
        "id": it.id,
        "title": it.title or "",
        "destination": it.destination or "",
        "description": it.description or "",
        "description_html": str(linkify_comment(it.description)) if it.description else "",
        "cover_url": cover_url,
        "author": author.username if author else f"user#{it.user_id}",
        "visibility": it.visibility,
        "is_hidden": it.is_hidden,
        "created_at": _vn_fmt(it.created_at),
        "total_days": it.total_days,
        "comment_count": len(cmts),
        "comments": [_comment_json(c, cmt_authors) for c in cmts],
        "attachment_url": attachment_url,
        "attachment_name": attachment_name,
    })


@admin_bp.route("/itineraries/<int:itinerary_id>/attachment")
@login_required
@admin_required
def itinerary_attachment(itinerary_id):
    """Tải tệp đính kèm của lịch trình — dành riêng cho admin kiểm duyệt (bỏ qua kiểm tra
    "có quyền xem lịch trình" của app/plans/routes.py:attachment, vì admin vốn không phải
    chủ và lịch trình có thể đang bị khóa/is_hidden)."""
    from app.plans.routes import _attachment_path

    it = Itinerary.query.get_or_404(itinerary_id)
    full = _attachment_path(it)
    if not full:
        abort(404)
    name = re.sub(r"^[0-9a-f]{12}_", "", os.path.basename(full)) or os.path.basename(full)
    resp = send_file(full, as_attachment=True, download_name=name, max_age=0)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


_URL_RE = re.compile(r'(https?://[^\s<>"\')]+)')


@admin_bp.app_template_filter("linkify")
def linkify_comment(text):
    """Hiện URL trong bình luận thành link bấm được (chỉ dùng ở trang Quản lý Bình luận) để
    admin kiểm tra nhanh link spam/độc hại mà không phải đọc raw text. Escape TRƯỚC, chỉ chèn
    thẻ <a> sau khi đã escape toàn bộ, nên không mở lại lỗ XSS dù nội dung là do người dùng gõ."""
    if not text:
        return ""
    safe = str(escape(text))
    linked = _URL_RE.sub(
        lambda m: f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer nofollow" class="text-info">{m.group(1)}</a>',
        safe,
    )
    return Markup(linked)


# --------------------------------------------------------- Quản lý Bình luận
@admin_bp.route("/comments")
@login_required
@admin_required
def comments():
    q = request.args.get("q", "").strip()
    query = Comment.query.join(User, Comment.user_id == User.id)
    if q:
        like = f"%{q}%"
        query = query.filter(
            db.or_(Comment.content.ilike(like), User.username.ilike(like))
        )
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Comment.id.desc()).paginate(page=page, per_page=30, error_out=False)

    author_ids = {c.user_id for c in pagination.items}
    authors = {u.id: u for u in User.query.filter(User.id.in_(author_ids)).all()} if author_ids else {}

    return render_template(
        "admin/comments.html", comments=pagination.items, pagination=pagination, q=q, authors=authors
    )


@admin_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_comment(comment_id):
    """Xóa bình luận (kiểm duyệt) — xóa bình luận gốc thì xóa luôn các trả lời (1 cấp lồng),
    cùng logic với route xóa bình luận của chính chủ ở app/locations/routes.py.
    Hỗ trợ cả gọi thường (redirect, dùng ở trang /admin/comments) lẫn gọi AJAX từ modal xem
    nhanh bài đăng/lịch trình (trả JSON, không rời trang) — phân biệt qua header X-Requested-With."""
    comment = Comment.query.get_or_404(comment_id)
    reply_ids = []
    if not comment.parent_id:
        reply_ids = [r.id for r in Comment.query.filter_by(parent_id=comment.id).all()]
        Comment.query.filter_by(parent_id=comment.id).delete()
    deleted_ids = [comment.id] + reply_ids
    db.session.delete(comment)
    db.session.commit()

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"deleted_ids": deleted_ids, "deleted_count": len(deleted_ids)})

    flash(f"Đã xóa {len(deleted_ids)} bình luận.", "success")
    return redirect(request.referrer or url_for("admin.comments"))


# --------------------------------------------------------- Quản lý Tin nhắn
@admin_bp.route("/messages")
@login_required
@admin_required
def messages():
    """Danh sách hội thoại 1-1: 2 người, tổng số tin nhắn, tổng số ảnh/video đã gửi.
    KHÔNG hiện nội dung tin nhắn ở đây — chỉ số liệu. Bấm vào 1 hội thoại mới xem full nội dung
    (route conversation_detail), để hạn chế việc đọc tin nhắn riêng tư khi không cần thiết."""
    q = request.args.get("q", "").strip()
    query = Conversation.query
    if q:
        like = f"%{q}%"
        matching_ids = [u.id for u in User.query.filter(User.username.ilike(like)).all()]
        if matching_ids:
            query = query.filter(
                db.or_(Conversation.user_a_id.in_(matching_ids), Conversation.user_b_id.in_(matching_ids))
            )
        else:
            query = query.filter(Conversation.id == -1)

    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Conversation.last_message_at.desc()).paginate(page=page, per_page=20, error_out=False)

    conv_ids = [c.id for c in pagination.items]
    user_ids = set()
    for c in pagination.items:
        user_ids.add(c.user_a_id)
        user_ids.add(c.user_b_id)
    users_by_id = {u.id: u for u in User.query.filter(User.id.in_(user_ids)).all()} if user_ids else {}

    msg_counts, media_counts = {}, {}
    if conv_ids:
        rows = (
            db.session.query(Message.conversation_id, db.func.count(Message.id))
            .filter(Message.conversation_id.in_(conv_ids))
            .group_by(Message.conversation_id)
            .all()
        )
        msg_counts = dict(rows)
        media_rows = (
            db.session.query(Message.conversation_id, db.func.count(Message.id))
            .filter(Message.conversation_id.in_(conv_ids), Message.file_kind.in_(["image", "video"]))
            .group_by(Message.conversation_id)
            .all()
        )
        media_counts = dict(media_rows)

    return render_template(
        "admin/messages.html", conversations=pagination.items, pagination=pagination, q=q,
        users_by_id=users_by_id, msg_counts=msg_counts, media_counts=media_counts,
    )


@admin_bp.route("/messages/<int:conversation_id>/detail")
@login_required
@admin_required
def conversation_detail(conversation_id):
    """JSON toàn bộ tin nhắn của 1 hội thoại — chỉ gọi khi admin chủ động bấm xem 1 hội thoại cụ thể
    (không tải sẵn nội dung ở trang danh sách) để hạn chế lộ nội dung riêng tư ngoài nhu cầu kiểm duyệt."""
    conv = Conversation.query.get_or_404(conversation_id)
    user_a = db.session.get(User, conv.user_a_id)
    user_b = db.session.get(User, conv.user_b_id)
    msgs = Message.query.filter_by(conversation_id=conv.id).order_by(Message.created_at.asc()).all()
    senders = {u.id: u for u in (user_a, user_b) if u}

    def _msg_json(m):
        sender = senders.get(m.sender_id)
        return {
            "id": m.id,
            "sender": sender.username if sender else f"user#{m.sender_id}",
            "body": m.body or "",
            "file_name": m.file_name,
            "file_kind": m.file_kind,
            "file_url": url_for("admin.message_file", message_id=m.id) if (m.file_path and not m.is_deleted) else None,
            "is_deleted": bool(m.is_deleted),
            "created_at": _vn_fmt(m.created_at),
        }

    return jsonify({
        "id": conv.id,
        "user_a": user_a.username if user_a else f"user#{conv.user_a_id}",
        "user_b": user_b.username if user_b else f"user#{conv.user_b_id}",
        "status": conv.status,
        "messages": [_msg_json(m) for m in msgs],
    })


@admin_bp.route("/messages/file/<int:message_id>")
@login_required
@admin_required
def message_file(message_id):
    """Tải/xem file đính kèm trong tin nhắn — dành riêng cho admin kiểm duyệt (bỏ qua kiểm
    tra 'phải là thành viên hội thoại' của app/chat/routes.py:download_file, vì admin vốn
    không phải thành viên của bất kỳ hội thoại nào)."""
    from app.chat.routes import _upload_dir, _safe_ext, IMAGE_TYPES, VIDEO_TYPES

    msg = Message.query.get_or_404(message_id)
    if not msg.file_path:
        abort(404)
    path = os.path.join(_upload_dir(), os.path.basename(msg.file_path))
    if not os.path.isfile(path):
        abort(404)
    ext = _safe_ext(msg.file_path)
    inline_type = IMAGE_TYPES.get(ext) or VIDEO_TYPES.get(ext)
    resp = send_file(
        path,
        mimetype=inline_type or "application/octet-stream",
        as_attachment=inline_type is None,
        download_name=msg.file_name or "tep",
        conditional=True,
        max_age=0,
    )
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ------------------------------------------------------ Nhật ký An ninh (Phát)
EVENT_TYPE_LABELS = {
    "DDOS_RATE_LIMIT": "DDoS / Rate Limit",
    "BRUTE_FORCE_AUTH": "Brute-force đăng nhập",
    "SQLI_DETECTED": "SQL Injection",
    "XSS_DETECTED": "XSS",
    "ACCESS_DENIED_403": "Truy cập bị từ chối (403)",
    "USER_REGISTER": "Đăng ký tài khoản",
    "USER_LOGIN": "Đăng nhập",
    "POST_CREATED": "Đăng bài viết",
    "COMMENT_CREATED": "Bình luận",
    "WATCHED_ACTION": "Hoạt động tài khoản theo dõi",
}


@admin_bp.route("/security-logs")
@login_required
@admin_required
def security_logs():
    from datetime import datetime, timedelta

    event_type = request.args.get("event_type", "").strip()
    query = SecurityLog.query
    if event_type in EVENT_TYPE_LABELS:
        query = query.filter_by(event_type=event_type)

    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(SecurityLog.timestamp.desc()).paginate(page=page, per_page=25, error_out=False)

    # Người dùng gắn với mỗi dòng log (nếu lúc đó họ đã đăng nhập) -> 1 query, tránh N+1.
    log_user_ids = {log.user_id for log in pagination.items if log.user_id}
    log_users = {u.id: u for u in User.query.filter(User.id.in_(log_user_ids)).all()} if log_user_ids else {}

    # Tính mốc 00:00:00 đầu ngày theo giờ Việt Nam (UTC+7), sau đó quy đổi ngược về UTC
    # để so sánh với cột SecurityLog.timestamp (lưu UTC). Nếu lấy thẳng utcnow().replace(hour=0)
    # sẽ bị lệch mốc sang 07:00 sáng giờ Việt Nam.
    vn_now = to_vn(datetime.utcnow())
    today_start = vn_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=7)
    today_q = SecurityLog.query.filter(SecurityLog.timestamp >= today_start)
    kpi = {
        "total_today": today_q.count(),
        "rate_limit_today": today_q.filter_by(event_type="DDOS_RATE_LIMIT").count(),
        "suspicious_today": today_q.filter(
            SecurityLog.event_type.in_(["SQLI_DETECTED", "XSS_DETECTED"])
        ).count(),
    }
    top_ips = (
        db.session.query(SecurityLog.ip_address, db.func.count(SecurityLog.id).label("cnt"))
        .filter(SecurityLog.timestamp >= today_start)
        .group_by(SecurityLog.ip_address)
        .order_by(db.text("cnt DESC"))
        .limit(3)
        .all()
    )

    return render_template(
        "admin/security_logs.html",
        logs=pagination.items,
        pagination=pagination,
        kpi=kpi,
        top_ips=top_ips,
        event_type=event_type,
        event_labels=EVENT_TYPE_LABELS,
        log_users=log_users,
    )


@admin_bp.route("/security-logs/clear", methods=["POST"])
@login_required
@admin_required
def clear_security_logs():
    """Xóa nhật ký DEMO — dùng để dọn sạch log trước khi chạy lại kịch bản tấn công mẫu
    trước mặt giảng viên (đề xuất của Phát, mục 4.3). KHÔNG dùng để xóa bằng chứng thật."""
    count = SecurityLog.query.delete()
    db.session.commit()
    flash(f"Đã xóa {count} dòng nhật ký (demo reset).", "success")
    return redirect(url_for("admin.security_logs"))
