"""Routes LOCATIONS — đăng bài (nhiều ảnh/video + hashtag), bảng tin Discovery,
chi tiết bài viết, like, comment, share, save, địa điểm, lịch trình.
"""

import os
from datetime import datetime
import uuid
from flask import abort, request, jsonify, current_app, render_template, redirect, url_for, flash, send_from_directory
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from app.locations import locations_bp
from app.notifications import notify
from app.mentions import display_name as mention_display_name, notify_mentions, notify_new_mentions, render_mentions, strip_leading_self_mention, suggest as suggest_mentions
from app.security_logging import log_security_event, detect_xss
from app.extensions import db, limiter
from sqlalchemy.exc import IntegrityError
from app.models import (
    Post, Photo, Like, Comment, Share, SavedPost, Location, User,
    Itinerary, ItineraryDay, ItineraryStop, ItineraryLodging, ItineraryAttachment,
    Notification,
)
from app.models.comment import CommentLike
from app.locations.utils import get_or_create_location, normalize_location_name
import app.locations.places as places
from app.locations.places import resolve_location
from app.locations.hashtag_utils import parse_hashtags, get_or_create_hashtag
from app.locations.serializers import serialize_posts
from app.timeutil import to_vn
from app.locations.maplinks import parse_map_links, attach_map_links, visible_links, POST_LIMIT
from app.locations.discovery import (
    popular_locations, suggest_users, following_ids, root_post, can_view_post,
    can_view_itinerary, profile_card,
)


IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}
VIDEO_EXTENSIONS = {"mp4", "mov", "webm"}
MAX_FILES_PER_POST = 10


def get_media_type(filename):
    if "." not in filename:
        return None

    ext = filename.rsplit(".", 1)[1].lower()

    if ext in IMAGE_EXTENSIONS:
        return "image"

    if ext in VIDEO_EXTENSIONS:
        return "video"

    return None

def user_can_access_post(post):
    """
    Quyền xem/tương tác 1 bài:
    - Bài riêng tư -> chỉ chủ bài (follower cũng không).
    - Tác giả để tài khoản riêng tư -> chỉ người đang follow (và chính chủ).
    - Repost: còn phải xem được cả bài gốc thật sự.
    """
    viewer_id = current_user.id if current_user.is_authenticated else None
    follow_set = following_ids(viewer_id) if viewer_id else set()
    return can_view_post(post, viewer_id, follow_set)


def visible_itinerary_or_404(itinerary_id):
    """Lịch trình mà người xem được phép thấy; không được phép -> 404 (không lộ là có tồn tại)."""
    from flask import abort
    it = db.session.get(Itinerary, itinerary_id)
    if it is None:
        abort(404)
    if not can_view_itinerary(it, current_user.id, following_ids(current_user.id)):
        abort(404)
    return it


# =========================================================
# POST - UPLOAD
# =========================================================

@locations_bp.route("/upload", methods=["GET", "POST"])
@login_required
@limiter.limit("10 per minute", methods=["POST"])
def create_post():
    if request.method == "GET":
        # Trang riêng đã bỏ: mở hộp "Tạo mới" (nút máy bay) ở tab Đăng bài
        return redirect(url_for("locations.feed", compose="post"))

    files = request.files.getlist("images")
    is_browser_form = request.accept_mimetypes.accept_html and not request.is_json and request.headers.get("X-Requested-With") != "XMLHttpRequest"

    if not files or files[0].filename == "":
        if is_browser_form:
            flash("Vui lòng chọn ít nhất 1 file ảnh hoặc video", "error")
            return render_template("locations/upload.html"), 400
        return jsonify({
            "error": "Thiếu ít nhất 1 file ảnh/video"
        }), 400

    if len(files) > MAX_FILES_PER_POST:
        if is_browser_form:
            flash(f"Tối đa {MAX_FILES_PER_POST} file mỗi bài viết", "error")
            return render_template("locations/upload.html"), 400
        return jsonify({
            "error": f"Tối đa {MAX_FILES_PER_POST} file mỗi bài viết"
        }), 400

    for file in files:
        if get_media_type(file.filename) is None:
            if is_browser_form:
                flash(f"File '{file.filename}' không đúng định dạng", "error")
                return render_template("locations/upload.html"), 400
            return jsonify({
                "error": f"File '{file.filename}' sai định dạng"
            }), 400


    caption = request.form.get("caption", "")

    hashtags = request.form.get(
        "hashtags",
        ""
    ).strip()

    visibility = request.form.get("visibility", "public")

    if visibility not in ("public", "private"):
        return jsonify({
            "error": "visibility phải là 'public' hoặc 'private'"
        }), 400

    # Kiểm tra hashtag TRƯỚC khi tạo bất cứ thứ gì (địa điểm, bài viết)
    hashtag_names, hashtag_error = parse_hashtags(request.form.getlist("hashtags"))
    if hashtag_error:
        return jsonify({"error": hashtag_error}), 400

    # Địa chỉ Google Maps: kiểm tra ở server (tối đa 2, đúng tên miền Google Maps) TRƯỚC khi tạo bất cứ thứ gì
    map_links, map_error = parse_map_links(request.form.get("map_links"), POST_LIMIT)
    if map_error:
        return jsonify({"error": map_error}), 400

    # Tạo/tìm địa điểm TRƯỚC khi tạo Post (resolve_location có thể commit). Chỉ nhận địa điểm trong danh mục hành chính.
    location, loc_error = resolve_location(request.form.get("location_name", ""), request.form.get("location_code", ""))
    if loc_error:
        if is_browser_form:
            flash(loc_error, "error")
            return render_template("locations/upload.html"), 400
        return jsonify({"error": loc_error}), 400

    warning = None

    extra_names = request.form.getlist("extra_location_names")
    extra_links = request.form.getlist("extra_location_links")
    extra_codes = request.form.getlist("extra_location_codes")

    if len(extra_names) > 2:
        extra_names = extra_names[:2]
        extra_links = extra_links[:2]
        extra_codes = extra_codes[:2]

        warning = (
            "Bạn đã thêm tối đa 2 địa điểm phụ, "
            "các địa điểm còn lại đã bị bỏ qua"
        )

    extra_locations = []
    for i, name in enumerate(extra_names):
        extra_loc, e_err = resolve_location(name, extra_codes[i] if i < len(extra_codes) else "")
        if e_err:
            if is_browser_form:
                flash("Địa điểm phụ: " + e_err, "error")
                return render_template("locations/upload.html"), 400
            return jsonify({"error": "Địa điểm phụ: " + e_err}), 400
        if extra_loc and extra_loc not in extra_locations and (not location or extra_loc.id != location.id):
            extra_locations.append(extra_loc)

    post = Post(
        user_id=current_user.id,
        caption=caption,
        visibility=visibility,
        location_id=location.id if location else None,
    )

    db.session.add(post)
    db.session.flush()

    post.extra_locations.extend(extra_locations)
    attach_map_links(map_links, post=post, locations=[location] + list(extra_locations))

    upload_folder = os.path.join(
        current_app.root_path,
        "static",
        "uploads"
    )

    os.makedirs(upload_folder, exist_ok=True)

    saved_paths = []

    try:
        for name in hashtag_names:
            post.hashtags.append(get_or_create_hashtag(name))

        for file in files:
            media_type = get_media_type(file.filename)

            ext = secure_filename(
                file.filename
            ).rsplit(".", 1)[1]

            unique_filename = (
                f"{uuid.uuid4().hex}.{ext}"
            )

            full_path = os.path.join(
                upload_folder,
                unique_filename
            )

            file.save(full_path)
            saved_paths.append(full_path)

            photo = Photo(
                post_id=post.id,
                file_path=f"static/uploads/{unique_filename}",
                media_type=media_type,
            )

            db.session.add(photo)

        db.session.commit()
        log_security_event(event_type="POST_CREATED", severity="INFO",
                            status_code=201, action_taken="LOGGED_ONLY") 
        # Gắn thẻ @ trong caption -> báo cho người được tag (bỏ qua người không xem được bài / chính mình)
        if notify_mentions(caption, current_user.id, current_user.id, post=post):
            db.session.commit()

    except Exception:
        db.session.rollback()

        for path in saved_paths:
            if os.path.exists(path):
                os.remove(path)

        if is_browser_form:
            flash("Có lỗi xảy ra khi lưu bài viết, vui lòng thử lại", "error")
            return render_template("locations/upload.html"), 500

        return jsonify({
            "error": "Có lỗi xảy ra khi lưu bài viết, vui lòng thử lại"
        }), 500

    if is_browser_form:
        flash("Đăng bài viết thành công!", "success")
        return redirect(url_for("locations.feed"))

    response = {
        "message": "Đăng bài thành công",
        "post_id": post.id,
        "hashtags": hashtag_names
    }

    if warning:
        response["warning"] = warning

    return jsonify(response), 201



