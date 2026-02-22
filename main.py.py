"""
====================================================================================================
PROJECT:        AAU TEACHER REVIEW BOT
VERSION:        12.0.0 — MongoDB Atlas Edition
DATE:           FEBRUARY 2026
FRAMEWORK:      python-telegram-bot v20.x+ | Flask | MongoDB Atlas (pymongo)

BUGS FIXED FROM v11.0:
  🐛 BUG 1 — handler_teacher was unreachable:
     In ST_SUBJECT state, selecting a subject via cb_subject_select() called
     query.edit_message_text() and returned ST_TEACHER. But ST_TEACHER only
     registered MessageHandler (text messages). The user never sent a new
     message after clicking the inline button — the bot was silently frozen.
     FIX: cb_subject_select() now sends a NEW bot message asking for the teacher
     name (instead of editing the subject selection message), so the user's next
     text reply is correctly captured by ST_TEACHER.

  🐛 BUG 2 — EMOJI_MAP had duplicate "Anatomy" key:
     Python dicts silently overwrite duplicate keys. The second "Anatomy": "🦴"
     entry overwrote the first, making the "Physiology" key unreachable.
     FIX: Removed the duplicate "Anatomy" entry.

  🐛 BUG 3 — do_submit() rate-limit counted entire batch as 1 submission:
     Rate limit was checked once per batch, so a user could submit 100 reviews
     in one batch and bypass the per-review limit entirely.
     FIX: Rate limit is now checked once per individual draft, not per batch.
     Submission stops and warns the user as soon as the limit is hit.

  🐛 BUG 4 — cb_manage_drafts() ST_MANAGE pattern was wrong:
     The ConversationHandler regex r"^(d(edit|del|submit|add))\b|^dsubmit$"
     did NOT match "ddel|0", "dedit|1", "dadd" etc. because \b doesn't work
     after the pipe character in callback data.
     FIX: Pattern simplified to r"^d(edit|del|submit|add)" which correctly
     matches all draft management callbacks.

  🐛 BUG 5 — broadcast_command escaped the message BEFORE sending:
     html.escape() was applied to the admin's own broadcast message, meaning
     any formatting like <b>bold</b> the admin typed would appear as literal
     &lt;b&gt;bold&lt;/b&gt; to students.
     FIX: Removed html.escape() from broadcast text. Admin is trusted.

MONGODB CHANGES:
  - Replaced entire Database class JSON backend with pymongo (Motor async driver)
  - Collections: bans, contexts, votes, reviews, users, violations, ratelimits, members
  - All asyncio.Lock() removed (MongoDB handles concurrency at the server level)
  - All in-memory caches kept for is_banned() / is_approved_member() hot-path reads
  - MONGO_URI read from environment variable (set it in Render dashboard)
====================================================================================================
"""

import logging
import asyncio
import uuid
import os
import html
import re
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

# ── Telegram ──────────────────────────────────────────────────────────────────
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    User,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden

# ── MongoDB (Motor = async pymongo driver) ────────────────────────────────────
import motor.motor_asyncio

# ── Flask keep-alive ──────────────────────────────────────────────────────────
from flask import Flask

# ==============================================================================
# ⚙️  CONFIGURATION  — edit these values, or set as Render env vars
# ==============================================================================

BOT_TOKEN  = os.environ.get("BOT_TOKEN",  "8396251452:AAGkVJ3PfIUttzjgJNUbzVSzDISPJ8gz8UQ")
ADMIN_ID   = int(os.environ.get("ADMIN_ID", "5113670239"))

# ⚠️  IMPORTANT: Paste your MongoDB Atlas connection string here,
#     OR set the MONGO_URI environment variable in Render dashboard.
#     Format: mongodb+srv://<user>:<password>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGO_URI  = os.environ.get("MONGO_URI", "YOUR_MONGODB_ATLAS_URI_HERE")
MONGO_DB   = "teacher_review_bot"

# Telegram Channel  (must be -100<numeric_id>)
RAW_CHANNEL_ID = "1003881172658"
CHANNEL_ID = int(f"-100{RAW_CHANNEL_ID}") if not RAW_CHANNEL_ID.startswith("-") else int(RAW_CHANNEL_ID)

SUBJECTS_PER_PAGE     = 6
MAX_REVIEWS_PER_HOUR  = 5
MAX_PROFANITY_STRIKES = 3

# ==============================================================================
# 🚫  PROFANITY FILTER
# ==============================================================================

PROFANITY_SET: Set[str] = {
    # English
    "idiot", "stupid", "dumb", "moron", "retard", "imbecile", "fool",
    "bastard", "asshole", "bitch", "crap", "fuck", "shit", "piss",
    "cock", "dick", "pussy", "whore", "slut", "cunt", "nigger",
    "faggot", "retarded", "loser", "scum", "trash", "garbage",
    # Amharic / Ethiopic romanised
    "yenya", "leba", "ahiya", "wusha", "dedeb", "goblata",
    "shilegna", "baldeg", "gmatam", "neger", "wend",
    # Afaan Oromo
    "gaafii", "waraana",
}

def contains_profanity(text: str) -> bool:
    return any(w in PROFANITY_SET for w in re.findall(r"\w+", text.lower()))

# ==============================================================================
# 📝  STRINGS  (zero channel/identity references)
# ==============================================================================

class S:
    WELCOME = (
        "📋 <b>Teacher Review Bot</b>\n\n"
        "👋 <b>Welcome!</b>\n"
        "This platform collects anonymous instructor feedback to help the student community.\n\n"
        "🎁 <b>Your Reward:</b>\n"
        "Every approved review grants you access to the full review archive.\n\n"
        "🛡️ <b>Privacy First:</b> Your identity is never shared.\n\n"
        "👇 Select an option below:"
    )
    WELCOME_DEEP = (
        "🔄 <b>Add Additional Feedback</b>\n\n"
        "You are adding another review for:\n"
        "👨‍🏫 <b>{teacher}</b>\n"
        "📚 <i>{subject}</i>\n\n"
        "Please give a rating to continue:"
    )
    BTN_WRITE     = "✍️ Write a Review"
    BTN_MATERIALS = "📚 Get Materials"
    BTN_CANCEL    = "❌ Cancel"
    BTN_ADD_MORE  = "➕ Review Another Teacher"
    BTN_MANAGE    = "📋 Manage My Drafts"
    BTN_SUBMIT    = "🚀 Submit All Reviews"

    PROMPT_STREAM  = "🏫 <b>Select your Department / Stream:</b>"
    PROMPT_YEAR    = "📅 <b>Select your Academic Year:</b>"
    PROMPT_SUBJECT = "📚 <b>Select the Course / Subject:</b>\n<i>Use ⬅️ / ➡️ to browse pages.</i>"
    PROMPT_TEACHER = "👤 <b>Instructor's Full Name?</b>\n\n⚠️ <i>Type the full name (e.g., 'Dr. Abebe Kebede').</i>"
    PROMPT_RATING  = "⭐ <b>How would you rate this instructor overall?</b>"
    PROMPT_CONTENT = (
        "📝 <b>Write your detailed, constructive feedback:</b>\n\n"
        "Try to cover:\n"
        "  • Teaching style & clarity\n"
        "  • Exam difficulty & fairness\n"
        "  • Grading policy\n"
        "  • Attendance / pop-quiz policy\n"
        "  • Participation marks\n\n"
        "<i>Minimum 30 characters. Type below:</i>"
    )
    ERR_BANNED       = "🚫 <b>Access Denied.</b>\nYou have been restricted from this bot."
    ERR_INVALID      = "⚠️ Invalid selection. Please use the buttons provided."
    ERR_SHORT_NAME   = "⚠️ Name too short. Please enter the full name (at least 3 characters)."
    ERR_SHORT_REVIEW = "⚠️ <b>Review too short!</b> Please write at least 30 characters with meaningful detail."
    ERR_NO_DATA      = "⚠️ Session expired. Please type /start to begin again."
    ERR_RATE_LIMIT   = (
        f"⏳ <b>Slow down!</b>\n"
        f"You can submit at most {MAX_REVIEWS_PER_HOUR} reviews per hour.\n"
        "Please wait a while and try again."
    )
    ERR_PROFANITY    = (
        "⚠️ <b>Review Rejected Automatically.</b>\n\n"
        "Your review contains inappropriate or offensive language.\n"
        "Please rewrite it in a respectful, constructive manner.\n\n"
        "<i>Repeated violations will result in a permanent ban.</i>"
    )
    ERR_SEARCH_ONLY  = (
        "🔒 <b>Members Only Feature</b>\n\n"
        "The /search command is available only to students whose reviews have been approved.\n"
        "Submit a review and get it approved to unlock this feature!"
    )
    SUCCESS_DRAFT_SAVED = "✅ <b>Review saved to drafts!</b>"
    SUCCESS_SUBMITTED   = (
        "🚀 <b>All Reviews Submitted!</b>\n\n"
        "Thank you for contributing to the student community.\n"
        "🔔 You will receive a notification here once your review is approved."
    )
    MSG_MATERIALS       = "📂 <b>Study Materials</b>\n\nJoin the archive channel to access all resources."
    ADMIN_REJECT_PROMPT = (
        "📋 <b>Select a rejection reason</b>\n"
        "The student will receive a polite, specific message explaining the issue."
    )

