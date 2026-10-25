"""Routes AUTH — xử lý đăng ký, đăng nhập, đăng xuất, hồ sơ cá nhân (Profile),
follow/unfollow, onboarding, cài đặt, đổi mật khẩu, OTP khôi phục mật khẩu,
thông tin liên hệ, CSRF token, và đăng nhập Google OAuth.
Hỗ trợ song song cả giao diện HTML (Jinja2 + Flask-WTF + CSRF) lẫn API JSON (Postman/Mobile).
"""
import json
import os
import random
import re
import secrets
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from werkzeug.utils import secure_filename

from flask import (
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_login import current_user, login_required, login_user, logout_user
from flask_wtf.csrf import generate_csrf
from sqlalchemy import func, or_

from app.auth import auth_bp
from app.auth.forms import (
    ChangePasswordForm,
    EditProfileForm,
    ForgotPasswordForm,
    LoginForm,
    RegisterForm,
    ResetPasswordForm,
    SettingsForm,
)
from app.auth.utils import validate_password
from app.profile_bg import save_background, save_original, original_url_for, remove_background_file, BgError, MAX_BG_BYTES
from app.locations.discovery import (following_ids, can_view_post, can_view_itinerary, viewer_follow_set, norm_text,
                                     likers_of, liker_count)
from app.presence import presence_state
from app.timeutil import relative_label
from app.notifications import notify
from app.extensions import db, csrf, limiter
from app.security_logging import log_security_event, detect_sqli
import app.locations.places as places
from app.models import (
    Comment,
    Follow,
    Itinerary,
    ItineraryStop,
    ItineraryLodging,
    Like,
    Location,
    Notification,
    Photo,
    Post,
    SavedPost,
    Share,
    User,
    Hashtag,
)

from app.auth.tags import normalize_tags, hashtag_for, FIXED_TAG_HASHTAGS

FIXED_TAGS = set(FIXED_TAG_HASHTAGS)  # 5 nhãn có sẵn (nhãn tự đặt: xem app/auth/tags.py)

PROFILE_POSTS_CAP = 30   # tab "Bài viết" hiện đầy đủ tối đa 30 bài mới nhất (mỗi thẻ có ảnh/bình luận nên không nạp hết)

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


@auth_bp.route('/', methods=['GET'])
def index():
    """Trang chủ — chuyển hướng về dòng tin feed."""
    return redirect(url_for('locations.feed'))


# =========================================================
# TÌM KIẾM TOÀN DIỆN (UNIVERSAL SEARCH) — CHUẨN FACEBOOK FEED
# =========================================================

def search_variants(q):
    """Sinh các biến thể của từ khóa tìm kiếm:
    - Chuỗi gốc (e.g. 'Đà Lạt')
    - Chuỗi thường (e.g. 'đà lạt')
    - Chuỗi không dấu có dấu cách (e.g. 'da lat')
    - Chuỗi không dấu viết liền (e.g. 'dalat')
    """
    raw = (q or "").strip()
    if not raw:
        return []
    variants = set()
    variants.add(raw)
    variants.add(raw.lower())

    s = unicodedata.normalize("NFD", raw).replace("đ", "d").replace("Đ", "D")
    unaccented_with_spaces = "".join(c for c in s if unicodedata.category(c) != "Mn")
    unaccented_clean = re.sub(r"\s+", " ", unaccented_with_spaces).strip().lower()
    if unaccented_clean:
        variants.add(unaccented_clean)

    compact = norm_text(raw)
    if compact:
        variants.add(compact)

    return [v for v in variants if v]


def _fold(text):
    """Bỏ dấu tiếng Việt + chữ thường + gộp khoảng trắng: 'Võ  Quốc Khánh' -> 'vo quoc khanh'."""
    s = unicodedata.normalize("NFD", text or "").replace("đ", "d").replace("Đ", "D")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", s).strip().lower()


def find_users(q, limit=None):
    """Tìm người dùng theo username / tên hiển thị / họ tên / thẻ, KHÔNG phân biệt dấu và hoa thường.

    Vì sao không dùng SQL ILIKE: SQL so khớp chuỗi thô nên 'vo quoc khanh' không khớp 'Võ Quốc Khánh'.
    Cách làm: mỗi từ trong ô tìm kiếm phải xuất hiện (đã bỏ dấu) trong tên/username của người đó, theo thứ tự bất kỳ
    ('khanh quoc' vẫn ra); gõ liền không dấu ('voquockhanh') cũng khớp. Chỉ đọc vài cột nhẹ rồi lọc bằng Python.
    Trả danh sách User, xếp: trùng đúng > bắt đầu bằng > còn lại."""
    qf = _fold(q)
    if not qf:
        return []
    tokens, qc = qf.split(), qf.replace(" ", "")
    rows = db.session.query(User.id, User.username, User.nickname, User.full_name, User.profile_tag, User.tags).all()
    scored = []
    for uid, uname, nick, full, ptag, tags in rows:
        names = [_fold(uname), _fold(nick), _fold(full)]
        hay = " ".join(names + [_fold(ptag), _fold(tags)])
        hay_c = hay.replace(" ", "")
        if not (all(t in hay for t in tokens) or qc in hay_c):
            continue
        if any(n == qf or n.replace(" ", "") == qc for n in names if n):
            rank = 0
        elif any(n.startswith(qf) for n in names if n):
            rank = 1
        elif any(qf in n for n in names if n):
            rank = 2
        else:
            rank = 3
        scored.append((rank, uid))
    scored.sort()
    ids = [uid for _, uid in (scored[:limit] if limit else scored)]
    if not ids:
        return []
    by_id = {u.id: u for u in User.query.filter(User.id.in_(ids)).all()}
    return [by_id[i] for i in ids if i in by_id]


@auth_bp.route('/search', methods=['GET'])
@limiter.limit('45 per minute')
def search_page():
    """Trang kết quả tìm kiếm đa đối tượng: User, Bài viết, Lịch trình, Địa điểm.
    Sắp xếp theo độ tương tác (Engagement Score: Like, Comment, Share).
    Bảo vệ quyền riêng tư qua can_view_post và can_view_itinerary.
    Hỗ trợ tiếng Việt có dấu, không dấu và liên kết địa danh tự động.
    """
    from app.locations.routes import build_post_cards
    from app.plans.routes import cards_for_profile

    q = (request.args.get('q') or '').strip()
    tab = (request.args.get('tab') or 'all').strip().lower()
    if tab not in ('all', 'users', 'posts', 'locations'):
        tab = 'all'

    if detect_sqli(q):
        log_security_event(event_type="SQLI_DETECTED", severity="WARNING",
                            status_code=200, payload_snippet=q,
                            action_taken="LOGGED_ONLY")  # vẫn trả kết quả bình thường (ORM tự an toàn), chỉ ghi nhận để theo dõi

    viewer_id = current_user.id if current_user.is_authenticated else None
    viewer_follow = viewer_follow_set(viewer_id)

    matched_users = []
    matched_posts = []
    matched_plans = []
    matched_locations = []
    location_posts = []
    location_plans = []

    if q:
        ql = q.lower()
        qn = norm_text(q)
        q_vars = search_variants(q)

        # 1. TÌM ĐỊA ĐIỂM (Dataset hành chính Việt Nam + CSDL Location)
        # Thực hiện trước để trích xuất matched_db_loc_ids phục vụ lọc Bài viết & Lịch trình
        matched_locations = []
        loc_codes = set()
        matched_db_loc_ids = set()

        # 1.1. Dataset chuẩn hành chính Việt Nam (34 tỉnh/thành, phường/xã, địa danh du lịch)
        real_places = places.search(q, limit=12)
        real_place_codes = {rp["code"] for rp in real_places if rp.get("code")}
        for rp in real_places:
            loc_codes.add(rp["code"])
            map_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(rp['label'])}"
            kind_label = "Tỉnh / Thành phố" if rp.get("kind") == "province" else "Phường / Xã / Đặc khu"
            matched_locations.append({
                "code": rp["code"],
                "name": rp["name"],
                "full_name": rp["label"],
                "kind": rp.get("kind"),
                "kind_label": kind_label,
                "note": rp.get("note") or rp.get("via") or "",
                "map_link": map_url,
                "is_real_place": True
            })

        # 1.2. Tìm thêm các địa điểm đã lưu trong CSDL khớp với từ khóa hoặc mã đơn vị hành chính
        for dl in Location.query.all():
            dl_norm = norm_text(dl.name or "")
            dl_addr_norm = norm_text(dl.address or "")
            dl_slug_norm = norm_text(dl.slug or "")
            matches = (
                (dl.code and dl.code in real_place_codes) or
                any(v in (dl.name or '').lower() or v in (dl.address or '').lower() for v in q_vars) or
                any(norm_text(v) in dl_norm or norm_text(v) in dl_addr_norm or norm_text(v) in dl_slug_norm for v in q_vars if norm_text(v))
            )
            if matches:
                matched_db_loc_ids.add(dl.id)
                if dl.code not in loc_codes:
                    matched_locations.append({
                        "id": dl.id,
                        "code": dl.code,
                        "name": dl.name,
                        "full_name": dl.address or dl.name,
                        "kind": dl.kind or "location",
                        "kind_label": "Địa điểm",
                        "note": dl.address or "",
                        "map_link": dl.map_link or f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(dl.name)}",
                        "is_real_place": True
                    })

        # 2. TÌM USER (Tên, username, nickname, profile_tag, tags) — Hỗ trợ đa biến thể tiếng Việt
        users_query = find_users(q)          # không phân biệt dấu: 'vo quoc khanh' ra 'Võ Quốc Khánh'

        user_cards = []
        for u in users_query:
            rel = 0
            u_name = (u.username or '').lower()
            u_nick = (u.nickname or '').lower()
            u_full = (u.full_name or '').lower()
            if u_name == ql or norm_text(u_name) == qn:
                rel += 1000
            elif u_name.startswith(ql) or norm_text(u_name).startswith(qn):
                rel += 500
            elif u_nick == ql or norm_text(u_nick) == qn:
                rel += 400
            elif u_full == ql or norm_text(u_full) == qn:
                rel += 300
            elif ql in u_name or qn in norm_text(u_name):
                rel += 200
            else:
                rel += 100

            followers_count = Follow.query.filter_by(followed_id=u.id).count()
            following_count = Follow.query.filter_by(follower_id=u.id).count()
            is_following = bool(viewer_id and u.id in viewer_follow)
            user_cards.append({
                "user": u,
                "relevance": rel + followers_count,
                "base_rel": rel,
                "followers_count": followers_count,
                "following_count": following_count,
                "is_following": is_following,
                "is_self": bool(viewer_id and viewer_id == u.id)
            })
        user_cards.sort(key=lambda x: x["relevance"], reverse=True)
        matched_users = user_cards

        # 3. TÌM BÀI ĐĂNG (Caption, Hashtag, Địa điểm gắn thẻ) — Sắp xếp theo tương tác cao nhất
        post_filters = []
        for var in q_vars:
            post_filters.append(Post.caption.ilike(f"%{var}%"))
            post_filters.append(Hashtag.name.ilike(f"%{var}%"))
        if matched_db_loc_ids:
            post_filters.append(Post.location_id.in_(list(matched_db_loc_ids)))

        post_query = Post.query.outerjoin(Post.hashtags).filter(or_(*post_filters)).distinct().all()

        # Bài viết CỦA người dùng khớp tên: tìm "vo quoc khanh" thì ngoài hồ sơ còn thấy luôn bài người đó đăng.
        # Chỉ lấy người khớp mạnh (trùng đúng username/tên hoặc username bắt đầu bằng từ khóa), tối đa 3 người, 40 bài mới nhất mỗi người,
        # để gõ "khanh" không đổ cả trăm bài của mọi người tên Khánh vào kết quả.
        author_ids = [c["user"].id for c in user_cards if c["base_rel"] >= 300][:3]
        if author_ids:
            have = {p.id for p in post_query}
            for ap in Post.query.filter(Post.user_id.in_(author_ids)).order_by(Post.created_at.desc()).limit(40 * len(author_ids)).all():
                if ap.id not in have:
                    post_query.append(ap)
                    have.add(ap.id)

        # Quét bổ sung qua norm_text để không bỏ sót các hashtag dính liền không dấu (#dalat)
        existing_p_ids = {p.id for p in post_query}
        for p in Post.query.order_by(Post.created_at.desc()).limit(300).all():
            if p.id in existing_p_ids:
                continue
            cap_norm = norm_text(p.caption or "")
            if any(norm_text(v) in cap_norm for v in q_vars if norm_text(v)):
                post_query.append(p)
                existing_p_ids.add(p.id)

        viewable_posts = [p for p in post_query if can_view_post(p, viewer_id, viewer_follow)]

        scored_posts = []
        for p in viewable_posts:
            l_cnt = Like.query.filter_by(post_id=p.id).count()
            c_cnt = Comment.query.filter_by(post_id=p.id).count()
            s_cnt = Share.query.filter_by(post_id=p.id).count()
            score = l_cnt * 2 + c_cnt * 3 + s_cnt * 4
            if any(v in (p.caption or '').lower() or norm_text(v) in norm_text(p.caption or '') for v in q_vars if v):
                score += 10
            if p.user_id in author_ids:
                score += 20          # bài do chính người được tìm đăng: ưu tiên lên đầu
            scored_posts.append((score, p))
        scored_posts.sort(key=lambda x: (x[0], x[1].created_at or datetime.min), reverse=True)
        sorted_posts = [item[1] for item in scored_posts]
        matched_posts = build_post_cards(sorted_posts, viewer_id)
        location_posts = matched_posts

        # 4. TÌM LỊCH TRÌNH (Tiêu đề, điểm đến, loại chuyến đi, địa điểm ghé)
        itin_filters = []
        for var in q_vars:
            itin_filters.append(Itinerary.title.ilike(f"%{var}%"))
            itin_filters.append(Itinerary.destination.ilike(f"%{var}%"))
            itin_filters.append(Itinerary.description.ilike(f"%{var}%"))
            itin_filters.append(Itinerary.trip_type.ilike(f"%{var}%"))
        if matched_db_loc_ids:
            itin_filters.append(Itinerary.location_id.in_(list(matched_db_loc_ids)))
        itin_query = Itinerary.query.filter(or_(*itin_filters)).all()

        stop_filters = []
        for var in q_vars:
            stop_filters.append(ItineraryStop.activity_name.ilike(f"%{var}%"))
            stop_filters.append(ItineraryStop.note.ilike(f"%{var}%"))
        stops_matching = ItineraryStop.query.filter(or_(*stop_filters)).all()
        itin_ids = {p.id for p in itin_query}
        for st in stops_matching:
            if st.day and st.day.itinerary and st.day.itinerary.id not in itin_ids:
                itin_query.append(st.day.itinerary)
                itin_ids.add(st.day.itinerary.id)

        viewable_plans = [p for p in itin_query if can_view_itinerary(p, viewer_id, viewer_follow)]

        scored_plans = []
        for it in viewable_plans:
            l_cnt = Like.query.filter_by(itinerary_id=it.id).count()
            c_cnt = Comment.query.filter_by(itinerary_id=it.id).count()
            s_cnt = Share.query.filter_by(itinerary_id=it.id).count()
            save_cnt = SavedPost.query.filter_by(itinerary_id=it.id).count()
            score = l_cnt * 2 + c_cnt * 3 + s_cnt * 4 + save_cnt * 3
            if any(v in (it.title or '').lower() or norm_text(v) in norm_text(it.title or '') for v in q_vars if v):
                score += 10
            scored_plans.append((score, it))
        scored_plans.sort(key=lambda x: (x[0], x[1].created_at or datetime.min), reverse=True)
        sorted_plans = [item[1] for item in scored_plans]
        matched_plans = cards_for_profile(sorted_plans, viewer_id)
        location_plans = matched_plans

    counts = {
        "users": len(matched_users),
        "posts": len(matched_posts),
        "plans": len(matched_plans),
        "locations": len(matched_locations)
    }
    total_results = sum(counts.values())

    return render_template(
        'search_results.html',
        q=q,
        tab=tab,
        users=matched_users,
        posts=matched_posts,
        plans=matched_plans,
        locations=matched_locations,
        location_posts=location_posts,
        location_plans=location_plans,
        counts=counts,
        total_results=total_results
    )