# =========================================================
# POST - CHỈNH SỬA / XÓA (chỉ chủ bài)
# =========================================================

MAX_EDIT_NEW_MEDIA = 2          # chỉ được THÊM tối đa 2 ảnh/video bằng chức năng sửa (cả vòng đời bài)
MAX_IMAGE_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_CAPTION_LEN = 1500


def sniff_media(file_storage, media_type):
    """Kiểm tra NỘI DUNG thật của file (magic bytes), không chỉ tin vào đuôi file. Trả về True nếu khớp loại."""
    stream = file_storage.stream
    stream.seek(0)
    head = stream.read(16)
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if media_type == "image":
        if size > MAX_IMAGE_BYTES:
            return False
        return (head.startswith(b"\xff\xd8\xff") or head.startswith(b"\x89PNG\r\n\x1a\n")
                or head[:6] in (b"GIF87a", b"GIF89a"))
    if media_type == "video":
        if size > MAX_VIDEO_BYTES:
            return False
        return head[4:8] == b"ftyp" or head.startswith(b"\x1a\x45\xdf\xa3")    # mp4/mov | webm
    return False


def _post_snapshot(post):
    return (
        post.caption or "",
        post.visibility,
        post.location_id,
        tuple(sorted(l.id for l in post.extra_locations)),
        tuple(sorted(h.name for h in post.hashtags)),
        tuple((m.url, m.label or "") for m in post.map_links),
    )


def edit_data_for(post):
    """Dữ liệu điền sẵn vào form Chỉnh sửa (chỉ gửi cho chủ bài)."""
    n_media = Photo.query.filter_by(post_id=post.id).count()
    loc = db.session.get(Location, post.location_id) if post.location_id else None
    can_add = max(0, min(MAX_EDIT_NEW_MEDIA - (post.edit_media_added or 0), MAX_FILES_PER_POST - n_media))
    return {
        "id": post.id,
        "caption": post.caption or "",
        "hashtags": " ".join(sorted(h.name for h in post.hashtags)),
        "location": loc.name if loc else "",
        "extra_locations": [l.name for l in post.extra_locations],
        "map_links": [{"url": m.url, "label": m.label or ""} for m in post.map_links if m.safe_url],   # URL xấu trong DB không đưa ra form
        "visibility": post.visibility,
        "media_count": n_media,
        "can_add_media": can_add,
    }


@locations_bp.route("/posts/<int:post_id>/edit", methods=["POST"])
@login_required
def edit_post(post_id):
    """Chủ bài sửa mọi thông tin (chữ, hashtag, địa điểm, địa chỉ Maps, chế độ) + thêm tối đa 2 ảnh/video mới.
    Không sửa/xóa ảnh/video đã đăng. Bản đăng lại không tự sửa được (luôn theo bài gốc)."""
    post = db.session.get(Post, post_id)
    if post is None or not user_can_access_post(post):
        return jsonify({"error": "Bài viết không tồn tại"}), 404
    if post.user_id != current_user.id:
        return jsonify({"error": "Chỉ chủ bài mới được chỉnh sửa"}), 403
    if post.original_post_id:
        return jsonify({"error": "Bài đăng lại không chỉnh sửa được, nó luôn hiển thị theo bài gốc"}), 403

    # ---- đọc + kiểm tra TOÀN BỘ dữ liệu trước khi thay đổi bất cứ thứ gì ----
    caption = (request.form.get("caption", "") or "")
    if len(caption) > MAX_CAPTION_LEN:
        return jsonify({"error": f"Nội dung tối đa {MAX_CAPTION_LEN} ký tự"}), 400
    caption = caption.replace("\x00", "").strip()

    visibility = request.form.get("visibility", post.visibility)
    if visibility not in ("public", "private"):
        return jsonify({"error": "visibility phải là 'public' hoặc 'private'"}), 400

    hashtag_names, hashtag_error = parse_hashtags(request.form.getlist("hashtags"))
    if hashtag_error:
        return jsonify({"error": hashtag_error}), 400

    map_links, map_error = parse_map_links(request.form.get("map_links"), POST_LIMIT)
    if map_error:
        return jsonify({"error": map_error}), 400

    files = [f for f in request.files.getlist("images") if f and f.filename]
    n_media = Photo.query.filter_by(post_id=post.id).count()
    if len(files) > MAX_EDIT_NEW_MEDIA:
        return jsonify({"error": f"Chỉ được thêm tối đa {MAX_EDIT_NEW_MEDIA} ảnh/video khi chỉnh sửa"}), 400
    if (post.edit_media_added or 0) + len(files) > MAX_EDIT_NEW_MEDIA:
        return jsonify({"error": f"Bài này chỉ được thêm tối đa {MAX_EDIT_NEW_MEDIA} ảnh/video bằng chỉnh sửa "
                                 f"(đã thêm {post.edit_media_added or 0})"}), 400
    if n_media + len(files) > MAX_FILES_PER_POST:
        return jsonify({"error": f"Tối đa {MAX_FILES_PER_POST} file mỗi bài viết (hiện có {n_media})"}), 400
    prepared = []
    for f in files:
        mtype = get_media_type(f.filename)
        if mtype is None or not sniff_media(f, mtype):
            return jsonify({"error": f"File '{secure_filename(f.filename) or 'không tên'}' sai định dạng hoặc quá lớn"}), 400
        prepared.append((f, mtype))

    before = _post_snapshot(post)

    # ---- áp dụng ----
    keep = [db.session.get(Location, post.location_id) if post.location_id else None] + list(post.extra_locations)      # địa điểm cũ (gõ tay) giữ nguyên nếu người dùng không đổi chữ
    location, loc_error = resolve_location(request.form.get("location_name", ""), request.form.get("location_code", ""), keep)
    if loc_error:
        return jsonify({"error": loc_error}), 400
    extra_locations = []
    e_codes = request.form.getlist("extra_location_codes")
    for i, name in enumerate(request.form.getlist("extra_location_names")[:2]):
        loc, e_err = resolve_location(name, e_codes[i] if i < len(e_codes) else "", keep)
        if e_err:
            return jsonify({"error": "Địa điểm phụ: " + e_err}), 400
        if loc and loc not in extra_locations and (not location or loc.id != location.id):
            extra_locations.append(loc)

    post = db.session.get(Post, post_id)       # get_or_create_location có thể commit
    old_caption = post.caption or ""
    post.caption = caption
    post.visibility = visibility
    post.location_id = location.id if location else None
    post.extra_locations[:] = extra_locations
    post.hashtags[:] = [get_or_create_hashtag(n) for n in hashtag_names]
    for m in list(post.map_links):
        db.session.delete(m)
    db.session.flush()
    attach_map_links(map_links, post=post, locations=[location] + list(extra_locations))

    saved_paths = []
    upload_folder = os.path.join(current_app.root_path, "static", "uploads")
    os.makedirs(upload_folder, exist_ok=True)
    try:
        for f, mtype in prepared:
            ext = secure_filename(f.filename).rsplit(".", 1)[1].lower()
            unique = f"{uuid.uuid4().hex}.{ext}"
            full = os.path.join(upload_folder, unique)
            f.save(full)
            saved_paths.append(full)
            db.session.add(Photo(post_id=post.id, file_path=f"static/uploads/{unique}", media_type=mtype))
        post.edit_media_added = (post.edit_media_added or 0) + len(prepared)
        db.session.flush()
        db.session.refresh(post)
        changed = bool(prepared) or _post_snapshot(post) != before
        if changed:
            post.edited_at = datetime.utcnow()
        notify_new_mentions(old_caption, caption, current_user.id, post=post)      # chỉ báo người MỚI được tag
        db.session.commit()
    except Exception:
        db.session.rollback()
        for path in saved_paths:
            if os.path.exists(path):
                os.remove(path)
        return jsonify({"error": "Có lỗi xảy ra khi lưu chỉnh sửa, vui lòng thử lại"}), 500

    return jsonify({
        "message": "Đã cập nhật bài viết" if changed else "Không có thay đổi nào",
        "changed": changed,
        "edited_at": post.edited_at.isoformat() + "Z" if post.edited_at else None,
    })