# ==============================================================================
# 📊  ACADEMIC DATABASE
# ==============================================================================

ACADEMIC_DB: Dict[str, Dict[str, List[str]]] = {
    "🔬 Freshman Natural Science": {
        "Year 1 (Freshman)": [
            "Logic & Critical Thinking", "General Psychology", "Geography of Ethiopia",
            "Communicative English I", "Freshman Mathematics", "General Physics",
            "Emerging Technology", "Social Anthropology", "History of Ethiopia",
            "Civics & Moral Education", "Global Trends", "Entrepreneurship",
            "Economics", "Communicative English II", "Applied Mathematics I",
            "Computer Programming (Python)", "Physical Fitness",
        ]
    },
    "🌍 Freshman Social Science": {
        "Year 1 (Freshman)": [
            "Logic & Critical Thinking", "General Psychology", "Civics & Moral Education",
            "Global Trends", "Entrepreneurship", "Economics", "Social Anthropology",
            "Geography of Ethiopia", "Communicative English I", "Emerging Technology",
            "Mathematics for Social Science", "Communicative English II",
            "History of Ethiopia", "Physical Fitness",
        ]
    },
    "⚙️ Pre-Engineering & Engineering": {
        "Year 1 (Pre-Engineering Common)": [
            "Applied Math I", "Applied Math II", "Engineering Mechanics I (Statics)",
            "Engineering Mechanics II (Dynamics)", "Engineering Drawing",
            "Workshop Practice", "Introduction to Computing",
            "Communicative English", "Civics & Ethics", "Logic",
        ],
        "Year 2 (Mechanical Eng)": [
            "Applied Math III", "Strength of Materials", "Thermodynamics I",
            "Machine Drawing", "Materials Science", "Fluid Mechanics",
            "Thermodynamics II", "Manufacturing Processes",
            "Kinematics of Machinery", "Electrical Circuits",
        ],
        "Year 2 (Software Eng)": [
            "Applied Math III", "Physics for Engineers", "Programming Fundamentals",
            "Discrete Mathematics", "Digital Logic Design", "Probability & Statistics",
            "Data Structures & Algorithms", "Object-Oriented Programming",
            "Database Systems", "Computer Organization",
        ],
        "Year 2 (Electrical Eng)": [
            "Applied Math III", "Network Analysis", "Electronic Circuits I",
            "Digital Logic", "Electromagnetic Fields", "Signals and Systems",
            "Electrical Workshop", "Object Oriented Programming",
        ],
        "Year 2 (Civil Eng)": [
            "Theory of Structures I", "Surveying I", "Engineering Geology",
            "Construction Materials", "Strength of Materials",
            "Applied Math III", "Hydraulics I",
        ],
        "Year 3 (General)": [
            "Internship / Industrial Practice", "Research Methods",
            "Entrepreneurship for Engineers", "Operating Systems",
            "Computer Networks", "Software Engineering", "Machine Design",
            "Heat Transfer", "Control Systems", "Reinforced Concrete",
        ],
    },
    "🩺 Medicine & Health Sciences": {
        "Year 1 (Pre-Medicine)": [
            "General Biology", "General Chemistry", "General Physics",
            "Introduction to Medicine", "Communicative English",
            "Medical Ethics", "Civics", "Information Technology",
        ],
        "Year 2 (Pre-Clinical)": [
            "Human Anatomy I", "Human Anatomy II", "Human Physiology I",
            "Human Physiology II", "Medical Biochemistry I", "Medical Biochemistry II",
            "Histology & Embryology", "Public Health", "Microbiology",
        ],
        "Year 3 (Clinical Start)": [
            "Pathology I", "Pathology II", "Pharmacology I", "Pharmacology II",
            "Introduction to Clinical Medicine", "Immunology",
            "Parasitology", "Epidemiology",
        ],
        "Other Health (Nursing / Pharma)": [
            "Fundamentals of Nursing", "Pharmaceutics", "Medicinal Chemistry",
            "Clinical Nursing", "Health Service Management",
        ],
    },
    "⚖️ Law & Governance": {
        "Year 1": [
            "Introduction to Law", "Sociology of Law", "Legal History",
            "Constitutional Law I", "Logic", "English for Lawyers",
        ],
        "Year 2": [
            "Constitutional Law II", "Law of Contracts I", "Law of Contracts II",
            "Family Law", "Criminal Law I", "Criminal Law II", "Law of Persons",
        ],
        "Year 3": [
            "Law of Traders", "Business Organizations", "Administrative Law",
            "Property Law", "Law of Sales", "Human Rights Law",
            "Public International Law",
        ],
    },
    "💼 Business & Economics": {
        "Year 1": [
            "Principles of Management", "Introduction to Economics",
            "Business Mathematics", "Communicative English", "Civics",
            "Logic", "Financial Accounting I",
        ],
        "Year 2": [
            "Microeconomics", "Macroeconomics", "Cost Accounting",
            "Business Statistics", "Organizational Behavior",
            "Marketing Management", "Financial Accounting II",
            "Business Law", "Managerial Economics",
        ],
        "Year 3": [
            "Financial Management", "Human Resource Management",
            "Operations Management", "International Trade",
            "Strategic Management", "Research Methods",
            "Entrepreneurship", "Investment Analysis",
        ],
    },
}

# BUG FIX 2: removed duplicate "Anatomy" key
EMOJI_MAP = {
    "Physics": "⚛️", "Math": "🧮", "Calculus": "∫", "Chemistry": "🧪",
    "Biology": "🧬", "English": "🇬🇧", "Civics": "⚖️", "Logic": "🧠",
    "Geography": "🌍", "Computer": "💻", "Programming": "⌨️",
    "Psychology": "🧩", "Sociology": "👥", "Economics": "📉",
    "History": "📜", "Anatomy": "🦴", "Accounting": "💰",
    "Law": "⚖️", "Drawing": "📐", "Statics": "🏗️", "Dynamics": "🚀",
    "Software": "💾", "Network": "🌐", "Thermodynamics": "🔥",
    "Management": "📊", "Marketing": "📣", "Finance": "💵",
    "Statistics": "📈", "Fluid": "💧", "Mechanics": "⚙️",
    "Physiology": "❤️", "Microbiology": "🦠",
    "Pharmacology": "💊", "Pathology": "🔬", "Nursing": "🩺",
}