@auth_bp.route('/search/suggest', methods=['GET'])
@limiter.limit('60 per minute')
def search_suggest():
    """API gợi ý nhanh khi người dùng gõ tìm kiếm ở Topbar."""
    q = (request.args.get('q') or '').strip()
    if not q or len(q) < 2:
        return jsonify({"users": [], "locations": []})

    q_vars = search_variants(q)
    users = find_users(q, limit=5)       # tên hiển thị / họ tên / username, bỏ dấu

    # 1. Tra cứu CSDL địa điểm đã có trong hệ thống
    db_loc_filters = []
    for var in q_vars:
        db_loc_filters.append(Location.name.ilike(f"%{var}%"))
        db_loc_filters.append(Location.address.ilike(f"%{var}%"))
    db_locs = Location.query.filter(or_(*db_loc_filters)).limit(4).all()

    loc_results = []
    seen_names = set()
    for dl in db_locs:
        seen_names.add(dl.name.lower())
        loc_results.append({
            "code": dl.code or "",
            "name": dl.name,
            "address": dl.address or dl.name,
            "note": dl.address or "",
            "search_url": url_for('auth.search_page', q=dl.name, tab='locations')
        })

    # 2. Tra cứu thêm địa danh thực tế từ dataset places (vietnamese-provinces-database)
    real_places = places.search(q, limit=4)
    for p in real_places:
        if p["name"].lower() not in seen_names:
            seen_names.add(p["name"].lower())
            loc_results.append({
                "code": p["code"],
                "name": p["name"],
                "address": p["label"],
                "note": p.get("note") or "",
                "search_url": url_for('auth.search_page', q=p["name"], tab='locations')
            })

    return jsonify({
        "users": [{
            "id": u.id,
            "username": u.username,
            "name": u.nickname or u.full_name or u.username,
            "avatar_url": u.avatar_url or f"https://ui-avatars.com/api/?name={u.username}&background=169fe3&color=fff",
            "profile_url": url_for('auth.profile', username=u.username)
        } for u in users],
        "locations": loc_results[:6]
    })