@locations_bp.route("/posts/<int:post_id>/delete", methods=["POST", "DELETE"])
@login_required
def delete_post(post_id):
    """Chủ bài xóa bài của mình (kèm tim/bình luận/thông báo...). Xóa bài gốc => các bản đăng lại bị xóa theo.
    Người đăng lại chỉ xóa được bản đăng lại của chính mình."""
    post = db.session.get(Post, post_id)
    if post is None or not user_can_access_post(post):
        return jsonify({"error": "Bài viết không tồn tại"}), 404
    if post.user_id != current_user.id:
        return jsonify({"error": "Chỉ chủ bài mới được xóa"}), 403
    from app.deletion import delete_post_cascade
    ids = delete_post_cascade(post)
    return jsonify({"message": "Đã xóa bài viết", "deleted_ids": ids})


@locations_bp.route("/photos/<int:photo_id>/delete", methods=["POST", "DELETE"])
@login_required
def delete_photo(photo_id):
    """Chủ bài xóa 1 ẢNH của bài mình (từ tab Album). Chỉ ảnh, không đụng video.
    Bài không còn ảnh/video nào VÀ không có chữ -> xóa luôn bài (bài rỗng vô nghĩa); có chữ thì giữ bài."""
    photo = db.session.get(Photo, photo_id)
    post = db.session.get(Post, photo.post_id) if photo else None
    if photo is None or post is None or not user_can_access_post(post):
        return jsonify({"error": "Ảnh không tồn tại"}), 404
    if post.user_id != current_user.id:
        return jsonify({"error": "Chỉ chủ bài mới được xóa ảnh"}), 403
    if photo.media_type == "video":
        return jsonify({"error": "Chỉ xóa được ảnh ở Album"}), 400
    others = Photo.query.filter(Photo.post_id == post.id, Photo.id != photo.id).count()
    if others == 0 and not (post.caption or "").strip():
        from app.deletion import delete_post_cascade
        delete_post_cascade(post)                    # xóa cả bài (kèm file ảnh)
        return jsonify({"message": "Đã xóa ảnh và bài viết trống", "post_deleted": True, "photo_id": photo_id})
    path = photo.file_path
    db.session.delete(photo)
    db.session.commit()
    from app.deletion import remove_files_if_unused
    remove_files_if_unused([path])
    return jsonify({"message": "Đã xóa ảnh", "post_deleted": False, "photo_id": photo_id})

# =========================================================
# FEED
# =========================================================

def comment_like_map(comment_ids, viewer_id):
    """{comment_id: {"count": n, "mine": bool}} — dùng chung cho comment bài đăng lẫn lịch trình."""
    if not comment_ids:
        return {}
    rows = CommentLike.query.filter(CommentLike.comment_id.in_(comment_ids)).all()
    out = {}
    for r in rows:
        d = out.setdefault(r.comment_id, {"count": 0, "mine": False})
        d["count"] += 1
        if r.user_id == viewer_id:
            d["mine"] = True
    return out


@locations_bp.route("/api/comments/<int:comment_id>/like", methods=["POST"])
@login_required
def toggle_comment_like(comment_id):
    """Thả / bỏ tim 1 bình luận (bài đăng hoặc lịch trình đều dùng chung route này)."""
    c = db.session.get(Comment, comment_id)
    if not c:
        abort(404)
    existing = CommentLike.query.filter_by(comment_id=c.id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
    else:
        db.session.add(CommentLike(comment_id=c.id, user_id=current_user.id))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
    lk = comment_like_map([c.id], current_user.id).get(c.id, {"count": 0, "mine": False})
    return jsonify({"likes_count": lk["count"], "is_liked": lk["mine"]})


def build_comments_tree(comments, viewer_id):
    """Cây bình luận (name/content tách dòng, @mention reply, thả tim) — dùng chung cho bài đăng VÀ lịch trình."""
    cmt_likes = comment_like_map([c.id for c in comments], viewer_id)

    def _fmt(c, c_author, reply_to_author=None):
        lk = cmt_likes.get(c.id, {"count": 0, "mine": False})
        display_content = strip_leading_self_mention(c.content, reply_to_author) if reply_to_author else c.content
        return {
            "id": c.id,
            "user_id": c.user_id,
            "parent_id": c.parent_id,
            "reply_to_id": c.reply_to_id,
            "reply_to_name": mention_display_name(reply_to_author) if reply_to_author else None,
            "reply_to_username": reply_to_author.username if reply_to_author else None,
            "author_name": c_author.username if c_author else f"User #{c.user_id}",
            "author_username": c_author.username if c_author else None,
            "author_avatar": c_author.avatar_url if c_author else None,
            "content": display_content,
            "created_at": to_vn(c.created_at).strftime("%H:%M %d/%m/%Y") if c.created_at else "",
            "created_dt": c.created_at,
            "can_delete": (c.user_id == viewer_id),
            "likes_count": lk["count"],
            "is_liked": lk["mine"],
            "replies": [],
        }

    by_id = {}
    top_level = []
    for c in comments:
        c_author = db.session.get(User, c.user_id)
        reply_to_author = None
        if c.reply_to_id:
            reply_to_comment = db.session.get(Comment, c.reply_to_id)
            if reply_to_comment:
                reply_to_author = db.session.get(User, reply_to_comment.user_id)
        by_id[c.id] = _fmt(c, c_author, reply_to_author)
    for c in comments:
        node = by_id[c.id]
        if c.parent_id and c.parent_id in by_id:
            by_id[c.parent_id]["replies"].append(node)
        else:
            top_level.append(node)
    return top_level


def build_post_cards(posts, viewer_id):
    """Dữ liệu thẻ bài viết (bảng tin + tab "Bài viết" ở trang cá nhân dùng chung 1 mẫu: locations/_post_card.html).
    `posts` đã được lọc quyền xem ở nơi gọi. `is_mine` / `edit_data` chỉ bật cho chủ bài."""
    post_cards = []
    for p in posts:
        reposter = db.session.get(User, p.user_id)
        src = root_post(p) if p.original_post_id else None
        author = db.session.get(User, src.user_id) if src else reposter
        content = src or p        # bản đăng lại luôn hiển thị NỘI DUNG của bài gốc (gốc sửa thì đăng lại đổi theo)
        photos = Photo.query.filter_by(post_id=content.id).order_by(Photo.id.asc()).all()
        loc = db.session.get(Location, content.location_id) if content.location_id else None
        likes_count = Like.query.filter_by(post_id=p.id).count()
        is_liked = bool(Like.query.filter_by(user_id=viewer_id, post_id=p.id).first())
        comments = Comment.query.filter_by(post_id=p.id).order_by(Comment.created_at.asc()).all()
        comments_count = len(comments)
        is_saved = bool(SavedPost.query.filter_by(user_id=viewer_id, post_id=p.id).first())
        shares_count = Share.query.filter_by(post_id=p.id).count()
        formatted_comments = build_comments_tree(comments, viewer_id)

        post_cards.append({
            "id": p.id,
            "author": author,
            "is_repost": bool(p.original_post_id),
            "reposter": reposter if p.original_post_id else None,
            "repost_note": p.repost_note,
            "visibility": p.visibility,
            # Bài sắp được repost (gốc thật sự) là riêng tư => bản repost bị khóa ở chế độ riêng tư
            "repost_locked": ((src or p).visibility == "private"),
            "caption": content.caption,
            "location": loc,
            "is_mine": p.user_id == viewer_id,
            "edit_data": edit_data_for(p) if (p.user_id == viewer_id and not p.original_post_id) else None,
            "map_links": visible_links(src or p),   # bài repost hiển thị địa chỉ của bài gốc
            "created_at": p.created_at.strftime("%d/%m/%Y %H:%M") if p.created_at else "",
            "created_dt": p.created_at,                                  # giờ đăng (UTC) -> time_tag
            "edited_dt": None if p.original_post_id else p.edited_at,   # bài đăng lại không tự sửa được
            "src_edited_dt": src.edited_at if src else None,            # lần cập nhật gần nhất của bài gốc
            "photos": photos,
            "likes_count": likes_count,
            "is_liked": is_liked,
            "comments_count": comments_count,
            "comments": formatted_comments,
            "is_saved": is_saved,
            "shares_count": shares_count,
        })
    return post_cards


@locations_bp.route("/feed", methods=["GET"])
def feed():
    """Bảng tin Feed — Hỗ trợ song song cả Web HTML UI lẫn REST API JSON.
    Khách (chưa đăng nhập) vẫn xem được bài CÔNG KHAI (không có me_card/gợi ý theo follow);
    mọi hành động (like/comment/lưu/đăng...) vẫn bắt đăng nhập ở route riêng (data-login-gate)."""
    from app.models import Follow

    viewer_id = current_user.id if current_user.is_authenticated else None

    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 10, type=int), 1), 50)

    # 1. Trả về JSON cho Mobile / API / Automation tests
    if request.args.get("format") == "json" or (
        request.headers.get("Accept") == "application/json" and not request.accept_mimetypes.accept_html
    ):
        json_follow = following_ids(viewer_id) if viewer_id else set()
        cand = (
            Post.query.filter(Post.visibility == "public")
            .order_by(Post.created_at.desc(), Post.id.desc())
            .all()
        )
        cand = [p for p in cand if can_view_post(p, viewer_id, json_follow)]
        start = (page - 1) * per_page
        posts = cand[start:start + per_page + 1]
        has_next = len(posts) > per_page
        posts = posts[:per_page]

        return jsonify({
            "feed": serialize_posts(posts, viewer_id),
            "page": page,
            "per_page": per_page,
            "has_next": has_next,
        })

    # 2. Render Template HTML cho Trình duyệt Web
    # Bài CÔNG KHAI của bất kỳ ai cũng hiện trong bảng tin (không bắt buộc phải follow mới thấy),
    # cộng thêm bài RIÊNG TƯ của người mình đang follow (và của chính mình) -> can_view_post lọc lại lần cuối cho chắc.
    follow_set = following_ids(viewer_id) if viewer_id else set()
    feed_user_ids = list(follow_set | ({viewer_id} if viewer_id else set()))

    posts = (
        Post.query.filter(
            db.or_(Post.visibility == "public", Post.user_id.in_(feed_user_ids))
        )
        .order_by(Post.created_at.desc())
        .all()
    )
    posts = [p for p in posts if can_view_post(p, viewer_id, follow_set)]

    post_cards = build_post_cards(posts, viewer_id)

    return render_template(
        "locations/feed.html",
        posts=post_cards,
        me_card=profile_card(current_user) if viewer_id else None,
        popular_places=popular_locations(10),
        suggested_users=suggest_users(viewer_id, 5) if viewer_id else [],
    )