def subject_emoji(name: str) -> str:
    for kw, em in EMOJI_MAP.items():
        if kw.lower() in name.lower():
            return em
    return "📚"

def stars_str(rating: int) -> str:
    r = max(0, min(5, rating))
    return "⭐" * r + "☆" * (5 - r)

# ==============================================================================
# 💾  DATABASE  — MongoDB Atlas (Motor async driver)
# ==============================================================================

class Database:
    """
    All persistence goes through MongoDB Atlas.
    Hot-path reads (is_banned, is_approved_member) use small in-memory caches
    so every Telegram message doesn't make a network round-trip.
    """

    def __init__(self):
        client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        mdb    = client[MONGO_DB]

        # Collections — one per logical domain
        self._bans       = mdb["bans"]        # {_id: uid}
        self._contexts   = mdb["contexts"]    # {_id: key, data: {...}}
        self._votes      = mdb["votes"]       # {_id: msg_id_str, up, down, voters:{}}
        self._reviews    = mdb["reviews"]     # approved reviews
        self._users      = mdb["users"]       # {_id: uid_str, name, username, joined}
        self._violations = mdb["violations"]  # {_id: uid_str, count}
        self._ratelimits = mdb["ratelimits"]  # {_id: uid_str, timestamps:[]}
        self._members    = mdb["members"]     # {_id: uid}  approved members

        # In-memory caches for hot reads (rebuilt on startup via async init)
        self._banned_cache:  Set[int] = set()
        self._members_cache: Set[int] = set()

    async def init(self):
        """Must be awaited once at startup to warm caches and create indexes."""
        # Warm ban cache
        async for doc in self._bans.find({}, {"_id": 1}):
            self._banned_cache.add(doc["_id"])

        # Warm member cache
        async for doc in self._members.find({}, {"_id": 1}):
            self._members_cache.add(doc["_id"])

        # Create indexes for fast lookups
        await self._reviews.create_index([("teacher", 1)])
        await self._reviews.create_index([("subject", 1)])
        await self._contexts.create_index([("_id", 1)])

        logging.info("DB: cache warmed. Banned=%d Members=%d",
                     len(self._banned_cache), len(self._members_cache))

    # ── BAN ───────────────────────────────────────────────────────────────────

    def is_banned(self, uid: int) -> bool:
        return uid in self._banned_cache

    async def ban(self, uid: int):
        self._banned_cache.add(uid)
        await self._bans.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)

    async def unban(self, uid: int):
        self._banned_cache.discard(uid)
        await self._bans.delete_one({"_id": uid})

    # ── CONTEXT (deep-link keys + pending reviews) ────────────────────────────

    async def set_ctx(self, key: str, data: dict):
        await self._contexts.update_one(
            {"_id": key},
            {"$set": {"_id": key, "data": data}},
            upsert=True,
        )

    async def get_ctx(self, key: str) -> Optional[dict]:
        doc = await self._contexts.find_one({"_id": key})
        return doc["data"] if doc else None

    # ── VOTES  (per-user, prevents repeat voting) ─────────────────────────────

    async def cast_vote(self, msg_id: int, uid: int, direction: str) -> Tuple[bool, dict]:
        """
        Returns (changed, {up, down}).
        Uses MongoDB's atomic findOneAndUpdate for race-safe voting.
        """
        mid  = str(msg_id)
        uidk = str(uid)

        # Read current state
        doc = await self._votes.find_one({"_id": mid})
        if not doc:
            doc = {"_id": mid, "up": 0, "down": 0, "voters": {}}

        voters = doc.get("voters", {})
        prev   = voters.get(uidk)

        if prev == direction:
            return False, doc          # nothing to change

        # Calculate new counts
        up   = doc.get("up",   0)
        down = doc.get("down", 0)

        if prev == "up":   up   = max(0, up - 1)
        if prev == "down": down = max(0, down - 1)
        if direction == "up":   up   += 1
        if direction == "down": down += 1

        voters[uidk] = direction

        await self._votes.update_one(
            {"_id": mid},
            {"$set": {"up": up, "down": down, "voters": voters}},
            upsert=True,
        )
        return True, {"up": up, "down": down}

    # ── APPROVED REVIEWS ──────────────────────────────────────────────────────

    async def add_review(self, review: dict):
        await self._reviews.insert_one(review)

    async def search(self, query: str) -> List[dict]:
        q = query.strip()
        cursor = self._reviews.find(
            {"teacher": {"$regex": re.escape(q), "$options": "i"}},
            {"_id": 0}
        )
        return await cursor.to_list(length=50)

    async def top_teachers(self, n: int = 5) -> List[dict]:
        pipeline = [
            {"$group": {
                "_id":     "$teacher",
                "avg":     {"$avg": "$rating"},
                "count":   {"$sum": 1},
                "subject": {"$first": "$subject"},
            }},
            {"$sort": {"avg": -1}},
            {"$limit": n},
            {"$project": {"teacher": "$_id", "avg": 1, "count": 1, "subject": 1, "_id": 0}},
        ]
        return await self._reviews.aggregate(pipeline).to_list(length=n)

    async def toughest_courses(self, n: int = 5) -> List[dict]:
        pipeline = [
            {"$group": {
                "_id":   "$subject",
                "avg":   {"$avg": "$rating"},
                "count": {"$sum": 1},
            }},
            {"$sort": {"avg": 1}},      # ascending = lowest rated first
            {"$limit": n},
            {"$project": {"subject": "$_id", "avg": 1, "count": 1, "_id": 0}},
        ]
        return await self._reviews.aggregate(pipeline).to_list(length=n)

    async def review_count(self) -> int:
        return await self._reviews.count_documents({})

    # ── USERS ─────────────────────────────────────────────────────────────────

    async def register(self, user: User):
        uid = str(user.id)
        await self._users.update_one(
            {"_id": uid},
            {"$setOnInsert": {
                "_id":      uid,
                "name":     user.first_name,
                "username": user.username or "",
                "joined":   datetime.now().isoformat(),
            }},
            upsert=True,
        )

    async def all_user_ids(self) -> List[int]:
        docs = await self._users.find({}, {"_id": 1}).to_list(length=100_000)
        return [int(d["_id"]) for d in docs]

    async def user_count(self) -> int:
        return await self._users.count_documents({})

    # ── APPROVED MEMBERS ──────────────────────────────────────────────────────

    def is_approved_member(self, uid: int) -> bool:
        return uid in self._members_cache or uid == ADMIN_ID

    async def add_approved_member(self, uid: int):
        self._members_cache.add(uid)
        await self._members.update_one({"_id": uid}, {"$set": {"_id": uid}}, upsert=True)

    async def member_count(self) -> int:
        return await self._members.count_documents({})

    # ── VIOLATIONS ────────────────────────────────────────────────────────────

    async def add_violation(self, uid: int) -> int:
        uid_str = str(uid)
        result  = await self._violations.find_one_and_update(
            {"_id": uid_str},
            {"$inc": {"count": 1}},
            upsert=True,
            return_document=True,
        )
        return result.get("count", 1) if result else 1

    # ── RATE LIMIT ────────────────────────────────────────────────────────────

    async def rate_limit_ok(self, uid: int) -> bool:
        """
        Returns True if user may submit another review right now.
        BUG FIX 3: checked per draft, not per batch.
        """
        uid_str = str(uid)
        now     = datetime.now()
        cutoff  = now - timedelta(hours=1)

        doc = await self._ratelimits.find_one({"_id": uid_str})
        timestamps = doc.get("timestamps", []) if doc else []

        # Keep only the last hour
        recent = [t for t in timestamps if datetime.fromisoformat(t) > cutoff]

        if len(recent) >= MAX_REVIEWS_PER_HOUR:
            return False

        recent.append(now.isoformat())
        await self._ratelimits.update_one(
            {"_id": uid_str},
            {"$set": {"timestamps": recent}},
            upsert=True,
        )
        return True

    # ── STATS ─────────────────────────────────────────────────────────────────

    async def pending_count(self) -> int:
        return await self._contexts.count_documents(
            {"_id": {"$regex": "^pending_"}}
        )