# =========================================================
# ĐĂNG KÝ
# =========================================================

@auth_bp.route('/register', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 15 per hour', methods=['POST'])
def register():
    """Đăng ký tài khoản người dùng mới (hỗ trợ song song HTML Form và JSON API)."""
    if current_user.is_authenticated:
        logout_user()

    if request.method == 'GET':
        form = RegisterForm()
        return render_template('auth/register.html', form=form)

    if request.is_json:
        data = request.get_json(silent=True) or {}
        full_name = (data.get('full_name') or '').strip()
        username = (data.get('username') or '').strip()
        email = (data.get('email') or '').strip().lower()
        password = data.get('password') or ''

        if not full_name or not username or not email or not password:
            return jsonify({"error": "Thiếu họ tên/username/email/password"}), 400

        password_error = validate_password(password)
        if password_error:
            return jsonify({"error": password_error}), 400

        if User.query.filter_by(username=username).first():
            return jsonify({"error": "Username đã tồn tại"}), 400

        if User.query.filter_by(email=email).first():
            return jsonify({"error": "Email đã tồn tại"}), 400

        user = User(username=username, email=email, full_name=full_name)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        log_security_event(event_type="USER_REGISTER", severity="INFO",
                            status_code=201, action_taken="LOGGED_ONLY")
        login_user(user)
        return jsonify({
            "message": "Đăng ký thành công",
            "user_id": user.id,
            "profile_completed": user.profile_completed,
        }), 201

    form = RegisterForm()
    if form.validate_on_submit():
        full_name = form.full_name.data.strip()
        username = form.username.data.strip()
        email = form.email.data.strip().lower()

        if User.query.filter_by(username=username).first():
            form.username.errors.append("Tên người dùng này đã được sử dụng")
            return render_template('auth/register.html', form=form), 400

        if User.query.filter_by(email=email).first():
            form.email.errors.append("Địa chỉ email này đã được đăng ký tài khoản")
            return render_template('auth/register.html', form=form), 400

        try:
            user = User(
                username=username,
                email=email,
                full_name=full_name
            )

            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            log_security_event(event_type="USER_REGISTER", severity="INFO",
                                status_code=201, action_taken="LOGGED_ONLY")

            if current_user.is_authenticated:
                logout_user()

            flash("Đăng ký tài khoản thành công! Vui lòng đăng nhập để tiếp tục.", "success")
            return redirect(url_for('auth.login', registered='1'))
        except Exception as e:
            db.session.rollback()
            flash(f"Lỗi khi tạo tài khoản: {str(e)}", "error")
            return render_template('auth/register.html', form=form), 500

    return render_template('auth/register.html', form=form), 400 if form.errors else 200


# =========================================================
# ĐĂNG NHẬP
# =========================================================

@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit('5 per minute; 20 per hour', methods=['POST'])
def login():
    """Đăng nhập tài khoản người dùng — hỗ trợ linh hoạt Username, Email (không phân biệt chữ hoa/thường) hoặc Số điện thoại."""
    if request.method == 'GET':
        if (request.args.get('registered') or request.args.get('switch')) and current_user.is_authenticated:
            logout_user()

        if current_user.is_authenticated:
            return redirect(url_for('locations.feed'))
        form = LoginForm()
        return render_template('auth/login.html', form=form)

    if request.is_json:
        data = request.get_json(silent=True) or {}
        raw_val = (data.get('username') or '').strip()
        password = data.get('password') or ''

        if not raw_val or not password:
            return jsonify({"error": "Sai username hoặc password"}), 401

        if detect_sqli(raw_val):
            log_security_event(event_type="SQLI_DETECTED", severity="CRITICAL",
                                status_code=401, payload_snippet=raw_val,
                                action_taken="BLOCKED")

        user = User.query.filter(
            or_(
                func.lower(User.username) == raw_val.lower(),
                func.lower(User.email) == raw_val.lower(),
                User.phone_number == raw_val
            )
        ).first()

        if not user or not user.check_password(password):
            log_security_event(event_type="BRUTE_FORCE_AUTH", severity="WARNING",
                                status_code=401, payload_snippet=f"Failed login: {raw_val[:80]}",
                                action_taken="BLOCKED")
            return jsonify({"error": "Sai username hoặc password"}), 401

        if current_user.is_authenticated:
            logout_user()
        login_user(user)
        log_security_event(event_type="USER_LOGIN", severity="INFO",
                            status_code=200, action_taken="LOGGED_ONLY")
        return jsonify({"message": "Đăng nhập thành công", "username": user.username})

    form = LoginForm()
    if form.validate_on_submit():
        login_val = form.username.data.strip()
        password = form.password.data

        if detect_sqli(login_val):
            log_security_event(event_type="SQLI_DETECTED", severity="CRITICAL",
                                status_code=401, payload_snippet=login_val,
                                action_taken="BLOCKED")

        user = User.query.filter(
            or_(
                func.lower(User.username) == login_val.lower(),
                func.lower(User.email) == login_val.lower(),
                User.phone_number == login_val
            )
        ).first()

        if not user or not user.check_password(password):
            log_security_event(event_type="BRUTE_FORCE_AUTH", severity="WARNING",
                                status_code=401, payload_snippet=f"Failed login: {login_val[:80]}",
                                action_taken="BLOCKED")
            flash("Tên đăng nhập hoặc mật khẩu không chính xác.", "error")
            return render_template('auth/login.html', form=form), 401

        if current_user.is_authenticated:
            logout_user()
        login_user(user, remember=form.remember_me.data)
        log_security_event(event_type="USER_LOGIN", severity="INFO",
                            status_code=200, action_taken="LOGGED_ONLY")
        flash(f"Chào mừng trở lại, {user.nickname or user.username}!", "success")

        if not user.profile_completed:
            return redirect(url_for('auth.onboarding'))

        next_page = request.args.get('next')
        if next_page and next_page.startswith('/'):
            return redirect(next_page)
        return redirect(url_for('locations.feed'))

    return render_template('auth/login.html', form=form), 400 if form.errors else 200


# =========================================================
# ĐĂNG XUẤT
# =========================================================

@auth_bp.route("/logout", methods=["GET", "POST"])
@login_required
@csrf.exempt
def logout():
    """Đăng xuất người dùng."""
    try:
        current_user.last_seen = None  # hiện Offline ngay khi đăng xuất
        db.session.commit()
    except Exception:
        db.session.rollback()
    logout_user()
    if request.is_json or (request.headers.get("X-Requested-With") == "XMLHttpRequest" and not request.accept_mimetypes.accept_html):
        return jsonify({"message": "Đăng xuất thành công"})
    flash("Bạn đã đăng xuất tài khoản an toàn.", "info")
    return redirect(url_for('auth.login'))


# =========================================================
# PROFILE CÁ NHÂN
# =========================================================

@auth_bp.route('/profile', methods=['GET'])
@auth_bp.route('/profile/<username>', methods=['GET'])
@login_required
def profile(username=None):
    """Trang cá nhân của chính mình hoặc người dùng khác."""
    if username:
        profile_user = User.query.filter_by(username=username).first_or_404()
    else:
        profile_user = current_user

    followers_count = Follow.query.filter_by(followed_id=profile_user.id).count()
    following_count = Follow.query.filter_by(follower_id=profile_user.id).count()
    is_following = bool(
        Follow.query.filter_by(follower_id=current_user.id, followed_id=profile_user.id).first()
    ) if current_user.id != profile_user.id else False

    # Bài viết: chủ hồ sơ thấy hết; người khác chỉ thấy bài công khai (hoặc riêng tư nếu đang follow)
    my_follow_set = following_ids(current_user.id)
    raw_posts = Post.query.filter_by(user_id=profile_user.id).order_by(Post.created_at.desc()).all()
    raw_posts = [p for p in raw_posts if can_view_post(p, current_user.id, my_follow_set)]
    # Tài khoản riêng tư: chủ + người đang follow xem đầy đủ; người lạ chỉ thấy khung khóa
    profile_locked = bool(profile_user.is_private and profile_user.id != current_user.id and not is_following)
    if profile_locked:
        raw_posts = []
    posts_count = len(raw_posts)

    # Album = ảnh do chính người này đăng (không tính ảnh của bài đăng lại, không tính video)
    album_photos = []
    for p in raw_posts:
        if not p.original_post_id:
            for ph in Photo.query.filter_by(post_id=p.id).order_by(Photo.id.asc()).all():
                if ph.media_type != "video":
                    album_photos.append({"file_path": ph.file_path, "post_id": p.id, "photo_id": ph.id})

    # Tab "Bài viết": thẻ đầy đủ như ngoài bảng tin, chỉ bài của chính hồ sơ này (đã lọc quyền xem ở trên).
    # Lazy import: tránh vòng lặp import (locations.routes <-> auth).
    from app.locations.routes import build_post_cards
    from app.plans.routes import cards_for_profile
    post_cards = build_post_cards(raw_posts[:PROFILE_POSTS_CAP], current_user.id)

    # Lịch trình: chủ hồ sơ thấy hết; người khác chỉ thấy lịch trình công khai
    plans = sorted(profile_user.itineraries, key=lambda it: it.created_at or datetime.min, reverse=True)
    plans = [it for it in plans if can_view_itinerary(it, current_user.id, my_follow_set)] if not profile_locked else []
    plan_cards = cards_for_profile(plans, current_user.id)

    # Ô "Lượt thích" (số người đã tim bài của mình): chỉ chủ trang thấy
    is_owner = profile_user.id == current_user.id
    likers_count = liker_count(profile_user.id) if is_owner else None

    edit_form = EditProfileForm(obj=profile_user) if profile_user.id == current_user.id else None

    return render_template(
        'auth/profile.html',
        profile_user=profile_user,
        posts_count=posts_count,
        followers_count=followers_count,
        following_count=following_count,
        is_following=is_following,
        post_cards=post_cards,
        posts_capped=posts_count > len(post_cards),
        album_photos=album_photos,
        plans=plans,
        plan_cards=plan_cards,
        likers_count=likers_count,
        edit_form=edit_form,
        profile_locked=profile_locked
    )


@auth_bp.route('/profile/<username>/connections', methods=['GET'])
@login_required
def profile_connections(username):
    """Danh sách Follower / Following / Lượt thích của 1 người (có tìm kiếm không dấu, không phân biệt hoa thường).
    type=likes (những người đã thả tim bài của mình, mỗi người 1 lần) chỉ CHỦ TRANG xem được."""
    profile_user = User.query.filter_by(username=username).first_or_404()
    kind = request.args.get("type", "followers")
    if kind not in ("followers", "following", "likes"):
        return jsonify({"error": "type phải là followers, following hoặc likes"}), 400
    if kind == "likes" and profile_user.id != current_user.id:
        return jsonify({"error": "Chỉ chủ trang mới xem được danh sách này"}), 403

    if kind != "likes" and profile_user.is_private and profile_user.id != current_user.id:
        is_follower = Follow.query.filter_by(follower_id=current_user.id, followed_id=profile_user.id).first()
        if not is_follower:
            return jsonify({"error": "Bạn cần follow để xem"}), 403

    like_info = {}
    if kind == "likes":
        rows = likers_of(profile_user.id)
        ids = [r[0] for r in rows]
        like_info = {r[0]: (r[1], r[2]) for r in rows}
    elif kind == "followers":
        rows = (Follow.query.filter_by(followed_id=profile_user.id)
                .order_by(Follow.created_at.desc(), Follow.id.desc()).all())
        ids = [r.follower_id for r in rows]
    else:
        rows = (Follow.query.filter_by(follower_id=profile_user.id)
                .order_by(Follow.created_at.desc(), Follow.id.desc()).all())
        ids = [r.followed_id for r in rows]

    users = {u.id: u for u in User.query.filter(User.id.in_(ids or [0])).all()}
    q = norm_text(request.args.get("q", ""))
    mine = following_ids(current_user.id)

    items = []
    for uid in ids:
        u = users.get(uid)
        if not u:
            continue
        display = u.nickname or u.full_name or u.username
        if q and q not in norm_text(display + u.username + (u.full_name or "")):
            continue
        items.append({
            "id": u.id,
            "username": u.username,
            "display": display,
            "avatar_url": u.avatar_url,
            "profile_url": url_for("auth.profile", username=u.username),
            "presence": presence_state(u, current_user),
            "is_following": u.id in mine,
            "is_me": u.id == current_user.id,
        })
        if kind == "likes":
            n, last_at = like_info.get(uid, (0, None))
            items[-1]["liked_posts"] = n
            items[-1]["last_label"] = relative_label(last_at) if last_at else ""
        if len(items) >= 300:
            break

    return jsonify({"type": kind, "total": len(ids), "users": items})


@auth_bp.route('/profile/activity-status', methods=['POST'])
@login_required
def update_activity_status():
    """Bật/tắt hiển thị trạng thái hoạt động (chấm Online/Offline) của chính mình."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("show_activity"), bool):
        return jsonify({"error": "show_activity phải là true hoặc false"}), 400
    current_user.show_activity = data["show_activity"]
    db.session.commit()
    return jsonify({"show_activity": current_user.show_activity})


@auth_bp.route('/profile/privacy', methods=['POST'])
@login_required
def update_privacy():
    """Bật/tắt chế độ riêng tư của tài khoản (nút gạt trong Cài đặt)."""
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("is_private"), bool):
        return jsonify({"error": "is_private phải là true hoặc false"}), 400
    current_user.is_private = data["is_private"]
    db.session.commit()
    return jsonify({"is_private": current_user.is_private})


@auth_bp.route('/profile/edit', methods=['POST'])
@login_required
def edit_profile():
    """Chỉnh sửa thông tin cá nhân (Bio, số điện thoại, vai trò)."""
    form = EditProfileForm()
    if form.validate_on_submit():
        current_user.bio = form.bio.data.strip() if form.bio.data else None
        current_user.phone_number = form.phone_number.data.strip() if form.phone_number.data else None
        current_user.role = form.role.data
        db.session.commit()
        flash("Cập nhật hồ sơ cá nhân thành công!", "success")
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{error}", "error")

    return redirect(url_for('auth.profile'))


# =========================================================
# FOLLOW / UNFOLLOW USER
# =========================================================

@auth_bp.route("/follow/<int:user_id>", methods=["POST"])
@login_required
def follow(user_id):
    """Theo dõi người dùng khác và tạo thông báo (Notification)."""
    if current_user.id == user_id:
        return jsonify({"error": "Không thể tự follow chính mình"}), 400

    followed_user = db.session.get(User, user_id)
    if not followed_user:
        return jsonify({"error": "Không tìm thấy user"}), 404

    existing = Follow.query.filter_by(
        follower_id=current_user.id, followed_id=user_id).first()
    if existing:
        return jsonify({"error": "Đã follow rồi"}), 400

    new_follow = Follow(
        follower_id=current_user.id,
        followed_id=user_id
    )
    db.session.add(new_follow)

    notify(user_id, current_user.id, "follow")     # báo vào chuông của người được theo dõi (không trùng nếu bấm lặp)

    db.session.commit()
    return jsonify({"message": f"Đã follow user {user_id}"}), 201


@auth_bp.route("/unfollow/<int:user_id>", methods=["POST"])
@login_required
def unfollow(user_id):
    """Hủy theo dõi người dùng."""
    follow_record = Follow.query.filter_by(
        follower_id=current_user.id,
        followed_id=user_id
    ).first()

    if not follow_record:
        return jsonify({"error": "Chưa follow user này"}), 400

    db.session.delete(follow_record)
    db.session.commit()
    return jsonify({"message": f"Đã unfollow user {user_id}"})


# =========================================================
# CÀI ĐẶT TÀI KHOẢN — BẢN CỦA KHÁNH (khớp mockup, giữ nguyên /settings)
# =========================================================

@auth_bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    if request.method == "POST":
        return settings()
    return render_template("settings.html")


# =========================================================
# CÀI ĐẶT TÀI KHOẢN — BẢN CỦA PHÁT (chuyển qua /profile/settings để khỏi đụng route trên)
# =========================================================

@auth_bp.route('/profile/settings', methods=['GET', 'POST'])
@login_required
def settings():
    """Giao diện & Xử lý Cài đặt tài khoản người dùng theo Proposal GEs."""
    form = SettingsForm()

    if request.method == 'GET':
        form.nickname.data = current_user.nickname or current_user.username
        form.profile_tag.data = current_user.profile_tag or "#nguoidulich"
        form.email.data = current_user.email
        form.phone_number.data = current_user.phone_number or ""
        form.bio.data = current_user.bio or ""
        form.avatar_url.data = current_user.avatar_url or ""
        return render_template('auth/settings.html', form=form)

    if request.is_json:
        data = request.get_json(silent=True) or {}
        email = (data.get('email') or '').strip().lower()
        if email and email != current_user.email.lower():
            if User.query.filter(func.lower(User.email) == email, User.id != current_user.id).first():
                return jsonify({"error": "Địa chỉ email này đã được sử dụng bởi tài khoản khác"}), 400
            current_user.email = email

        if 'nickname' in data:
            current_user.nickname = data['nickname'].strip() if data['nickname'] else None
        if 'profile_tag' in data:
            tag = data['profile_tag'].strip()
            if tag and not tag.startswith('#'):
                tag = f"#{tag}"
            current_user.profile_tag = tag or "#nguoidulich"
        if 'phone_number' in data:
            current_user.phone_number = data['phone_number'].strip() if data['phone_number'] else None
        if 'bio' in data:
            current_user.bio = data['bio'].strip() if data['bio'] else None
        if 'avatar_url' in data:
            current_user.avatar_url = data['avatar_url'].strip() if data['avatar_url'] else None

        db.session.commit()
        return jsonify({
            "message": "Cập nhật cài đặt thành công",
            "nickname": current_user.nickname or current_user.username,
            "profile_tag": current_user.profile_tag,
            "email": current_user.email,
            "phone_number": current_user.phone_number,
            "bio": current_user.bio,
            "avatar_url": current_user.avatar_url
        })

    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        if email != current_user.email.lower():
            existing = User.query.filter(func.lower(User.email) == email, User.id != current_user.id).first()
            if existing:
                flash("Địa chỉ email này đã được sử dụng bởi tài khoản khác.", "error")
                return render_template('auth/settings.html', form=form), 400
            current_user.email = email

        current_user.nickname = form.nickname.data.strip() if form.nickname.data else None
        tag = form.profile_tag.data.strip() if form.profile_tag.data else "#nguoidulich"
        if tag and not tag.startswith('#'):
            tag = f"#{tag}"
        current_user.profile_tag = tag

        current_user.phone_number = form.phone_number.data.strip() if form.phone_number.data else None
        current_user.bio = form.bio.data.strip() if form.bio.data else None
        if form.avatar_url.data:
            current_user.avatar_url = form.avatar_url.data.strip()

        db.session.commit()
        flash("Cập nhật thông tin cài đặt thành công!", "success")
        return redirect(url_for('auth.settings'))

    return render_template('auth/settings.html', form=form), 400 if form.errors else 200


@auth_bp.route('/api/profile/tag', methods=['POST'])
@login_required
@csrf.exempt
def update_profile_tag():
    """API cập nhật nhanh nhãn hồ sơ từ modal Chọn Nhãn Hồ Sơ."""
    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    tag = (data.get('tag') or '').strip()
    if not tag:
        return jsonify({"error": "Nhãn không được để trống"}), 400

    if not tag.startswith('#'):
        tag = f"#{tag}"

    current_user.profile_tag = tag
    db.session.commit()
    return jsonify({"message": "Cập nhật nhãn thành công", "tag": current_user.profile_tag})


@auth_bp.route('/api/profile/background', methods=['POST'])
@login_required
def update_profile_background():
    """Đổi ảnh nền khung thông tin ở trang cá nhân (CSRF bật). Chỉ nhận JPG/PNG/WebP thật, tối đa 5MB."""
    if request.content_length and request.content_length > 2 * MAX_BG_BYTES + 1024 * 1024:
        return jsonify({"error": "Ảnh vượt quá 5MB."}), 413
    f = request.files.get('bg_file')
    if f is None or not f.filename:
        return jsonify({"error": "Bạn chưa chọn ảnh."}), 400
    try:
        url = save_background(f, current_user.id)
    except BgError as e:
        return jsonify({"error": str(e)}), 400
    old = current_user.profile_bg
    # Ảnh gốc (nếu client gửi kèm) -> để lần sau bấm vào ảnh nền vẫn căn chỉnh lại được từ ảnh đầy đủ, không phải từ bản đã cắt.
    # Nếu client KHÔNG gửi (căn chỉnh lại từ ảnh cũ) thì giữ ảnh gốc cũ bằng cách sao sang mã mới.
    orig_file = request.files.get('bg_original')
    if orig_file is not None and orig_file.filename:
        save_original(orig_file, url)
    else:
        prev_orig = original_url_for(old)
        if prev_orig:
            try:
                import shutil
                from app.profile_bg import URL_PREFIX, _folder
                stem = url[len(URL_PREFIX):].rsplit(".", 1)[0][len("bg_"):]
                ext = prev_orig.rsplit(".", 1)[1]
                shutil.copyfile(os.path.join(_folder(), prev_orig[len(URL_PREFIX):]), os.path.join(_folder(), f"bgo_{stem}.{ext}"))
            except Exception:
                pass
    current_user.profile_bg = url
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        remove_background_file(url)
        return jsonify({"error": "Không lưu được ảnh nền, vui lòng thử lại."}), 500
    remove_background_file(old)           # chỉ xóa file cũ SAU khi DB đã trỏ sang ảnh mới (lỗi giữa chừng không để mất ảnh)
    return jsonify({"message": "Đã đổi ảnh nền", "profile_bg_url": url, "profile_bg_original_url": original_url_for(url)})


@auth_bp.route('/api/profile/background/reset', methods=['POST'])
@login_required
def reset_profile_background():
    """Quay về nền mặc định: bỏ ảnh nền riêng và xóa file."""
    old = current_user.profile_bg
    current_user.profile_bg = None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Không thể đặt lại, vui lòng thử lại."}), 500
    remove_background_file(old)
    return jsonify({"message": "Đã quay về nền mặc định", "profile_bg_url": None})


@auth_bp.route('/api/profile/avatar', methods=['POST'])
@login_required
@csrf.exempt
def update_profile_avatar():
    """API cập nhật ảnh đại diện: hỗ trợ upload trực tiếp từ thiết bị lẫn đường dẫn URL.
    Loại bỏ hoàn toàn .svg và kiểm tra magic bytes để phòng chống Stored XSS."""
    from app.profile_bg import sniff_image_type, MAX_BG_BYTES

    if 'avatar_file' in request.files or 'file' in request.files or 'avatar' in request.files:
        uploaded_file = request.files.get('avatar_file') or request.files.get('file') or request.files.get('avatar')
        if uploaded_file and uploaded_file.filename:
            data = uploaded_file.stream.read(MAX_BG_BYTES + 1)
            uploaded_file.stream.seek(0)

            if not data or len(data) > MAX_BG_BYTES:
                return jsonify({"error": "Ảnh đại diện vượt quá dung lượng cho phép (tối đa 5MB)."}), 400

            kind = sniff_image_type(data[:16])
            if kind is None or kind not in ('jpg', 'png', 'webp'):
                return jsonify({"error": "Chỉ chấp nhận file ảnh JPG, PNG hoặc WebP hợp lệ."}), 400

            unique_filename = f"avatar_{current_user.id}_{secrets.token_hex(6)}.{kind}"
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'avatars')
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, unique_filename)
            uploaded_file.save(file_path)

            current_user.avatar_url = f"/static/uploads/avatars/{unique_filename}"
            db.session.commit()
            return jsonify({
                "message": "Cập nhật ảnh đại diện thành công",
                "avatar_url": current_user.avatar_url
            })

    data = request.get_json(silent=True) if request.is_json else request.form
    data = data or {}
    avatar_url = (data.get('avatar_url') or '').strip()
    if not avatar_url:
        return jsonify({"error": "Vui lòng chọn file ảnh từ thiết bị hoặc nhập đường dẫn ảnh hợp lệ"}), 400

    low_url = avatar_url.lower()
    if low_url.startswith(("javascript:", "data:")) or low_url.split("?", 1)[0].endswith(".svg"):
        return jsonify({"error": "Đường dẫn ảnh đại diện không hợp lệ hoặc không an toàn."}), 400

    current_user.avatar_url = avatar_url
    db.session.commit()
    return jsonify({"message": "Cập nhật ảnh đại diện thành công", "avatar_url": current_user.avatar_url})


# =========================================================
# ONBOARDING & THÔNG TIN LIÊN HỆ (DEV PACK)
# =========================================================

@auth_bp.route("/onboarding", methods=["GET", "POST"])
@login_required
def onboarding():
    """Thiết lập hồ sơ ban đầu sau khi đăng ký theo Slide 23 của Proposal."""
    if request.method == "GET":
        if current_user.profile_completed:
            return redirect(url_for("locations.feed"))
        return render_template("auth/onboarding.html")

    # Xử lý POST (hỗ trợ cả JSON API lẫn Form HTML)
    if request.is_json:
        data = request.get_json(silent=True) or {}
        nickname = data.get("nickname")
        avatar_url = data.get("avatar_url")
        tags_provided = "tags" in data
        tags = data.get("tags", [])
    else:
        tags_provided = "tags" in request.form or "tag" in request.form
        nickname = request.form.get("nickname")
        avatar_url = request.form.get("avatar_url")
        tags = request.form.getlist("tags")
        if not tags and request.form.get("tag"):
            tags = [request.form.get("tag")]

        # Hỗ trợ upload ảnh đại diện trực tiếp từ thiết bị
        if "avatar_file" in request.files or "file" in request.files or "avatar" in request.files:
            uploaded_file = request.files.get("avatar_file") or request.files.get("file") or request.files.get("avatar")
            if uploaded_file and uploaded_file.filename:
                ext = uploaded_file.filename.rsplit(".", 1)[-1].lower() if "." in uploaded_file.filename else "png"
                if ext in {"png", "jpg", "jpeg", "webp", "gif"}:
                    unique_filename = f"avatar_{current_user.id}_{secrets.token_hex(6)}.{ext}"
                    upload_dir = os.path.join(current_app.root_path, "static", "uploads", "avatars")
                    os.makedirs(upload_dir, exist_ok=True)
                    uploaded_file.save(os.path.join(upload_dir, unique_filename))
                    avatar_url = f"/static/uploads/avatars/{unique_filename}"

    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    if not isinstance(tags, list):
        if request.is_json:
            return jsonify({"error": "tags phải là một danh sách"}), 400
        flash("Danh sách nhãn không hợp lệ", "error")
        return render_template("auth/onboarding.html"), 400

    # 5 nhãn có sẵn + nhãn tự đặt ("Người đi bộ" -> nguoidibo). Chuẩn hóa/kiểm tra hoàn toàn ở server.
    tags, tags_error = normalize_tags(tags)
    if tags_error:
        if request.is_json:
            return jsonify({"error": tags_error, "allowed_tags": sorted(FIXED_TAGS)}), 400
        flash(tags_error, "error")
        return render_template("auth/onboarding.html"), 400

    if nickname:
        current_user.nickname = nickname.strip()
    if avatar_url:
        current_user.avatar_url = avatar_url.strip()
    if tags:
        current_user.tags = ",".join(tags)
        current_user.profile_tag = hashtag_for(tags[0])
    elif tags_provided:
        # Người dùng xóa hết nhãn -> xóa thật trong DB (trước đây danh sách rỗng bị bỏ qua nên nhãn cũ "hồi sinh" khi tải lại trang)
        current_user.tags = ""
        current_user.profile_tag = "#nguoidulich"

    current_user.profile_completed = True
    db.session.commit()

    if request.is_json:
        return jsonify({
            "message": "Hoàn tất onboarding",
            "user": {
                "id": current_user.id,
                "nickname": current_user.nickname,
                "avatar_url": current_user.avatar_url,
                "tags": current_user.tags,
                "profile_completed": current_user.profile_completed,
            },
        })

    flash("Hồ sơ đã được thiết lập thành công! Chào mừng bạn đến với GEs.", "success")
    return redirect(url_for("locations.feed"))


@auth_bp.route("/onboarding/skip", methods=["GET", "POST"])
@login_required
def onboarding_skip():
    """Bỏ qua bước onboarding và hoàn tất profile mặc định."""
    current_user.profile_completed = True
    db.session.commit()
    return redirect(url_for("locations.feed"))


@auth_bp.route("/notifications", methods=["GET"])
@login_required
def notifications_page():
    """Giao diện danh sách thông báo hoạt động của người dùng."""
    _migrate_legacy_share_notifications(current_user.id)
    notifications = Notification.query.filter(
        Notification.user_id == current_user.id,
        Notification.notification_type != "share",
    ).order_by(
        Notification.created_at.desc()
    ).limit(50).all()

    previews = _notification_previews(notifications)

    # Đánh dấu đã đọc
    for n in notifications:
        n.is_read = True
    db.session.commit()

    return render_template("auth/notifications.html", notifications=notifications, previews=previews)


def _migrate_legacy_share_notifications(user_id):
    """Thông báo "share" kiểu CŨ (trước khi tách riêng): nếu thật ra là một lượt ĐĂNG LẠI thì đổi thành "repost" và trỏ tới
    bài đăng lại của người đó; còn lại (copy link / tải về) thì xóa vì không còn báo loại này nữa."""
    from app.models import Post, Share
    rows = Notification.query.filter_by(user_id=user_id, notification_type="share").all()
    if not rows:
        return
    for n in rows:
        if n.post_id:
            rp = Post.query.filter_by(user_id=n.actor_id, original_post_id=n.post_id).order_by(Post.id.desc()).first()
            if rp:
                n.notification_type, n.post_id = "repost", rp.id
                continue
        elif n.itinerary_id:
            if Share.query.filter_by(user_id=n.actor_id, itinerary_id=n.itinerary_id, share_type="repost").first():
                n.notification_type = "repost"
                continue
        db.session.delete(n)
    db.session.commit()


def _notification_previews(notifications):
    """Khung xem trước bài/lịch trình cho từng thông báo (ảnh nhỏ + trích nội dung + số tim/bình luận),
    để biết ai đã thích bài NÀO mà không cần mở. Gom truy vấn theo lô (không N+1)."""
    from sqlalchemy import func
    from app.models import Post, Photo, Like, Comment, Itinerary
    post_ids = {n.post_id for n in notifications if n.post_id}
    plan_ids = {n.itinerary_id for n in notifications if n.itinerary_id}
    out = {}
    if post_ids:
        posts = {p.id: p for p in Post.query.filter(Post.id.in_(post_ids)).all()}
        # bài đăng lại luôn hiển thị nội dung của bài gốc
        content_ids = {p.id: (p.original_post_id or p.id) for p in posts.values()}
        photos = {}
        for ph in Photo.query.filter(Photo.post_id.in_(set(content_ids.values()))).order_by(Photo.id.asc()).all():
            photos.setdefault(ph.post_id, ph)
        caps = {p.id: p for p in Post.query.filter(Post.id.in_(set(content_ids.values()))).all()}
        likes = dict(db.session.query(Like.post_id, func.count(Like.id)).filter(Like.post_id.in_(post_ids)).group_by(Like.post_id).all())
        cmts = dict(db.session.query(Comment.post_id, func.count(Comment.id)).filter(Comment.post_id.in_(post_ids)).group_by(Comment.post_id).all())
        for pid, p in posts.items():
            ph = photos.get(content_ids[pid])
            cap = (p.repost_note or (caps.get(content_ids[pid]).caption if caps.get(content_ids[pid]) else p.caption) or "").strip()
            out[("post", pid)] = {
                "text": (cap[:90] + "…") if len(cap) > 90 else cap,
                "thumb": (url_for("static", filename=ph.file_path.replace("static/", "")) if ph and ph.media_type != "video" else None),
                "is_video": bool(ph and ph.media_type == "video"),
                "likes": likes.get(pid, 0), "comments": cmts.get(pid, 0),
                "in_caption": ("@" + (current_user.username or "").lower()) in (caps.get(content_ids[pid]).caption if caps.get(content_ids[pid]) and caps.get(content_ids[pid]).caption else "").lower(),
            }
    if plan_ids:
        likes = dict(db.session.query(Like.itinerary_id, func.count(Like.id)).filter(Like.itinerary_id.in_(plan_ids)).group_by(Like.itinerary_id).all())
        cmts = dict(db.session.query(Comment.itinerary_id, func.count(Comment.id)).filter(Comment.itinerary_id.in_(plan_ids)).group_by(Comment.itinerary_id).all())
        for pl in Itinerary.query.filter(Itinerary.id.in_(plan_ids)).all():
            cov = pl.cover_image
            out[("plan", pl.id)] = {
                "text": pl.title, "sub": pl.destination or "",
                "thumb": (url_for("static", filename=cov.replace("static/", "")) if cov and not cov.startswith(("http://", "https://", "/")) else cov) or None,
                "is_video": False, "likes": likes.get(pl.id, 0), "comments": cmts.get(pl.id, 0),
            }
    return out


@auth_bp.route("/settings/contact", methods=["POST"])
@login_required
def update_contact():
    """Cập nhật thông tin liên hệ: số điện thoại và email phụ."""
    data = request.get_json(silent=True) or {}
    phone_number = data.get("phone_number")
    secondary_email = data.get("secondary_email")

    if not phone_number and not secondary_email:
        return jsonify({"error": "Cần cung cấp phone_number hoặc secondary_email"}), 400

    if secondary_email:
        if "@" not in secondary_email or "." not in secondary_email:
            return jsonify({"error": "secondary_email không hợp lệ"}), 400

        if secondary_email == current_user.email:
            return jsonify({"error": "secondary_email không được trùng với email chính"}), 400

        existing = User.query.filter(
            (User.email == secondary_email) | (User.secondary_email == secondary_email)
        ).filter(User.id != current_user.id).first()
        if existing:
            return jsonify({"error": "Email này đã được dùng bởi tài khoản khác"}), 400

        current_user.secondary_email = secondary_email

    if phone_number:
        current_user.phone_number = phone_number

    db.session.commit()

    return jsonify({
        "message": "Cập nhật thông tin liên hệ thành công",
        "user": {
            "phone_number": current_user.phone_number,
            "secondary_email": current_user.secondary_email,
        },
    })


@auth_bp.route("/profile/bio", methods=["POST"])
@login_required
def update_bio():
    """Lưu tiểu sử (bio) của người dùng. Nhận JSON {"bio": "..."}; rỗng = xoá tiểu sử."""
    data = request.get_json(silent=True) or {}
    bio = data.get("bio", "")
    if bio is None:
        bio = ""
    if not isinstance(bio, str):
        return jsonify({"error": "Tiểu sử không hợp lệ."}), 400
    bio = bio.strip()
    if len(bio) > 255:
        return jsonify({"error": "Tiểu sử tối đa 255 ký tự (hiện tại %d)." % len(bio)}), 400
    current_user.bio = bio or None
    db.session.commit()
    return jsonify({"bio": current_user.bio or ""})


@auth_bp.route("/csrf-token", methods=["GET"])
def csrf_token():
    """Lấy CSRF token cho AJAX/Single Page requests."""
    return jsonify({"csrf_token": generate_csrf()})


# =========================================================
# ĐỔI MẬT KHẨU
# =========================================================

@auth_bp.route('/change-password', methods=['GET', 'POST'])
@auth_bp.route('/settings/password', methods=['GET', 'POST'])
@login_required
@limiter.limit('5 per minute', methods=['POST'])
def change_password():
    """Đổi mật khẩu người dùng (hỗ trợ cả Web Form và JSON API)."""
    form = ChangePasswordForm()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        old_password = data.get('old_password') or data.get('current_password', '')
        new_password = data.get('new_password', '')

        if not old_password or not new_password:
            return jsonify({"error": "Thiếu old_password/new_password"}), 400

        if not current_user.check_password(old_password):
            return jsonify({"error": "Mật khẩu cũ không đúng"}), 401

        if old_password == new_password:
            return jsonify({"error": "Mật khẩu mới phải khác mật khẩu cũ"}), 400

        password_error = validate_password(new_password)
        if password_error:
            return jsonify({"error": password_error}), 400

        current_user.set_password(new_password)
        db.session.commit()
        return jsonify({"message": "Đổi mật khẩu thành công"}), 200

    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            form.current_password.errors.append("Mật khẩu hiện tại không chính xác")
            return render_template('auth/change_password.html', form=form), 400

        if form.current_password.data == form.new_password.data:
            form.new_password.errors.append("Mật khẩu mới không được trùng với mật khẩu cũ")
            return render_template('auth/change_password.html', form=form), 400

        current_user.set_password(form.new_password.data)
        db.session.commit()
        flash("Đổi mật khẩu thành công! Mật khẩu mới của bạn đã có hiệu lực.", "success")
        return redirect(url_for('auth.profile'))

    return render_template('auth/change_password.html', form=form), 400 if form.errors else 200


# =========================================================
# QUÊN MẬT KHẨU & KHÔI PHỤC BẰNG OTP
# =========================================================

@auth_bp.route('/forgot-password', methods=['GET', 'POST'])
@limiter.limit('3 per minute; 10 per hour', methods=['POST'])
def forgot_password():
    """Yêu cầu khôi phục mật khẩu qua Email hoặc Số điện thoại (SĐT)."""
    if current_user.is_authenticated:
        return redirect(url_for('locations.feed'))

    form = ForgotPasswordForm()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        target = (data.get('target') or '').strip()
        method = data.get('method') or ('email' if '@' in target else 'phone')

        if not target:
            return jsonify({"error": "Vui lòng cung cấp Email hoặc Số điện thoại"}), 400

        user = None
        if '@' in target or method == 'email':
            user = User.query.filter_by(email=target).first()
            if not user:
                user = User.query.filter_by(phone_number=target).first()
        else:
            user = User.query.filter_by(phone_number=target).first()
            if not user:
                user = User.query.filter_by(email=target).first()

        if not user:
            return jsonify({"error": "Không tìm thấy tài khoản tương ứng với thông tin đã nhập"}), 404

        otp_code = f"{random.randint(100000, 999999)}"
        session['reset_user_id'] = user.id
        session['reset_otp'] = otp_code
        session['reset_target'] = target
        session['reset_method'] = method
        session['reset_expires_at'] = (datetime.utcnow() + timedelta(minutes=10)).timestamp()

        masked_target = target[:3] + "***" + target[-3:] if len(target) > 6 else target
        response_data = {
            "message": f"Mã xác nhận OTP đã được gửi đến {masked_target}",
            "target": masked_target,
        }
        # Chỉ đính kèm OTP trong JSON khi chạy môi trường phát triển / kiểm thử tự động
        if (
            current_app.config.get("DEBUG")
            or current_app.config.get("TESTING")
            or os.environ.get("DEMO_OTP_EXPOSE") == "1"
        ):
            response_data["otp"] = otp_code
        else:
            current_app.logger.info(f"[OTP DEBUG] Mã gửi tới {masked_target}: {otp_code}")

        return jsonify(response_data), 200

    if form.validate_on_submit():
        target = form.target.data.strip()
        method = form.method.data

        user = None
        if method == 'email' or '@' in target:
            user = User.query.filter_by(email=target).first()
            if not user:
                user = User.query.filter_by(phone_number=target).first()
        else:
            user = User.query.filter_by(phone_number=target).first()
            if not user:
                user = User.query.filter_by(email=target).first()

        if not user:
            channel_name = "Email" if (method == 'email' or '@' in target) else "Số điện thoại"
            form.target.errors.append(f"Không tìm thấy tài khoản nào gắn với {channel_name} này.")
            return render_template('auth/forgot_password.html', form=form), 400

        otp_code = f"{random.randint(100000, 999999)}"
        session['reset_user_id'] = user.id
        session['reset_otp'] = otp_code
        session['reset_target'] = target
        session['reset_method'] = method
        session['reset_expires_at'] = (datetime.utcnow() + timedelta(minutes=10)).timestamp()

        masked_target = target[:3] + "***" + target[-3:] if len(target) > 6 else target
        channel_label = "Email" if (method == 'email' or '@' in target) else "Số điện thoại"
        flash(f"Mã xác nhận OTP đã được gửi đến {channel_label} {masked_target}. (Mã kiểm thử: {otp_code} — có hiệu lực 10 phút)", "info")

        return redirect(url_for('auth.reset_password'))

    return render_template('auth/forgot_password.html', form=form)


@auth_bp.route('/reset-password', methods=['GET', 'POST'])
@limiter.limit('5 per minute', methods=['POST'])
def reset_password():
    """Xác thực mã OTP và Đặt lại mật khẩu mới."""
    if current_user.is_authenticated:
        return redirect(url_for('locations.feed'))

    user_id = session.get('reset_user_id')
    stored_otp = session.get('reset_otp')
    expires_at = session.get('reset_expires_at', 0)
    target = session.get('reset_target', '')
    method = session.get('reset_method', 'email')

    if not user_id or not stored_otp:
        flash("Vui lòng gửi yêu cầu khôi phục mật khẩu trước.", "warning")
        return redirect(url_for('auth.forgot_password'))

    if datetime.utcnow().timestamp() > expires_at:
        session.pop('reset_user_id', None)
        session.pop('reset_otp', None)
        session.pop('reset_expires_at', None)
        session.pop('reset_target', None)
        flash("Mã xác nhận OTP đã hết hạn (quá 10 phút). Vui lòng yêu cầu mã mới.", "error")
        return redirect(url_for('auth.forgot_password'))

    form = ResetPasswordForm()

    if request.is_json:
        data = request.get_json(silent=True) or {}
        otp = (data.get('otp') or '').strip()
        new_pwd = data.get('new_password', '')
        confirm_pwd = data.get('confirm_new_password', '')

        if otp != stored_otp:
            return jsonify({"error": "Mã xác thực OTP không chính xác"}), 400

        if new_pwd != confirm_pwd:
            return jsonify({"error": "Mật khẩu xác nhận không khớp"}), 400

        if len(new_pwd) < 8 or not re.search(r'[A-Z]', new_pwd) or not re.search(r'[a-z]', new_pwd) or not re.search(r'[@$!%*?&#^()_+\-=\[\]{};:\'",.<>\/\\|`~]', new_pwd):
            return jsonify({"error": "Mật khẩu mới phải từ 8 ký tự, gồm chữ hoa, chữ thường và ký tự đặc biệt"}), 400

        user = db.session.get(User, user_id)
        if not user:
            return jsonify({"error": "Tài khoản không tồn tại"}), 404

        if user.check_password(new_pwd):
            return jsonify({"error": "Mật khẩu mới không được trùng với mật khẩu cũ"}), 400

        user.set_password(new_pwd)
        db.session.commit()

        session.pop('reset_user_id', None)
        session.pop('reset_otp', None)
        session.pop('reset_expires_at', None)
        session.pop('reset_target', None)
        session.pop('reset_method', None)

        return jsonify({"message": "Đặt lại mật khẩu thành công"}), 200

    if form.validate_on_submit():
        if form.otp.data.strip() != stored_otp:
            form.otp.errors.append("Mã xác thực OTP không chính xác.")
            return render_template('auth/reset_password.html', form=form, target=target, method=method), 400

        user = db.session.get(User, user_id)
        if not user:
            flash("Tài khoản không tồn tại hoặc đã bị xóa.", "error")
            return redirect(url_for('auth.forgot_password'))

        if user.check_password(form.new_password.data):
            form.new_password.errors.append("Mật khẩu mới không được trùng với mật khẩu cũ.")
            return render_template('auth/reset_password.html', form=form, target=target, method=method), 400

        user.set_password(form.new_password.data)
        db.session.commit()

        session.pop('reset_user_id', None)
        session.pop('reset_otp', None)
        session.pop('reset_expires_at', None)
        session.pop('reset_target', None)
        session.pop('reset_method', None)

        flash("Đặt lại mật khẩu thành công! Bạn có thể đăng nhập bằng mật khẩu mới.", "success")
        return redirect(url_for('auth.login'))

    return render_template('auth/reset_password.html', form=form, target=target, method=method), 400 if form.errors else 200


@auth_bp.route('/resend-otp', methods=['POST'])
@limiter.limit('3 per minute; 10 per hour')
def resend_otp():
    """Gửi lại mã xác nhận OTP mới."""
    user_id = session.get('reset_user_id')
    target = session.get('reset_target')
    method = session.get('reset_method', 'email')

    if not user_id or not target:
        if request.is_json:
            return jsonify({"error": "Không tìm thấy phiên khôi phục mật khẩu hợp lệ"}), 400
        flash("Vui lòng thực hiện yêu cầu khôi phục mật khẩu từ đầu.", "warning")
        return redirect(url_for('auth.forgot_password'))

    new_otp = f"{random.randint(100000, 999999)}"
    session['reset_otp'] = new_otp
    session['reset_expires_at'] = (datetime.utcnow() + timedelta(minutes=10)).timestamp()

    masked_target = target[:3] + "***" + target[-3:] if len(target) > 6 else target
    channel_label = "Email" if method == 'email' else "Số điện thoại"

    if request.is_json:
        return jsonify({
            "message": f"Đã gửi lại mã OTP đến {masked_target}",
            "otp": new_otp
        }), 200

    flash(f"Mã OTP mới đã được gửi lại tới {channel_label} {masked_target}. (Mã kiểm thử: {new_otp})", "info")
    return redirect(url_for('auth.reset_password'))


# ===========================================================================
# ĐĂNG NHẬP BẰNG GOOGLE (OAuth 2.0 Authorization Code) — chỉ dùng urllib chuẩn
# ===========================================================================

def _google_exchange_code(code, redirect_uri):
    """Đổi authorization code lấy access_token từ Google."""
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": current_app.config["GOOGLE_CLIENT_ID"],
        "client_secret": current_app.config["GOOGLE_CLIENT_SECRET"],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _google_get_userinfo(access_token):
    """Lấy thông tin người dùng (sub, email, name, picture) từ Google."""
    req = urllib.request.Request(
        GOOGLE_USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _unique_username(base):
    """Sinh username chưa bị dùng, thêm số phía sau nếu trùng."""
    base = (base or "user")[:50]
    username = base
    n = 1
    while User.query.filter_by(username=username).first():
        n += 1
        username = f"{base}{n}"
    return username


@auth_bp.route("/auth/google/login", methods=["GET"])
def google_login():
    """Khởi động quy trình đăng nhập Google OAuth."""
    if not current_app.config.get("GOOGLE_CLIENT_ID") or not current_app.config.get("GOOGLE_CLIENT_SECRET"):
        return jsonify({"error": "Chưa cấu hình GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET trong .env"}), 500

    state = secrets.token_urlsafe(32)
    session["google_oauth_state"] = state

    params = urllib.parse.urlencode({
        "client_id": current_app.config["GOOGLE_CLIENT_ID"],
        "redirect_uri": url_for("auth.google_callback", _external=True),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })
    return redirect(f"{GOOGLE_AUTH_URL}?{params}")


@auth_bp.route("/auth/google/callback", methods=["GET"])
def google_callback():
    """Xử lý phản hồi từ Google sau khi người dùng đồng ý đăng nhập."""
    if request.args.get("error"):
        return jsonify({"error": "Bạn đã hủy đăng nhập Google"}), 400

    expected_state = session.pop("google_oauth_state", None)
    if not expected_state or request.args.get("state") != expected_state:
        return jsonify({"error": "State không hợp lệ, vui lòng đăng nhập lại"}), 400

    code = request.args.get("code")
    if not code:
        return jsonify({"error": "Thiếu authorization code"}), 400

    redirect_uri = url_for("auth.google_callback", _external=True)
    try:
        token = _google_exchange_code(code, redirect_uri)
        userinfo = _google_get_userinfo(token["access_token"])
    except urllib.error.HTTPError as e:
        current_app.logger.error("Google HTTPError %s: %s", e.code, e.read().decode(errors="replace"))
        return jsonify({"error": "Google từ chối yêu cầu, vui lòng thử lại"}), 502
    except (urllib.error.URLError, KeyError, ValueError) as e:
        current_app.logger.error("Google OAuth lỗi: %r", e)
        return jsonify({"error": "Không kết nối được với Google, vui lòng thử lại"}), 502

    google_id = userinfo.get("sub")
    email = userinfo.get("email")
    if not google_id or not email or not userinfo.get("email_verified"):
        return jsonify({"error": "Tài khoản Google chưa có email đã xác minh"}), 400

    user = User.query.filter_by(google_id=google_id).first()

    if not user:
        user = User.query.filter_by(email=email).first()
        if user:
            user.google_id = google_id
        else:
            user = User(
                username=_unique_username(email.split("@")[0]),
                email=email,
                google_id=google_id,
                avatar_url=userinfo.get("picture"),
                nickname=userinfo.get("name"),
            )
            user.set_password(secrets.token_urlsafe(32))
            db.session.add(user)
        db.session.commit()

    login_user(user)

    if not user.profile_completed:
        return redirect(url_for("auth.onboarding"))

    return redirect(url_for("auth.index"))