@locations_bp.route("/mentions/suggest", methods=["GET"])
@login_required
def mention_suggest():
    """Gợi ý người để @ trong bình luận. q rỗng: người đã theo dõi / theo dõi mình / vừa bình luận ở bài này."""
    q = (request.args.get("q") or "").strip()[:64]
    extra = []
    pid = request.args.get("post_id", type=int)
    if pid:
        post = db.session.get(Post, pid)
        if post and user_can_access_post(post):
            extra = [r[0] for r in db.session.query(Comment.user_id).filter_by(post_id=pid).distinct().limit(20).all()]
            extra.append(post.user_id)
    # Trả tối đa 200 người (người đang theo dõi xếp đầu); khung gợi ý ở trình duyệt hiện ~8 dòng rồi cuộn để xem hết
    return jsonify({"users": suggest_mentions(current_user.id, q, limit=200, extra_ids=extra)})


@locations_bp.route("/popular", methods=["GET"])
@login_required
def popular_places_api():
    """Top địa điểm nổi bật (bài đăng + lịch trình công khai) — dùng cho chip gợi ý ở form lịch trình."""
    limit = min(max(request.args.get("limit", 3, type=int), 1), 10)
    return jsonify({"places": popular_locations(limit)})


@locations_bp.route("/p/<int:post_id>", methods=["GET"])
def view_post(post_id):
    """Trang xem ĐÚNG 1 bài (link từ thông báo): không phụ thuộc bài đó có nằm trong bảng tin của người xem hay không.
    Khách xem được nếu bài là công khai (user_can_access_post đã tự xử lý viewer ẩn danh)."""
    post = db.session.get(Post, post_id)
    if not post or not user_can_access_post(post):
        abort(404)                       # không phân biệt "không tồn tại" và "không có quyền" -> không lộ bài riêng tư
    viewer_id = current_user.id if current_user.is_authenticated else None
    return render_template("locations/post_view.html", posts=build_post_cards([post], viewer_id))


@locations_bp.route("/posts/<int:post_id>", methods=["GET"])
def get_post(post_id):
    """Chi tiết 1 bài viết (thay cho route địa điểm cũ đã không còn). Khách xem được bài công khai."""
    post = db.session.get(Post, post_id)
    if not post:
        return jsonify({"error": "Bài viết không tồn tại"}), 404
    if not user_can_access_post(post):
        return jsonify({"error": "Bạn không có quyền xem bài viết này"}), 403

    viewer_id = current_user.id if current_user.is_authenticated else None
    return jsonify({"post": serialize_posts([post], viewer_id)[0]})


# =========================================================
# LIKE
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/like",
    methods=["POST"]
)
@login_required
def like_post(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền xem bài viết này"
        }), 403

    existing = Like.query.filter_by(
        user_id=current_user.id,
        post_id=post_id
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()

        return jsonify({
            "message": "Đã bỏ tim",
            "liked": False
        })

    like = Like(
        user_id=current_user.id,
        post_id=post_id
    )

    db.session.add(like)

    # Tạo thông báo Like cho chủ bài viết
    # Không gửi thông báo nếu tự like bài của mình
    notify(post.user_id, current_user.id, "like", post_id=post.id)

    # Lưu Like + Notification cùng transaction
    db.session.commit()

    return jsonify({
        "message": "Đã thả tim",
        "liked": True
    })


# =========================================================
# COMMENT - CREATE
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/comments",
    methods=["POST"]
)
@login_required
def create_comment(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền bình luận bài viết này"
        }), 403

    data = request.get_json() or {}
    content = data.get("content", "").strip()

    if detect_xss(content):
        log_security_event(event_type="XSS_DETECTED", severity="CRITICAL",
                            status_code=201, payload_snippet=content,
                            action_taken="LOGGED_ONLY")  # vẫn lưu bình thường — Jinja autoescape + escapeHtml() lo phần hiển thị an toàn

    if not content:
        return jsonify({
            "error": "Nội dung bình luận không được để trống"
        }), 400

    parent_id = data.get("parent_id")
    parent = None
    reply_to = None  # reply cụ thể đang được trả lời (nếu có) — khác parent khi trả lời-của-trả lời
    if parent_id:
        parent = db.session.get(Comment, int(parent_id))
        if not parent or parent.post_id != post_id:
            return jsonify({"error": "Bình luận gốc không hợp lệ"}), 400
        if parent.parent_id:
            # Trả lời 1 reply -> vẫn chỉ lồng 1 cấp trong DB (gắn parent_id vào bình luận gốc của reply đó),
            # nhưng lưu lại reply_to_id = chính reply vừa bị trả lời để hiện tag "@người" + báo đúng người.
            reply_to = parent
            parent_id = parent.parent_id
            parent = db.session.get(Comment, parent_id)

    comment = Comment(
        user_id=current_user.id,
        post_id=post_id,
        parent_id=parent.id if parent else None,
        reply_to_id=reply_to.id if reply_to else None,
        content=content
    )

    db.session.add(comment)
    log_security_event(event_type="COMMENT_CREATED", severity="INFO",
                        status_code=201, action_taken="LOGGED_ONLY")
    # @username trong bình luận -> thông báo "nhắc đến bạn"; chủ bài được tag thì chỉ nhận 1 thông báo (mention)
    tagged = notify_mentions(content, current_user.id, post.user_id, post=post)
    if post.user_id not in tagged:
        notify(post.user_id, current_user.id, "comment", post_id=post.id)
    # Người thực sự bị trả lời (reply_to nếu có, không thì bình luận gốc) -> báo cho đúng họ, không phải người khác
    notify_target = reply_to or parent
    if notify_target and notify_target.user_id != current_user.id and notify_target.user_id not in tagged and notify_target.user_id != post.user_id:
        notify(notify_target.user_id, current_user.id, "comment", post_id=post.id)
    db.session.commit()

    reply_to_author = db.session.get(User, reply_to.user_id) if reply_to else None
    display_content = strip_leading_self_mention(comment.content, reply_to_author) if reply_to_author else comment.content

    return jsonify({
        "message": "Bình luận thành công",
        "comment": {
            "id": comment.id,
            "user_id": comment.user_id,
            "post_id": comment.post_id,
            "parent_id": comment.parent_id,
            "reply_to_id": comment.reply_to_id,
            "reply_to_name": mention_display_name(reply_to_author) if reply_to_author else None,
            "reply_to_username": reply_to_author.username if reply_to_author else None,
            "author_username": current_user.username,
            "author_avatar": current_user.avatar_url,
            "content": display_content,
            "content_html": str(render_mentions(display_content)),
            "created_at": comment.created_at.isoformat()
        }
    }), 201