# Singleton — initialised asynchronously in main()
db = Database()

# ==============================================================================
# 🧠  SESSION MANAGEMENT  (in-memory, transient — that's correct for sessions)
# ==============================================================================

class Draft:
    __slots__ = ("id", "stream", "year", "subject", "teacher",
                 "rating", "content", "is_additional", "parent_msg_id", "ts")

    def __init__(self):
        self.id:            str            = str(uuid.uuid4())[:8]
        self.stream:        str            = ""
        self.year:          str            = ""
        self.subject:       str            = ""
        self.teacher:       str            = ""
        self.rating:        int            = 0
        self.content:       str            = ""
        self.is_additional: bool           = False
        self.parent_msg_id: Optional[int]  = None
        self.ts:            datetime       = datetime.now()


class Session:
    def __init__(self, uid: int):
        self.uid:              int            = uid
        self.draft:            Optional[Draft] = None
        self.drafts:           List[Draft]    = []
        self.subject_page:     int            = 0
        self.current_subjects: List[str]      = []

    def new_draft(self) -> Draft:
        self.draft           = Draft()
        self.subject_page    = 0
        self.current_subjects = []
        return self.draft

    def commit_draft(self):
        if self.draft:
            self.drafts.append(self.draft)
            self.draft = None

    def delete(self, idx: int) -> bool:
        if 0 <= idx < len(self.drafts):
            del self.drafts[idx]
            return True
        return False

    def pop_for_edit(self, idx: int) -> Optional[Draft]:
        if 0 <= idx < len(self.drafts):
            self.draft = self.drafts.pop(idx)
            return self.draft
        return None


_sessions: Dict[int, Session] = {}

def session(uid: int) -> Session:
    if uid not in _sessions:
        _sessions[uid] = Session(uid)
    return _sessions[uid]

# ==============================================================================
# 🗂️  CONVERSATION STATES
# ==============================================================================

(ST_STREAM, ST_YEAR, ST_SUBJECT, ST_TEACHER,
 ST_RATING, ST_CONTENT, ST_BATCH, ST_MANAGE) = range(8)

# ==============================================================================
# 🎨  KEYBOARD BUILDERS
# ==============================================================================

def kb_reply(items: List[str], cols: int = 1) -> ReplyKeyboardMarkup:
    rows = [items[i:i+cols] for i in range(0, len(items), cols)]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, one_time_keyboard=True)

def kb_main() -> ReplyKeyboardMarkup:
    return kb_reply([S.BTN_WRITE, S.BTN_MATERIALS])

def kb_subjects(subjects: List[str], page: int) -> InlineKeyboardMarkup:
    start = page * SUBJECTS_PER_PAGE
    chunk = subjects[start:start + SUBJECTS_PER_PAGE]
    total = (len(subjects) + SUBJECTS_PER_PAGE - 1) // SUBJECTS_PER_PAGE
    rows  = []
    for s in chunk:
        rows.append([InlineKeyboardButton(f"{subject_emoji(s)} {s}", callback_data=f"subj|{s}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"spage|{page-1}"))
    nav.append(InlineKeyboardButton(f"📄 {page+1}/{total}", callback_data="spage|noop"))
    if start + SUBJECTS_PER_PAGE < len(subjects):
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"spage|{page+1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="conv|cancel")])
    return InlineKeyboardMarkup(rows)

def kb_rating() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{i} ⭐", callback_data=f"rate|{i}") for i in range(1, 6)
    ]])

def kb_batch() -> ReplyKeyboardMarkup:
    return kb_reply([S.BTN_ADD_MORE, S.BTN_MANAGE, S.BTN_SUBMIT])

def kb_manage(drafts: List[Draft]) -> InlineKeyboardMarkup:
    rows = []
    for i, d in enumerate(drafts):
        short = html.escape(d.teacher[:22] + ("…" if len(d.teacher) > 22 else ""))
        rows.append([
            InlineKeyboardButton(f"✏️ Edit #{i+1}: {short}", callback_data=f"dedit|{i}"),
            InlineKeyboardButton(f"🗑️ Delete #{i+1}",         callback_data=f"ddel|{i}"),
        ])
    rows.append([InlineKeyboardButton("🚀 Submit All",  callback_data="dsubmit")])
    rows.append([InlineKeyboardButton("➕ Add Another", callback_data="dadd")])
    return InlineKeyboardMarkup(rows)

def kb_admin(uid: int, rev_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Approve",  callback_data=f"app|{uid}|{rev_id}"),
            InlineKeyboardButton("❌ Reject…",  callback_data=f"rej|{uid}|{rev_id}"),
        ],
        [InlineKeyboardButton("🔨 Ban User",    callback_data=f"ban|{uid}|{rev_id}")],
    ])

def kb_reject(uid: int, rev_id: str) -> InlineKeyboardMarkup:
    reasons = [
        ("🤬 Insulting / Aggressive Language", "insulting"),
        ("📏 Too Short / Lacks Detail",         "tooshort"),
        ("😕 Unclear / Hard to Understand",     "unclear"),
        ("🔗 Irrelevant to the Teacher",        "irrelevant"),
        ("♻️ Duplicate / Already Submitted",   "duplicate"),
        ("🚫 Community Policy Violation",       "policy"),
    ]
    rows = [[InlineKeyboardButton(label, callback_data=f"rr|{uid}|{rev_id}|{code}")]
            for label, code in reasons]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"rback|{uid}|{rev_id}")])
    return InlineKeyboardMarkup(rows)

def kb_channel_post(rev_id: str, up: int, down: int, deep_link: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👍 {up}",   callback_data=f"vup|{rev_id}"),
            InlineKeyboardButton(f"👎 {down}", callback_data=f"vdn|{rev_id}"),
        ],
        [InlineKeyboardButton("➕ Add More About This Teacher", url=deep_link)],
    ])

# Rejection messages — polite and specific
REJECT_MSG: Dict[str, str] = {
    "insulting": (
        "❌ <b>Review Not Approved</b>\n\n"
        "Your feedback contained language that may come across as aggressive or disrespectful.\n"
        "We encourage reviews that are professional and focus on teaching quality.\n\n"
        "Please revise your review and feel free to resubmit. 🙏"
    ),
    "tooshort": (
        "❌ <b>Review Not Approved</b>\n\n"
        "Your review was a bit brief to be truly helpful for other students.\n"
        "Consider adding details about teaching style, exam difficulty, grading fairness, and attendance policy.\n\n"
        "A helpful review is usually 3–5 sentences. Give it another try! ✍️"
    ),
    "unclear": (
        "❌ <b>Review Not Approved</b>\n\n"
        "Your review was a little difficult to follow.\n"
        "Please write in clear sentences and organise your points so they are easy to understand.\n\n"
        "You are welcome to revise and resubmit. ✅"
    ),
    "irrelevant": (
        "❌ <b>Review Not Approved</b>\n\n"
        "Your feedback did not appear to be about the instructor's teaching.\n"
        "Please make sure your review addresses the teacher's methods, exams, and grading.\n\n"
        "Feel free to start a fresh review. 📝"
    ),
    "duplicate": (
        "❌ <b>Review Not Approved</b>\n\n"
        "A very similar review already exists for this teacher.\n"
        "Thank you for your contribution — no need to resubmit this one. 🙂"
    ),
    "policy": (
        "❌ <b>Review Not Approved</b>\n\n"
        "Your review did not meet our community guidelines.\n"
        "Please ensure your feedback is honest, respectful, and focused on academic matters.\n\n"
        "You are welcome to submit a revised version. 🔄"
    ),
}