# =========================================================
# COMMENT - GET
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/comments",
    methods=["GET"]
)
@login_required
def get_comments(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền xem bài viết này"
        }), 403

    comments = (
        Comment.query
        .filter_by(post_id=post_id)
        .order_by(Comment.created_at.asc())
        .all()
    )

    return jsonify({
        "post_id": post_id,
        "comments": [
            {
                "id": comment.id,
                "user_id": comment.user_id,
                "content": comment.content,
                "content_html": str(render_mentions(comment.content)),
                "created_at": comment.created_at.isoformat()
            }
            for comment in comments
        ]
    })


# =========================================================
# COMMENT - DELETE
# =========================================================

@locations_bp.route(
    "/comments/<int:comment_id>",
    methods=["DELETE"]
)
@login_required
def delete_comment(comment_id):
    comment = db.session.get(Comment, comment_id)

    if not comment:
        return jsonify({
            "error": "Bình luận không tồn tại"
        }), 404

    if comment.user_id != current_user.id:
        return jsonify({
            "error": "Bạn không có quyền xóa bình luận này"
        }), 403

    # Xóa bình luận gốc -> xóa luôn các trả lời (chỉ 1 cấp lồng nên không cần đệ quy)
    deleted_count = 1
    if not comment.parent_id:
        deleted_count += Comment.query.filter_by(parent_id=comment.id).delete()

    db.session.delete(comment)
    db.session.commit()

    return jsonify({
        "message": "Xóa bình luận thành công",
        "deleted_count": deleted_count
    })


# =========================================================
# SHARE - DOWNLOAD
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/share/download",
    methods=["GET"]
)
@login_required
def download_post(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền tải bài viết này"
        }), 403

    photo = Photo.query.filter_by(
        post_id=post_id
    ).first()

    if not photo:
        return jsonify({
            "error": "Bài viết không có file"
        }), 404

    upload_folder = os.path.join(
        current_app.root_path,
        "static",
        "uploads"
    )

    filename = os.path.basename(
        photo.file_path
    )

    full_path = os.path.join(
        upload_folder,
        filename
    )

    if not os.path.exists(full_path):
        return jsonify({
            "error": "File không tồn tại trên server"
        }), 404

    # Chỉ ghi nhận download một lần
    existing = Share.query.filter_by(
        user_id=current_user.id,
        post_id=post_id,
        share_type="download"
    ).first()

    if not existing:
        share = Share(
            user_id=current_user.id,
            post_id=post_id,
            share_type="download"
        )

        db.session.add(share)

        # Tải về: chỉ ghi nhận lượt chia sẻ, KHÔNG báo cho chủ bài
        db.session.commit()

    return send_from_directory(
        upload_folder,
        filename,
        as_attachment=True
    )


# =========================================================
# SHARE - COPY LINK
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/share",
    methods=["POST"]
)
@login_required
def share_post(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền chia sẻ bài viết này"
        }), 403

    link = (
        f"http://127.0.0.1:5000/"
        f"locations/posts/{post_id}"
    )

    # Chỉ ghi nhận copy_link một lần
    existing = Share.query.filter_by(
        user_id=current_user.id,
        post_id=post_id,
        share_type="copy_link"
    ).first()

    if existing:
        return jsonify({
            "message": "Đã sao chép liên kết bài viết",
            "link": link,
            "share": {
                "id": existing.id,
                "user_id": existing.user_id,
                "post_id": existing.post_id,
                "share_type": existing.share_type,
                "created_at": existing.created_at.isoformat()
            }
        }), 200

    share = Share(
        user_id=current_user.id,
        post_id=post_id,
        share_type="copy_link"
    )

    db.session.add(share)

    # Copy link: chỉ ghi nhận lượt chia sẻ, KHÔNG báo cho chủ bài
    db.session.commit()

    return jsonify({
        "message": "Đã sao chép liên kết bài viết",
        "link": link,
        "share": {
            "id": share.id,
            "user_id": share.user_id,
            "post_id": share.post_id,
            "share_type": share.share_type,
            "created_at": share.created_at.isoformat()
        }
    }), 201


# =========================================================
# SAVE
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/save",
    methods=["POST"]
)
@login_required
def save_post(post_id):
    post = db.session.get(Post, post_id)

    if not post:
        return jsonify({
            "error": "Bài viết không tồn tại"
        }), 404

    if not user_can_access_post(post):
        return jsonify({
            "error": "Bạn không có quyền lưu bài viết này"
        }), 403

    existing = SavedPost.query.filter_by(
        user_id=current_user.id,
        post_id=post_id
    ).first()

    if existing:
        db.session.delete(existing)
        db.session.commit()

        return jsonify({
            "message": "Đã bỏ lưu bài viết",
            "saved": False
        })

    saved_post = SavedPost(
        user_id=current_user.id,
        post_id=post_id
    )

    db.session.add(saved_post)
    db.session.commit()

    return jsonify({
        "message": "Đã lưu bài viết",
        "saved": True
    }), 201


# =========================================================
# SHARE - REPOST
# =========================================================

@locations_bp.route(
    "/posts/<int:post_id>/repost",
    methods=["POST"]
)
@login_required
def repost_post(post_id):
    """Đăng lại bài viết (có chọn công khai/riêng tư + ghi chú).

    - Repost của repost luôn trỏ về BÀI GỐC thật sự.
    - Bài gốc riêng tư => bản đăng lại bị ép riêng tư (không thể "tẩy trắng" bài riêng tư thành công khai).
    - Người dùng phải xem được cả bài được bấm lẫn bài gốc.
    """
    clicked = db.session.get(Post, post_id)

    if not clicked:
        return jsonify({"error": "Bài viết không tồn tại"}), 404

    if not user_can_access_post(clicked):
        return jsonify({"error": "Bạn không có quyền đăng lại bài viết này"}), 403

    original_post = root_post(clicked)
    if original_post is None:
        return jsonify({"error": "Bài viết gốc không còn tồn tại"}), 404

    if not user_can_access_post(original_post):
        return jsonify({"error": "Bạn không có quyền đăng lại bài viết này"}), 403

    data = request.get_json(silent=True) or {}
    visibility = data.get("visibility", "public")
    if visibility not in ("public", "private"):
        return jsonify({"error": "visibility phải là 'public' hoặc 'private'"}), 400
    forced_private = original_post.visibility == "private"
    if forced_private:
        visibility = "private"

    note = (data.get("note") or "").strip()
    if len(note) > 500:
        return jsonify({"error": "Ghi chú tối đa 500 ký tự"}), 400

    if detect_xss(note):
        log_security_event(event_type="XSS_DETECTED", severity="CRITICAL",
                            status_code=201, payload_snippet=note,
                            action_taken="LOGGED_ONLY")  # vẫn lưu — Jinja autoescape {{ post.repost_note }} lo phần hiển thị an toàn

    # Mỗi user chỉ repost một bài gốc một lần
    existing = Share.query.filter_by(
        user_id=current_user.id,
        post_id=original_post.id,
        share_type="repost"
    ).first()

    if existing:
        return jsonify({"error": "Bạn đã đăng lại bài viết này"}), 409

    repost = Post(
        user_id=current_user.id,
        caption=original_post.caption,
        visibility=visibility,
        location_id=original_post.location_id,
        original_post_id=original_post.id,
        repost_note=note or None,
    )

    db.session.add(repost)
    db.session.flush()

    # Giữ các địa điểm phụ của bài gốc
    for location in original_post.extra_locations:
        repost.extra_locations.append(location)

    original_photos = Photo.query.filter_by(
        post_id=original_post.id
    ).all()

    for photo in original_photos:
        new_photo = Photo(
            post_id=repost.id,
            file_path=photo.file_path,
            media_type=photo.media_type
        )

        db.session.add(new_photo)

    share = Share(
        user_id=current_user.id,
        post_id=original_post.id,
        share_type="repost"
    )

    db.session.add(share)

    # Báo cho chủ bài gốc: "X đã đăng lại bài viết của bạn" — thông báo trỏ tới BÀI ĐĂNG LẠI (repost.id) chứ không phải bài gốc
    # (không báo nếu tự đăng lại bài của mình: notify() tự bỏ qua)
    notify(original_post.user_id, current_user.id, "repost", post_id=repost.id)

    # Lưu Repost + Share + Notification cùng transaction
    db.session.commit()

    return jsonify({
        "message": "Đăng lại bài viết thành công",
        "forced_private": forced_private,
        "repost": {
            "id": repost.id,
            "user_id": repost.user_id,
            "original_post_id": repost.original_post_id,
            "caption": repost.caption,
            "visibility": repost.visibility,
            "repost_note": repost.repost_note,
        },
        "share": {
            "id": share.id,
            "share_type": share.share_type
        }
    }), 201


# =========================================================
# LOCATION - CREATE
# =========================================================

@locations_bp.route(
    "/locations",
    methods=["POST"]
)
@login_required
def create_location():
    """
    Tạo địa điểm mới — tự dedupe theo slug
    (Đà Lạt = Da Lat = DALAT).
    """
    data = request.get_json() or {}

    name = data.get(
        "name",
        ""
    ).strip()

    map_link = data.get(
        "map_link",
        ""
    ).strip() or None

    if not name:
        return jsonify({
            "error": "Tên địa điểm không được để trống"
        }), 400

    location, loc_error = resolve_location(name, data.get("code", ""))
    if loc_error:
        return jsonify({"error": loc_error}), 400

    return jsonify({
        "location": {
            "id": location.id,
            "name": location.name,
            "slug": location.slug,
            "map_link": location.map_link
        }
    }), 201


# =========================================================
# DANH MỤC HÀNH CHÍNH — gợi ý / duyệt tỉnh → phường/xã (dữ liệu công khai, không chứa thông tin người dùng)
# =========================================================

def _places_guard():
    key = (str(current_user.id) if current_user.is_authenticated else (request.remote_addr or "?"))
    if not places.rate_ok("places:" + key):
        return jsonify({"error": "Bạn thao tác quá nhanh, thử lại sau ít giây"}), 429
    return None


@locations_bp.route("/places/suggest", methods=["GET"])
def places_suggest():
    """Gõ → gợi ý. ?q=...&province=<mã tỉnh, tuỳ chọn>&limit=8"""
    blocked = _places_guard()
    if blocked:
        return blocked
    try:
        limit = int(request.args.get("limit", 8))
    except ValueError:
        limit = 8
    return jsonify({"results": places.search(request.args.get("q", ""), request.args.get("province") or None, limit)})


@locations_bp.route("/places/provinces", methods=["GET"])
def places_provinces():
    blocked = _places_guard()
    if blocked:
        return blocked
    return jsonify({"provinces": [places.public(p) for p in places.provinces()]})


@locations_bp.route("/places/wards", methods=["GET"])
def places_wards():
    """Phường/xã/đặc khu của 1 tỉnh: ?province=<mã>&q=<lọc, tuỳ chọn>"""
    blocked = _places_guard()
    if blocked:
        return blocked
    code = (request.args.get("province") or "").strip()
    info = places.get(code)
    if not info or info["kind"] != "province":
        return jsonify({"error": "Tỉnh/thành không hợp lệ"}), 404
    return jsonify({"province": places.public(info),
                    "wards": [places.public(w) for w in places.wards_of(code, request.args.get("q", ""))]})


@locations_bp.route("/places/check-links", methods=["POST"])
@login_required
def places_check_links():
    """Kiểm tra TRƯỚC khi đăng (chỉ để cảnh báo, không lưu gì): link Google Maps có vẻ lệch tỉnh đã chọn không?
    Body JSON: {kind:'post'|'plan', locations:[{code,name}], links:[{url,label}]} → {warning: str|None, results:[...]}"""
    if not places.rate_ok("chk:%s" % current_user.id, limit=20, window=60):
        return jsonify({"error": "Bạn thao tác quá nhanh, thử lại sau ít giây"}), 429
    from app.locations import geocheck
    data = request.get_json(silent=True) or {}
    kind = "plan" if data.get("kind") == "plan" else "post"
    limit = POST_LIMIT if kind == "post" else 5
    links, err = parse_map_links(data.get("links"), limit)
    if err:
        return jsonify({"error": err}), 400
    locs = []
    for item in (data.get("locations") or [])[:3]:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or "").strip()
        if not places.get(code):
            code = places.resolve_text(item.get("name")) or ""
        if code:                                   # chỉ cần mã để biết tỉnh — không tạo gì trong DB
            from types import SimpleNamespace
            locs.append(SimpleNamespace(code=code, name=""))
    results = geocheck.check_links(links, locs, kind)
    return jsonify({"warning": geocheck.warning_text(results),
                    "results": [{k: r[k] for k in ("url", "label", "status", "km", "province")} for r in results]})


# =========================================================
# LOCATION - SEARCH
# =========================================================

@locations_bp.route("/search", methods=["GET"])
@locations_bp.route(
    "/locations/search",
    methods=["GET"]
)
@login_required
def search_locations():
    """
    Tìm kiếm địa điểm.
    Sort theo số lần share của các bài viết thuộc địa điểm:
    share nhiều -> đứng trước.
    """

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:
        return jsonify({
            "locations": []
        })

    query_slug = normalize_location_name(
        query
    )

    locations = (
        db.session.query(
            Location,
            db.func.count(Share.id).label("share_count")
        )
        .outerjoin(
            Post,
            Post.location_id == Location.id
        )
        .outerjoin(
            Share,
            Share.post_id == Post.id
        )
        .filter(
            Location.slug.contains(query_slug)
        )
        .group_by(
            Location.id
        )
        .order_by(
            db.func.count(Share.id).desc(),
            Location.name.asc()
        )
        .limit(10)
        .all()
    )

    return jsonify({
        "locations": [
            {
                "id": location.id,
                "name": location.name,
                "slug": location.slug,
                "address": location.address,
                "map_link": location.map_link,
                "share_count": share_count
            }
            for location, share_count in locations
        ]
    })

# ============================================================
# LỊCH TRÌNH (ITINERARY) — Pack 3, phụ trách: Khánh
# Quyền xem lịch trình: visible_itinerary_or_404 (private chỉ chủ; tài khoản kín cần follow)
# ============================================================