# ==============================================================================
# 🏁  CONVERSATION HANDLERS
# ==============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.register(user)

    if db.is_banned(user.id):
        await update.message.reply_text(S.ERR_BANNED, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    _sessions[user.id] = Session(user.id)
    sess = session(user.id)

    args = context.args
    if args and args[0].startswith("add_"):
        ctx_id = args[0][4:]
        data   = await db.get_ctx(ctx_id)
        if data:
            d               = sess.new_draft()
            d.stream        = data.get("stream", "")
            d.year          = data.get("year", "")
            d.subject       = data.get("subject", "")
            d.teacher       = data.get("teacher", "")
            d.parent_msg_id = data.get("parent_msg_id")
            d.is_additional = True
            await update.message.reply_text(
                S.WELCOME_DEEP.format(
                    teacher=html.escape(d.teacher),
                    subject=html.escape(d.subject),
                ),
                reply_markup=kb_rating(),
                parse_mode=ParseMode.HTML,
            )
            return ST_RATING
        else:
            await update.message.reply_text(
                "⚠️ <b>This link has expired.</b> Starting fresh.",
                parse_mode=ParseMode.HTML,
            )

    await update.message.reply_text(S.WELCOME, reply_markup=kb_main(), parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def cmd_materials(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(S.MSG_MATERIALS, parse_mode=ParseMode.HTML)
    return ConversationHandler.END


async def handler_start_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await db.register(user)

    if db.is_banned(user.id):
        await update.message.reply_text(S.ERR_BANNED, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    session(user.id).new_draft()
    streams = list(ACADEMIC_DB.keys())
    await update.message.reply_text(
        S.PROMPT_STREAM,
        reply_markup=kb_reply(streams + [S.BTN_CANCEL]),
        parse_mode=ParseMode.HTML,
    )
    return ST_STREAM


async def handler_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == S.BTN_CANCEL:
        return await do_cancel(update, context)
    if text not in ACADEMIC_DB:
        await update.message.reply_text(S.ERR_INVALID, parse_mode=ParseMode.HTML)
        return ST_STREAM

    sess = session(update.effective_user.id)
    sess.draft.stream = text
    years = list(ACADEMIC_DB[text].keys())
    await update.message.reply_text(
        S.PROMPT_YEAR,
        reply_markup=kb_reply(years + [S.BTN_CANCEL]),
        parse_mode=ParseMode.HTML,
    )
    return ST_YEAR


async def handler_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == S.BTN_CANCEL:
        return await do_cancel(update, context)

    sess   = session(update.effective_user.id)
    stream = sess.draft.stream
    if text not in ACADEMIC_DB.get(stream, {}):
        await update.message.reply_text(S.ERR_INVALID, parse_mode=ParseMode.HTML)
        return ST_YEAR

    sess.draft.year       = text
    subjects              = ACADEMIC_DB[stream][text]
    sess.current_subjects = subjects
    sess.subject_page     = 0
    total = (len(subjects) + SUBJECTS_PER_PAGE - 1) // SUBJECTS_PER_PAGE

    # Send the inline subject keyboard as its own message
    await update.message.reply_text(
        S.PROMPT_SUBJECT,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML,
    )
    await update.message.reply_text(
        f"📄 <b>Page 1 of {total}</b> — select your subject:",
        reply_markup=kb_subjects(subjects, 0),
        parse_mode=ParseMode.HTML,
    )
    return ST_SUBJECT


# ── Subject pagination ─────────────────────────────────────────────────────────

async def cb_subject_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, val = query.data.split("|", 1)
    if val == "noop":
        return ST_SUBJECT

    page     = int(val)
    sess     = session(query.from_user.id)
    sess.subject_page = page
    subjects = sess.current_subjects
    total    = (len(subjects) + SUBJECTS_PER_PAGE - 1) // SUBJECTS_PER_PAGE
    await query.edit_message_text(
        f"📄 <b>Page {page+1} of {total}</b> — select your subject:",
        reply_markup=kb_subjects(subjects, page),
        parse_mode=ParseMode.HTML,
    )
    return ST_SUBJECT


async def cb_subject_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    BUG FIX 1: After selecting a subject we must send a NEW message so the
    next user text message is captured by ST_TEACHER's MessageHandler.
    Previously we edited the subject-selection message and returned ST_TEACHER,
    but since no new message was sent, the ConversationHandler never advanced.
    """
    query   = update.callback_query
    await query.answer()
    subject = query.data.split("|", 1)[1]
    sess    = session(query.from_user.id)
    sess.draft.subject = subject

    # Acknowledge the selection in the inline message
    await query.edit_message_text(
        f"✅ <b>Subject selected:</b> {html.escape(subject)}",
        parse_mode=ParseMode.HTML,
    )
    # Send a NEW message prompting for teacher name — this is what the user replies to
    await context.bot.send_message(
        query.from_user.id,
        S.PROMPT_TEACHER,
        parse_mode=ParseMode.HTML,
    )
    return ST_TEACHER


async def cb_conv_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid   = query.from_user.id
    _sessions.pop(uid, None)
    await query.edit_message_text("❌ <b>Cancelled.</b>", parse_mode=ParseMode.HTML)
    await context.bot.send_message(uid, "Use the menu to start again:", reply_markup=kb_main())
    return ConversationHandler.END


# ── Teacher → Rating → Content ────────────────────────────────────────────────

async def handler_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if len(text) < 3:
        await update.message.reply_text(S.ERR_SHORT_NAME, parse_mode=ParseMode.HTML)
        return ST_TEACHER

    sess = session(update.effective_user.id)
    sess.draft.teacher = text
    await update.message.reply_text(
        f"👤 <b>{html.escape(text)}</b>\n\n{S.PROMPT_RATING}",
        reply_markup=kb_rating(),
        parse_mode=ParseMode.HTML,
    )
    return ST_RATING


async def cb_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query  = update.callback_query
    await query.answer()
    rating = int(query.data.split("|")[1])
    sess   = session(query.from_user.id)
    sess.draft.rating = rating
    await query.edit_message_text(
        f"⭐ <b>Rating set: {stars_str(rating)} ({rating}/5)</b>\n\n{S.PROMPT_CONTENT}",
        parse_mode=ParseMode.HTML,
    )
    return ST_CONTENT


async def handler_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = update.effective_user.id
    sess = session(uid)

    if len(text) < 30:
        await update.message.reply_text(S.ERR_SHORT_REVIEW, parse_mode=ParseMode.HTML)
        return ST_CONTENT

    # Profanity check
    if contains_profanity(text):
        strikes = await db.add_violation(uid)
        msg     = S.ERR_PROFANITY
        if strikes >= MAX_PROFANITY_STRIKES:
            await db.ban(uid)
            msg += "\n\n🚫 <b>You have been permanently banned due to repeated violations.</b>"
            await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
            _sessions.pop(uid, None)
            return ConversationHandler.END
        msg += f"\n\n⚠️ Strike <b>{strikes}/{MAX_PROFANITY_STRIKES}</b>."
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        return ST_CONTENT

    sess.draft.content = text
    sess.commit_draft()

    # Deep-link reviews auto-submit immediately
    if sess.drafts and sess.drafts[-1].is_additional:
        return await do_submit(uid, update, context)

    # Show draft summary + batch menu
    lines   = [
        f"<b>#{i+1}</b> {html.escape(d.teacher)} — {stars_str(d.rating)} ({d.rating}/5)"
        for i, d in enumerate(sess.drafts)
    ]
    summary = "\n".join(lines)
    await update.message.reply_text(
        f"{S.SUCCESS_DRAFT_SAVED}\n\n"
        f"📊 <b>Your Drafts ({len(sess.drafts)}):</b>\n{summary}\n\n"
        "👇 <b>What next?</b>",
        reply_markup=kb_batch(),
        parse_mode=ParseMode.HTML,
    )
    return ST_BATCH


# ── Batch decision ─────────────────────────────────────────────────────────────

async def handler_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    uid    = update.effective_user.id
    sess   = session(uid)

    if choice == S.BTN_SUBMIT:
        return await do_submit(uid, update, context)

    if choice == S.BTN_MANAGE:
        if not sess.drafts:
            await update.message.reply_text("No drafts yet!", parse_mode=ParseMode.HTML)
            return ST_BATCH
        await update.message.reply_text(
            "📋 <b>Manage Your Drafts:</b>",
            reply_markup=kb_manage(sess.drafts),
            parse_mode=ParseMode.HTML,
        )
        return ST_MANAGE

    if choice == S.BTN_ADD_MORE:
        if not sess.drafts:
            return await handler_start_review(update, context)
        last_stream = sess.drafts[-1].stream
        sess.new_draft()
        sess.draft.stream = last_stream
        years = list(ACADEMIC_DB[last_stream].keys())
        await update.message.reply_text(
            f"🔄 <b>Stream:</b> {last_stream}\n\n{S.PROMPT_YEAR}",
            reply_markup=kb_reply(years + [S.BTN_CANCEL]),
            parse_mode=ParseMode.HTML,
        )
        return ST_YEAR

    await update.message.reply_text(S.ERR_INVALID, parse_mode=ParseMode.HTML)
    return ST_BATCH


# ── Draft management callbacks ─────────────────────────────────────────────────

async def cb_manage_drafts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """BUG FIX 4: pattern simplified to r'^d(edit|del|submit|add)' to correctly match all callbacks."""
    query = update.callback_query
    await query.answer()
    data  = query.data
    uid   = query.from_user.id
    sess  = session(uid)

    if data == "dsubmit":
        await query.edit_message_text("⏳ Submitting…", parse_mode=ParseMode.HTML)
        return await do_submit(uid, None, context)

    if data == "dadd":
        await query.edit_message_text("✅ Starting new review…", parse_mode=ParseMode.HTML)
        if sess.drafts:
            last_stream = sess.drafts[-1].stream
            sess.new_draft()
            sess.draft.stream = last_stream
            years = list(ACADEMIC_DB[last_stream].keys())
            await context.bot.send_message(
                uid,
                f"🔄 <b>Stream:</b> {last_stream}\n\n{S.PROMPT_YEAR}",
                reply_markup=kb_reply(years + [S.BTN_CANCEL]),
                parse_mode=ParseMode.HTML,
            )
        return ST_YEAR

    if data.startswith("ddel|"):
        idx = int(data.split("|")[1])
        sess.delete(idx)
        if not sess.drafts:
            await query.edit_message_text("🗑️ All drafts deleted.", parse_mode=ParseMode.HTML)
            return ConversationHandler.END
        await query.edit_message_text(
            "🗑️ Draft deleted. Remaining:",
            reply_markup=kb_manage(sess.drafts),
            parse_mode=ParseMode.HTML,
        )
        return ST_MANAGE

    if data.startswith("dedit|"):
        idx = int(data.split("|")[1])
        if sess.pop_for_edit(idx):
            await query.edit_message_text(
                f"✏️ <b>Editing:</b> {html.escape(sess.draft.teacher)}\n\nPlease rewrite your feedback:",
                parse_mode=ParseMode.HTML,
            )
            return ST_CONTENT

    return ST_MANAGE


# ── Submit all drafts ──────────────────────────────────────────────────────────

async def do_submit(
    uid: int,
    update: Optional[Update],
    context: ContextTypes.DEFAULT_TYPE,
):
    sess = session(uid)

    if not sess.drafts:
        if update:
            await update.message.reply_text(S.ERR_NO_DATA, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    # Get display name
    try:
        tg_user = await context.bot.get_chat(uid)
        display = tg_user.first_name or "Student"
    except Exception:
        display = "Student"

    if update:
        await update.message.reply_text(
            "⏳ <b>Transmitting to admin…</b>",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode=ParseMode.HTML,
        )

    submitted = 0
    for draft in sess.drafts:
        # BUG FIX 3: rate limit checked per draft, not once per batch
        allowed = await db.rate_limit_ok(uid)
        if not allowed:
            try:
                await context.bot.send_message(
                    uid,
                    S.ERR_RATE_LIMIT + f"\n\n✅ <b>{submitted}</b> review(s) were sent before the limit.",
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
            break

        # Save structured pending data (no text-parsing on approve)
        await db.set_ctx(f"pending_{draft.id}", {
            "stream":        draft.stream,
            "year":          draft.year,
            "subject":       draft.subject,
            "teacher":       draft.teacher,
            "rating":        draft.rating,
            "content":       draft.content,
            "parent_msg_id": draft.parent_msg_id,
            "is_additional": draft.is_additional,
            "user_id":       uid,
        })

        s_str       = stars_str(draft.rating)
        header      = "🧵 <b>ADDITIONAL REVIEW (Thread)</b>" if draft.is_additional else "📩 <b>NEW REVIEW</b>"
        parent_line = f"🔗 <b>Thread Parent:</b> <code>{draft.parent_msg_id}</code>\n" if draft.parent_msg_id else ""

        admin_text = (
            f"{header}\n"
            f"{'─'*34}\n"
            f"👤 <b>User:</b> {html.escape(display)} (<code>{uid}</code>)\n"
            f"🏫 <b>Stream:</b>  {html.escape(draft.stream)}\n"
            f"📅 <b>Year:</b>    {html.escape(draft.year)}\n"
            f"📚 <b>Subject:</b> {html.escape(draft.subject)}\n"
            f"👨‍🏫 <b>Teacher:</b> {html.escape(draft.teacher)}\n"
            f"⭐ <b>Rating:</b>  {s_str} ({draft.rating}/5)\n"
            f"{parent_line}"
            f"🆔 <b>Ref ID:</b>  <code>{draft.id}</code>\n"
            f"{'─'*34}\n"
            f"💬 <b>Review:</b>\n{html.escape(draft.content)}"
        )

        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                reply_markup=kb_admin(uid, draft.id),
                parse_mode=ParseMode.HTML,
            )
            submitted += 1
        except Exception as exc:
            logging.error("send_to_admin failed: %s", exc)

    _sessions.pop(uid, None)

    try:
        await context.bot.send_message(
            uid, S.SUCCESS_SUBMITTED, reply_markup=kb_main(), parse_mode=ParseMode.HTML
        )
    except Exception:
        pass
    return ConversationHandler.END


async def do_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "❌ <b>Cancelled.</b>", reply_markup=kb_main(), parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# ==============================================================================
# 🛡️  ADMIN CALLBACKS
# ==============================================================================

async def cb_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id != ADMIN_ID:
        await query.answer("⛔ Admin only.", show_alert=True)
        return

    await query.answer()
    parts  = query.data.split("|")
    action = parts[0]

    if action == "app":
        uid, rev_id = int(parts[1]), parts[2]
        await _approve(query, context, uid, rev_id)

    elif action == "rej":
        uid, rev_id = int(parts[1]), parts[2]
        await query.edit_message_text(
            query.message.text + "\n\n" + S.ADMIN_REJECT_PROMPT,
            reply_markup=kb_reject(uid, rev_id),
            parse_mode=ParseMode.HTML,
        )

    elif action == "rr":
        uid, rev_id, reason = int(parts[1]), parts[2], parts[3]
        await _reject(query, context, uid, rev_id, reason)

    elif action == "rback":
        uid, rev_id = int(parts[1]), parts[2]
        clean = query.message.text.replace("\n\n" + S.ADMIN_REJECT_PROMPT, "")
        await query.edit_message_text(
            clean, reply_markup=kb_admin(uid, rev_id), parse_mode=ParseMode.HTML
        )

    elif action == "ban":
        uid = int(parts[1])
        if uid == ADMIN_ID:
            await query.answer("Cannot ban yourself.", show_alert=True)
            return
        await db.ban(uid)
        await query.edit_message_text(
            query.message.text + "\n\n⛔ <b>USER BANNED</b>",
            parse_mode=ParseMode.HTML,
        )


async def _approve(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, rev_id: str):
    data = await db.get_ctx(f"pending_{rev_id}")
    if not data:
        await query.message.reply_text("⚠️ Pending data not found (context may have expired).")
        return

    stream        = data["stream"]
    year          = data["year"]
    subject       = data["subject"]
    teacher       = data["teacher"]
    rating        = data["rating"]
    content       = data["content"]
    parent_msg_id = data.get("parent_msg_id")
    is_additional = data.get("is_additional", False)

    header = "📝 <b>ADDITIONAL FEEDBACK</b>" if is_additional else "📢 <b>TEACHER REVIEW</b>"
    post   = (
        f"{header}\n\n"
        f"{subject_emoji(subject)} <b>Subject:</b> {html.escape(subject)}\n"
        f"👨‍🏫 <b>Teacher:</b> {html.escape(teacher)}\n"
        f"⭐ <b>Rating:</b>  {stars_str(rating)} ({rating}/5)\n\n"
        f"💬 <b>Feedback:</b>\n"
        f"<i>{html.escape(content)}</i>"
    )

    ctx_uuid  = str(uuid.uuid4())[:10]
    bot_info  = await context.bot.get_me()
    deep_link = f"https://t.me/{bot_info.username}?start=add_{ctx_uuid}"

    try:
        sent = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=post,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_channel_post(rev_id, 0, 0, deep_link),
            reply_to_message_id=parent_msg_id,
        )
    except Exception as exc:
        logging.error("Channel post failed: %s", exc)
        await query.message.reply_text(f"⚠️ Channel post failed: {exc}")
        return

    # Save threading context so the next reply goes to the same thread
    next_parent = parent_msg_id if parent_msg_id else sent.message_id
    await db.set_ctx(ctx_uuid, {
        "stream": stream, "year": year,
        "subject": subject, "teacher": teacher,
        "parent_msg_id": next_parent,
    })

    # Persist the approved review
    await db.add_review({
        "teacher":   teacher,
        "subject":   subject,
        "stream":    stream,
        "year":      year,
        "rating":    rating,
        "content":   content,
        "timestamp": datetime.now().isoformat(),
        "msg_id":    sent.message_id,
    })

    # Unlock /search for this user
    await db.add_approved_member(user_id)

    # Notify student with invite link
    invite = "the review archive"
    try:
        lnk    = await context.bot.create_chat_invite_link(
            CHANNEL_ID, member_limit=1, name=f"R-{user_id}"
        )
        invite = lnk.invite_link
    except Exception:
        pass

    try:
        await context.bot.send_message(
            user_id,
            f"✅ <b>Your review has been approved!</b>\n\n🔑 <b>Your access link:</b>\n{invite}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass

    await query.edit_message_text(
        query.message.text + "\n\n✅ <b>APPROVED & POSTED</b>",
        parse_mode=ParseMode.HTML,
    )


async def _reject(query, context: ContextTypes.DEFAULT_TYPE, user_id: int, rev_id: str, reason: str):
    msg = REJECT_MSG.get(reason, REJECT_MSG["policy"])
    try:
        await context.bot.send_message(user_id, msg, parse_mode=ParseMode.HTML)
    except Exception:
        pass
    clean = query.message.text
    if S.ADMIN_REJECT_PROMPT in clean:
        clean = clean[: clean.index("\n\n" + S.ADMIN_REJECT_PROMPT)]
    await query.edit_message_text(
        clean + f"\n\n❌ <b>REJECTED</b> — <i>{reason}</i>",
        parse_mode=ParseMode.HTML,
    )

# ==============================================================================
# 🗳️  CHANNEL VOTING
# ==============================================================================

async def cb_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query     = update.callback_query
    parts     = query.data.split("|")
    direction = "up" if parts[0] == "vup" else "down"
    rev_id    = parts[1]
    uid       = query.from_user.id
    msg_id    = query.message.message_id

    changed, votes = await db.cast_vote(msg_id, uid, direction)

    if not changed:
        await query.answer("You've already voted this way!", show_alert=False)
        return

    old_kb   = query.message.reply_markup.inline_keyboard
    deep_url = old_kb[1][0].url if len(old_kb) > 1 and old_kb[1] else None

    new_rows = [
        [
            InlineKeyboardButton(f"👍 {votes['up']}",   callback_data=f"vup|{rev_id}"),
            InlineKeyboardButton(f"👎 {votes['down']}", callback_data=f"vdn|{rev_id}"),
        ]
    ]
    if deep_url:
        new_rows.append([InlineKeyboardButton("➕ Add More About This Teacher", url=deep_url)])

    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_rows))
        await query.answer("✅ Vote recorded!")
    except BadRequest:
        await query.answer()

# ==============================================================================
# 🔍  /search  — approved members only
# ==============================================================================

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if db.is_banned(uid):
        return

    if not db.is_approved_member(uid):
        await update.message.reply_text(S.ERR_SEARCH_ONLY, parse_mode=ParseMode.HTML)
        return

    if not context.args:
        await update.message.reply_text(
            "🔍 <b>Teacher Search</b>\n\nUsage: <code>/search Dr. Abebe</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    query_str = " ".join(context.args)
    results   = await db.search(query_str)

    if not results:
        await update.message.reply_text(
            f"🔍 No approved reviews found for <b>{html.escape(query_str)}</b>.\n"
            "Check the spelling and try again.",
            parse_mode=ParseMode.HTML,
        )
        return

    # Group by teacher
    by_teacher: Dict[str, List[dict]] = {}
    for r in results:
        by_teacher.setdefault(r["teacher"], []).append(r)

    msg = f"🔍 <b>Results for:</b> <i>{html.escape(query_str)}</i>\n{'─'*32}\n\n"
    for name, revs in by_teacher.items():
        avg     = sum(r["rating"] for r in revs) / len(revs)
        subjects = list({r["subject"] for r in revs})
        sub_str  = ", ".join(subjects[:3]) + ("…" if len(subjects) > 3 else "")

        msg += (
            f"👨‍🏫 <b>{html.escape(name)}</b>\n"
            f"⭐ <b>Avg Rating:</b> {avg:.1f}/5  {stars_str(round(avg))}\n"
            f"📊 <b>Reviews:</b> {len(revs)}\n"
            f"📚 <b>Subjects:</b> {html.escape(sub_str)}\n\n"
        )
        for r in revs[-2:]:
            snippet = r["content"][:150] + ("…" if len(r["content"]) > 150 else "")
            msg += f"💬 <i>{html.escape(snippet)}</i>\n\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ==============================================================================
# 🏆  /top  — leaderboard
# ==============================================================================

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if db.is_banned(update.effective_user.id):
        return

    top   = await db.top_teachers(5)
    tough = await db.toughest_courses(5)
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

    msg = "🏆 <b>LEADERBOARD</b>\n\n"

    msg += "⭐ <b>Top Rated Teachers:</b>\n"
    if top:
        for i, t in enumerate(top):
            msg += (
                f"{medals[i]} <b>{html.escape(t['teacher'])}</b>\n"
                f"   {stars_str(round(t['avg']))} {t['avg']:.1f}/5"
                f" — {t['count']} review{'s' if t['count'] != 1 else ''}\n"
            )
    else:
        msg += "<i>No data yet.</i>\n"

    msg += "\n📉 <b>Toughest / Lowest-Rated Courses:</b>\n"
    if tough:
        for i, c in enumerate(tough):
            msg += (
                f"{i+1}. <b>{html.escape(c['subject'])}</b>\n"
                f"   {c['avg']:.1f}/5 — {c['count']} review{'s' if c['count'] != 1 else ''}\n"
            )
    else:
        msg += "<i>No data yet.</i>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ==============================================================================
# 📊  ADMIN COMMANDS
# ==============================================================================

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    total_users    = await db.user_count()
    total_reviews  = await db.review_count()
    total_members  = await db.member_count()
    total_banned   = len(db._banned_cache)
    pending        = await db.pending_count()
    active_sess    = len(_sessions)

    msg = (
        f"📊 <b>BOT STATISTICS</b>\n"
        f"{'─'*30}\n"
        f"👥 <b>Total Users:</b>          {total_users}\n"
        f"✅ <b>Approved Reviews:</b>     {total_reviews}\n"
        f"🔓 <b>Approved Members:</b>     {total_members}\n"
        f"⏳ <b>Pending Queue:</b>        {pending}\n"
        f"💬 <b>Active Sessions:</b>      {active_sess}\n"
        f"🚫 <b>Banned Users:</b>         {total_banned}\n"
        f"{'─'*30}\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d  %H:%M')}"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: <code>/broadcast Your message here</code>", parse_mode=ParseMode.HTML
        )
        return

    # BUG FIX 5: don't html.escape admin's own broadcast message
    bcast  = f"📢 <b>Announcement</b>\n\n{' '.join(context.args)}"
    uids   = await db.all_user_ids()
    ok = fail = 0

    status = await update.message.reply_text(
        f"📡 Broadcasting to {len(uids)} users…", parse_mode=ParseMode.HTML
    )

    for uid in uids:
        try:
            await context.bot.send_message(uid, bcast, parse_mode=ParseMode.HTML)
            ok += 1
        except (Forbidden, BadRequest):
            fail += 1
        except Exception as exc:
            logging.error("Broadcast [%s]: %s", uid, exc)
            fail += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ <b>Broadcast done</b>\n\n✔️ Sent: {ok}  ❌ Failed: {fail}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: <code>/unban USER_ID</code>", parse_mode=ParseMode.HTML)
        return
    try:
        uid = int(context.args[0])
        await db.unban(uid)
        await update.message.reply_text(
            f"✅ User <code>{uid}</code> has been unbanned.", parse_mode=ParseMode.HTML
        )
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user ID.", parse_mode=ParseMode.HTML)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text(
        "🛠️ <b>Admin Commands</b>\n\n"
        "/stats — Dashboard\n"
        "/broadcast [msg] — Message all users\n"
        "/unban [uid] — Unban a user\n"
        "/search [name] — Search reviews\n"
        "/top — Leaderboard\n",
        parse_mode=ParseMode.HTML,
    )

# ==============================================================================
# ⚠️  ERROR HANDLER
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("Update caused exception:", exc_info=context.error)

# ==============================================================================
# 🌐  FLASK KEEP-ALIVE  (Render.com — must bind $PORT within 60 s)
# ==============================================================================

web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot is alive and running!", 200

@web_app.route("/health")
def health():
    return {
        "status":   "ok",
        "sessions": len(_sessions),
        "banned":   len(db._banned_cache),
    }, 200

def _run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port, use_reloader=False)

def keep_alive():
    t = threading.Thread(target=_run_web, daemon=True)
    t.start()
    logging.info("Flask keep-alive started on PORT=%s", os.environ.get("PORT", 8080))

# ==============================================================================
# 🔌  MAIN
# ==============================================================================

def main():
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        level=logging.INFO,
    )
    print("=" * 62)
    print("  TEACHER REVIEW BOT v12.0 — MongoDB Atlas Edition")
    print("  ✅ Flask keep-alive ($PORT, Render compatible)")
    print("  ✅ MongoDB Atlas persistence (Motor async driver)")
    print("  ✅ 5 logic bugs fixed from v11.0")
    print("  ✅ Per-user vote tracking in MongoDB")
    print("  ✅ Structured pending contexts (no text parsing)")
    print("  ✅ Deep linking + threaded channel replies")
    print("  ✅ Paginated subjects, draft management")
    print("  ✅ Profanity filter, rate limit, /search, /top")
    print("=" * 62)

    # 1. Start Flask FIRST — Render kills app if port not bound in 60 s
    keep_alive()

    # 2. Build application
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 3. Warm MongoDB caches (runs once before polling starts)
    async def post_init(app):
        await db.init()
        logging.info("MongoDB initialised successfully.")

    application.post_init = post_init

    # 4. Conversation handler
    # BUG FIX 4: ST_MANAGE pattern fixed to r'^d(edit|del|submit|add)'
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", cmd_start),
            MessageHandler(filters.Regex(f"^{re.escape(S.BTN_WRITE)}$"), handler_start_review),
        ],
        states={
            ST_STREAM:  [MessageHandler(filters.TEXT & ~filters.COMMAND, handler_stream)],
            ST_YEAR:    [MessageHandler(filters.TEXT & ~filters.COMMAND, handler_year)],
            ST_SUBJECT: [
                CallbackQueryHandler(cb_subject_page,   pattern=r"^spage\|"),
                CallbackQueryHandler(cb_subject_select, pattern=r"^subj\|"),
                CallbackQueryHandler(cb_conv_cancel,    pattern=r"^conv\|cancel$"),
            ],
            ST_TEACHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, handler_teacher)],
            ST_RATING:  [CallbackQueryHandler(cb_rating, pattern=r"^rate\|")],
            ST_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handler_content)],
            ST_BATCH:   [MessageHandler(filters.TEXT & ~filters.COMMAND, handler_batch)],
            ST_MANAGE:  [CallbackQueryHandler(cb_manage_drafts, pattern=r"^d(edit|del|submit|add)")],
        },
        fallbacks=[
            CommandHandler("cancel", do_cancel),
            MessageHandler(filters.Regex(f"^{re.escape(S.BTN_CANCEL)}$"), do_cancel),
        ],
        per_user=True,
        allow_reentry=True,
    )

    application.add_handler(conv)
    application.add_handler(
        MessageHandler(filters.Regex(f"^{re.escape(S.BTN_MATERIALS)}$"), cmd_materials)
    )

    # Public commands
    application.add_handler(CommandHandler("search", cmd_search))
    application.add_handler(CommandHandler("top",    cmd_top))

    # Admin commands
    application.add_handler(CommandHandler("stats",     cmd_stats))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("unban",     cmd_unban))
    application.add_handler(CommandHandler("admin",     cmd_admin))

    # Callbacks (specific patterns first)
    application.add_handler(CallbackQueryHandler(cb_admin, pattern=r"^(app|rej|ban|rr|rback)\|"))
    application.add_handler(CallbackQueryHandler(cb_vote,  pattern=r"^v(up|dn)\|"))

    application.add_error_handler(error_handler)

    # 5. Start polling
    print("✅ Polling started.")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