@locations_bp.route("/itineraries", methods=["POST"])
@login_required
def create_itinerary():
    data = request.get_json()
    if not data or not data.get("title"):
        return jsonify({"error": "Thiếu tên chuyến đi"}), 400

    from datetime import datetime
    location, loc_error = resolve_location(data.get("location_name"), data.get("location_code"))
    if loc_error:
        return jsonify({"error": loc_error}), 400

    itinerary = Itinerary(
        user_id=current_user.id,
        title=data["title"],
        description=data.get("description"),
        location_id=location.id if location else None,
        start_date=datetime.fromisoformat(data["start_date"]).date() if data.get("start_date") else None,
        end_date=datetime.fromisoformat(data["end_date"]).date() if data.get("end_date") else None,
        trip_type=data.get("trip_type"),
        participants_count=data.get("participants_count"),
        budget_estimate=data.get("budget_estimate"),
    )
    db.session.add(itinerary)
    db.session.flush()  # có itinerary.id ngay, chưa commit, để gắn Day/Stop cùng 1 transaction

    for day_data in data.get("days", []):
        day = ItineraryDay(itinerary_id=itinerary.id, day_number=day_data["day_number"])
        db.session.add(day)
        db.session.flush()
        for i, stop_data in enumerate(day_data.get("stops", [])):
            from datetime import time as dtime
            start_t = dtime.fromisoformat(stop_data["start_time"]) if stop_data.get("start_time") else None
            end_t = dtime.fromisoformat(stop_data["end_time"]) if stop_data.get("end_time") else None
            db.session.add(ItineraryStop(
                day_id=day.id,
                order_index=i,
                activity_name=stop_data["activity_name"],
                location_link=stop_data.get("location_link"),
                price=stop_data.get("price"),
                note=stop_data.get("note"),
                start_time=start_t,
                end_time=end_t,
            ))

    for lodging_data in data.get("lodgings", []):
        db.session.add(ItineraryLodging(
            itinerary_id=itinerary.id,
            name=lodging_data["name"],
            price_per_night=lodging_data.get("price_per_night"),
            booking_link=lodging_data.get("booking_link"),
            note=lodging_data.get("note"),
        ))

    db.session.commit()
    return jsonify({"message": "Đăng lịch trình thành công", "itinerary": serialize_itinerary(itinerary)}), 201


@locations_bp.route("/itineraries/feed", methods=["GET"])
@login_required
def itinerary_feed():
    feed_follow = following_ids(current_user.id)
    itineraries = [it for it in Itinerary.query.order_by(Itinerary.created_at.desc()).all()
                   if can_view_itinerary(it, current_user.id, feed_follow)]
    return jsonify({"feed": [
        {
            "id": it.id,
            "title": it.title,
            "cover_image": it.cover_image,
            "user_id": it.user_id,
            "start_date": it.start_date.isoformat() if it.start_date else None,
            "end_date": it.end_date.isoformat() if it.end_date else None,
        } for it in itineraries
    ]})

def serialize_itinerary(it):
    return {
        "id": it.id,
        "title": it.title,
        "description": it.description,
        "cover_image": it.cover_image,
        "user_id": it.user_id,
        "start_date": it.start_date.isoformat() if it.start_date else None,
        "end_date": it.end_date.isoformat() if it.end_date else None,
        "trip_type": it.trip_type,
        "participants_count": it.participants_count,
        "budget_estimate": it.budget_estimate,
        "days": [{
            "id": d.id,
            "day_number": d.day_number,
            "stops": [{
                "id": s.id,
                "activity_name": s.activity_name,
                "location_link": s.location_link,
                "price": s.price,
                "note": s.note,
                "image_path": s.image_path,
                "start_time": s.start_time.strftime("%H:%M") if s.start_time else None,
                "end_time": s.end_time.strftime("%H:%M") if s.end_time else None,
            } for s in d.stops],
        } for d in it.days],
        "lodgings": [{
            "id": l.id,
            "name": l.name,
            "price_per_night": l.price_per_night,
            "booking_link": l.booking_link,
            "note": l.note,
        } for l in it.lodgings],
        "attachments": [{"id": a.id, "original_name": a.original_name, "file_path": a.file_path} for a in it.attachments],
        "like_count": Like.query.filter_by(itinerary_id=it.id).count(),
        "comment_count": Comment.query.filter_by(itinerary_id=it.id).count(),
    }


@locations_bp.route("/itineraries/<int:itinerary_id>", methods=["GET"])
@login_required
def get_itinerary(itinerary_id):
    it = visible_itinerary_or_404(itinerary_id)
    return jsonify(serialize_itinerary(it))


@locations_bp.route("/itineraries/<int:itinerary_id>", methods=["DELETE"])
@login_required
def delete_itinerary(itinerary_id):
    it = visible_itinerary_or_404(itinerary_id)
    if it.user_id != current_user.id:
        return jsonify({"error": "Bạn không có quyền xóa lịch trình này"}), 403
    from app.deletion import delete_itinerary_cascade
    delete_itinerary_cascade(it)          # xóa kèm tim, bình luận, chia sẻ, đã lưu, thông báo, tệp
    return jsonify({"message": "Xóa lịch trình thành công"})


ATTACHMENT_EXTENSIONS = {"docx", "xlsx", "pdf", "pptx"}

@locations_bp.route("/itineraries/<int:itinerary_id>/attachments", methods=["POST"])
@login_required
def upload_itinerary_attachment(itinerary_id):
    it = db.get_or_404(Itinerary, itinerary_id)
    if it.user_id != current_user.id:
        return jsonify({"error": "Bạn không có quyền thêm file vào lịch trình này"}), 403

    file = request.files.get("file")
    if not file or file.filename == "":
        return jsonify({"error": "Thiếu file"}), 400
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in ATTACHMENT_EXTENSIONS:
        return jsonify({"error": f"Chỉ nhận file: {', '.join(ATTACHMENT_EXTENSIONS)}"}), 400

    filename = f"{uuid.uuid4().hex}_{secure_filename(file.filename)}"
    upload_dir = os.path.join(current_app.root_path, "static", "uploads", "itineraries")
    os.makedirs(upload_dir, exist_ok=True)
    save_path = os.path.join(upload_dir, filename)
    file.save(save_path)

    attachment = ItineraryAttachment(
        itinerary_id=itinerary_id,
        file_path=f"uploads/itineraries/{filename}",
        original_name=file.filename,
    )
    db.session.add(attachment)
    db.session.commit()
    return jsonify({"message": "Upload thành công", "attachment_id": attachment.id}), 201


# ---- Like / Comment / Share / Save áp dụng cho Itinerary ----

@locations_bp.route("/itineraries/<int:itinerary_id>/like", methods=["POST"])
@login_required
def like_itinerary(itinerary_id):
    plan = visible_itinerary_or_404(itinerary_id)
    existing = Like.query.filter_by(user_id=current_user.id, itinerary_id=itinerary_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"message": "Đã bỏ tim", "liked": False})
    db.session.add(Like(user_id=current_user.id, itinerary_id=itinerary_id))
    notify(plan.user_id, current_user.id, "like", itinerary_id=plan.id)
    db.session.commit()
    return jsonify({"message": "Đã thả tim", "liked": True})


@locations_bp.route("/itineraries/<int:itinerary_id>/comments", methods=["POST"])
@login_required
def create_itinerary_comment(itinerary_id):
    plan = visible_itinerary_or_404(itinerary_id)
    data = request.get_json() or {}
    content = (data.get("content") or "").strip()

    if detect_xss(content):
        log_security_event(event_type="XSS_DETECTED", severity="CRITICAL",
                            status_code=201, payload_snippet=content,
                            action_taken="LOGGED_ONLY")

    if not content:
        return jsonify({"error": "Nội dung bình luận không được rỗng"}), 400

    parent_id = data.get("parent_id")
    parent = None
    reply_to = None  # reply cụ thể đang được trả lời (nếu có) — khác parent khi trả lời-của-trả lời
    if parent_id:
        parent = db.session.get(Comment, int(parent_id))
        if not parent or parent.itinerary_id != itinerary_id:
            return jsonify({"error": "Bình luận gốc không hợp lệ"}), 400
        if parent.parent_id:
            reply_to = parent
            parent_id = parent.parent_id
            parent = db.session.get(Comment, parent_id)

    comment = Comment(
        user_id=current_user.id,
        itinerary_id=itinerary_id,
        parent_id=parent.id if parent else None,
        reply_to_id=reply_to.id if reply_to else None,
        content=content,
    )
    db.session.add(comment)
    tagged = notify_mentions(content, current_user.id, plan.user_id, itinerary=plan)
    if plan.user_id not in tagged:
        notify(plan.user_id, current_user.id, "comment", itinerary_id=plan.id)
    notify_target = reply_to or parent
    if notify_target and notify_target.user_id != current_user.id and notify_target.user_id not in tagged and notify_target.user_id != plan.user_id:
        notify(notify_target.user_id, current_user.id, "comment", itinerary_id=plan.id)
    db.session.commit()

    reply_to_author = db.session.get(User, reply_to.user_id) if reply_to else None
    display_content = strip_leading_self_mention(comment.content, reply_to_author) if reply_to_author else comment.content

    return jsonify({
        "message": "Đã bình luận",
        "comment": {
            "id": comment.id,
            "user_id": comment.user_id,
            "parent_id": comment.parent_id,
            "reply_to_id": comment.reply_to_id,
            "reply_to_name": mention_display_name(reply_to_author) if reply_to_author else None,
            "reply_to_username": reply_to_author.username if reply_to_author else None,
            "author_username": current_user.username,
            "author_avatar": current_user.avatar_url,
            "content": display_content,
            "content_html": str(render_mentions(display_content)),
            "likes_count": 0,
            "is_liked": False,
            "created_at": comment.created_at.isoformat(),
        },
    }), 201


@locations_bp.route("/itineraries/<int:itinerary_id>/comments", methods=["GET"])
@login_required
def get_itinerary_comments(itinerary_id):
    visible_itinerary_or_404(itinerary_id)
    comments = Comment.query.filter_by(itinerary_id=itinerary_id).order_by(Comment.created_at.asc()).all()
    return jsonify({"comments": [
        {"id": c.id, "user_id": c.user_id, "content": c.content, "content_html": str(render_mentions(c.content))} for c in comments
    ]})


@locations_bp.route("/itineraries/<int:itinerary_id>/share", methods=["POST"])
@login_required
def share_itinerary(itinerary_id):
    plan = visible_itinerary_or_404(itinerary_id)
    existing = Share.query.filter_by(user_id=current_user.id, itinerary_id=itinerary_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"message": "Đã hủy chia sẻ", "shared": False})
    db.session.add(Share(user_id=current_user.id, itinerary_id=itinerary_id, share_type="copy_link"))
    db.session.commit()                      # chia sẻ kiểu copy link: không báo cho chủ lịch trình
    return jsonify({"message": "Đã chia sẻ", "shared": True})


@locations_bp.route("/itineraries/<int:itinerary_id>/save", methods=["POST"])
@login_required
def save_itinerary(itinerary_id):
    visible_itinerary_or_404(itinerary_id)
    existing = SavedPost.query.filter_by(user_id=current_user.id, itinerary_id=itinerary_id).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({"message": "Đã bỏ lưu", "saved": False})
    db.session.add(SavedPost(user_id=current_user.id, itinerary_id=itinerary_id))
    db.session.commit()
    return jsonify({"message": "Đã lưu", "saved": True})

@locations_bp.route("/itineraries/<int:itinerary_id>", methods=["PUT"])
@login_required
def update_itinerary(itinerary_id):
    it = db.get_or_404(Itinerary, itinerary_id)
    if it.user_id != current_user.id:
        return jsonify({"error": "Bạn không có quyền sửa lịch trình này"}), 403

    data = request.get_json()
    if not data:
        return jsonify({"error": "Thiếu dữ liệu"}), 400

    from datetime import datetime

    if "title" in data:
        if not data["title"]:
            return jsonify({"error": "Tên chuyến đi không được để trống"}), 400
        it.title = data["title"]
    if "description" in data:
        it.description = data["description"]
    if "start_date" in data:
        it.start_date = datetime.fromisoformat(data["start_date"]).date() if data["start_date"] else None
    if "end_date" in data:
        it.end_date = datetime.fromisoformat(data["end_date"]).date() if data["end_date"] else None
    if "trip_type" in data:
        it.trip_type = data["trip_type"]
    if "participants_count" in data:
        it.participants_count = data["participants_count"]
    if "budget_estimate" in data:
        it.budget_estimate = data["budget_estimate"]

    # Nếu client gửi kèm "days" hoặc "lodgings" -> xóa sạch cũ, tạo lại từ đầu
    # (nhờ cascade="all, delete-orphan" đã khai báo sẵn trong model Itinerary)
    if "days" in data:
        it.days = []
        db.session.flush()
        for day_data in data["days"]:
            day = ItineraryDay(itinerary_id=it.id, day_number=day_data["day_number"])
            db.session.add(day)
            db.session.flush()
            for i, stop_data in enumerate(day_data.get("stops", [])):
                db.session.add(ItineraryStop(
                    day_id=day.id,
                    order_index=i,
                    activity_name=stop_data["activity_name"],
                    location_link=stop_data.get("location_link"),
                    price=stop_data.get("price"),
                    note=stop_data.get("note"),
                ))

    if "lodgings" in data:
        it.lodgings = []
        db.session.flush()
        for lodging_data in data["lodgings"]:
            db.session.add(ItineraryLodging(
                itinerary_id=it.id,
                name=lodging_data["name"],
                price_per_night=lodging_data.get("price_per_night"),
                booking_link=lodging_data.get("booking_link"),
                note=lodging_data.get("note"),
            ))

    db.session.commit()
    return jsonify({"message": "Cập nhật lịch trình thành công", "itinerary_id": it.id})

@locations_bp.route("/itineraries/<int:itinerary_id>/cover-image", methods=["POST"])
@login_required
def upload_itinerary_cover(itinerary_id):
    it = db.get_or_404(Itinerary, itinerary_id)
    if it.user_id != current_user.id:
        return jsonify({"error": "Bạn không có quyền sửa lịch trình này"}), 403

    file = request.files.get("image")
    if not file or file.filename == "":
        return jsonify({"error": "Thiếu file ảnh"}), 400
    if get_media_type(file.filename) != "image":
        return jsonify({"error": "Chỉ nhận file ảnh (jpg/png/gif)"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = os.path.join(current_app.root_path, "static", "uploads", "itineraries")
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))

    it.cover_image = f"uploads/itineraries/{filename}"
    db.session.commit()
    return jsonify({"message": "Đã cập nhật ảnh bìa", "cover_image": it.cover_image})


@locations_bp.route("/itineraries/stops/<int:stop_id>/image", methods=["POST"])
@login_required
def upload_stop_image(stop_id):
    stop = db.get_or_404(ItineraryStop, stop_id)
    if stop.day.itinerary.user_id != current_user.id:
        return jsonify({"error": "Bạn không có quyền sửa chặng này"}), 403

    file = request.files.get("image")
    if not file or file.filename == "":
        return jsonify({"error": "Thiếu file ảnh"}), 400
    if get_media_type(file.filename) != "image":
        return jsonify({"error": "Chỉ nhận file ảnh (jpg/png/gif)"}), 400

    ext = file.filename.rsplit(".", 1)[1].lower()
    filename = f"{uuid.uuid4().hex}.{ext}"
    upload_dir = os.path.join(current_app.root_path, "static", "uploads", "itineraries")
    os.makedirs(upload_dir, exist_ok=True)
    file.save(os.path.join(upload_dir, filename))

    stop.image_path = f"uploads/itineraries/{filename}"
    db.session.commit()
    return jsonify({"message": "Đã cập nhật ảnh chặng", "image_path": stop.image_path})

@locations_bp.route("/itineraries/new", methods=["GET"])
@login_required
def new_itinerary_page():
    return redirect(url_for("locations.feed", compose="plan"))

@locations_bp.route("/api/hashtags/top", methods=["GET"])
def api_top_hashtags():
    """API trả về top 10 hashtag được dùng nhiều nhất để hiển thị thanh gợi ý."""
    from app.locations.hashtag_utils import get_top_hashtags
    limit = min(max(request.args.get("limit", 10, type=int), 1), 30)
    tags = get_top_hashtags(limit=limit)
    return jsonify({"hashtags": tags})

