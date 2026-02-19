"""
====================================================================================================
PROJECT:        AAU TEACHER REVIEW BOT (ENTERPRISE EDITION)
AUTHOR:         NEXTGEN DEVELOPMENT TEAM
DATE:           FEBRUARY 19, 2026
VERSION:        9.0.0 (Production Release)
LOCATION:       ADDIS ABABA, ETHIOPIA
FRAMEWORK:      python-telegram-bot (v20.x+) & Flask

DESCRIPTION:
----------------------------------------------------------------------------------------------------
This is a full-scale, persistent Telegram bot designed for Addis Ababa University (AAU).
It allows students to anonymously review instructors with a specific focus on the Ethiopian
Higher Education curriculum (Freshman, Pre-Engineering, Medicine, etc.).

KEY FEATURES:
1.  **Hierarchical Course Selection**: Stream -> Year -> Subject.
2.  **Batch Processing**: Students can draft multiple reviews and submit them in one go.
3.  **Deep Linking**: "Add More Info" button in the channel links back to the bot with context.
4.  **Threaded Channel Posts**: Additional reviews appear as replies to the original post.
5.  **Voting System**: Live Like/Dislike buttons on channel posts.
6.  **Persistent Storage**: Auto-saves data to JSON files (No data loss on restart).
7.  **Admin Dashboard**: Ban users, Approve/Reject reviews, View Stats.
8.  **Safety**: HTML escaping to prevent formatting crashes.
9.  **Keep-Alive**: Integrated Web Server for free hosting on Render.com.

CONTACT:
----------------------------------------------------------------------------------------------------
Technical Support: @NextGen_helper
Public Channel: @AAU_freshman2026
====================================================================================================
"""

import logging
import asyncio
import uuid
import json
import os
import html
import threading
from datetime import datetime
from typing import Dict, List, Optional, Set, Any, Union

# Telegram API Imports
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ChatInviteLink,
    InputMediaPhoto,
    User,
    Message
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ConversationHandler,
    Application
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import TelegramError, BadRequest, Forbidden, NetworkError

# Flask for Render Deployment
from flask import Flask

# ==============================================================================
# ⚙️ CONFIGURATION & CONSTANTS
# ==============================================================================

# 🔐 SECURITY
# Replace with your actual Token
BOT_TOKEN = "8396251452:AAGkVJ3PfIUttzjgJNUbzVSzDISPJ8gz8UQ"
ADMIN_ID = 5113670239

# 📡 CHANNELS
# The -100 prefix is required for Supergroups/Channels in Telegram API
RAW_CHANNEL_ID = "1003881172658"
CHANNEL_ID = int(f"-{RAW_CHANNEL_ID}") if not RAW_CHANNEL_ID.startswith("-") else int(RAW_CHANNEL_ID)

# 🔗 LINKS
PUBLIC_CHANNEL_LINK = "@AAU_freshman2026"
SUPPORT_CONTACT = "@NextGen_helper"

# 📁 FILES
DATA_FILE_BANS = "data_banned_users.json"
DATA_FILE_CONTEXT = "data_contexts.json" # Stores info for Deep Links
DATA_FILE_VOTES = "data_votes.json"      # Stores Like/Dislike counts

# ==============================================================================
# 📝 STRINGS & LOCALIZATION
# ==============================================================================

class Strings:
    """
    Central repository for all text displayed by the bot.
    Uses HTML formatting.
    """
    WELCOME = (
        "🇪🇹 <b>AAU Teacher Review Bot (v9.0)</b>\n\n"
        "👋 <b>Welcome, Student!</b>\n"
        "This platform helps the AAU community by collecting anonymous feedback on instructors.\n\n"
        "🎁 <b>YOUR REWARD:</b>\n"
        "Approved reviews grant you access to the <b>AAU Review Archive</b> (A library of 100+ instructor reviews).\n\n"
        "🛡️ <b>Privacy First:</b> Your identity is never shared.\n\n"
        "👇 <b>Select an option to begin:</b>"
    )

    WELCOME_DEEP_LINK = (
        "🔄 <b>Add Additional Feedback</b>\n\n"
        "You are adding a review for:\n"
        "👨‍🏫 <b>{teacher}</b>\n"
        "📚 <i>{subject}</i>\n\n"
        "Please rate them to proceed:"
    )

    BTN_START = "✍️ Write a Review"
    BTN_MATERIALS = "📚 Get Materials"
    BTN_CANCEL = "❌ Cancel Operation"
    
    BTN_ADD_ANOTHER = "➕ Review Another Teacher"
    BTN_SUBMIT_ALL = "🚀 Submit All Reviews"

    PROMPT_STREAM = "🏫 <b>Select your Department / Stream:</b>"
    PROMPT_YEAR = "📅 <b>Select your Academic Year:</b>"
    PROMPT_SUBJECT = "📚 <b>Select the Course / Subject:</b>\n<i>(Scroll down if needed)</i>"
    
    PROMPT_TEACHER = (
        "👤 <b>What is the Instructor's Name?</b>\n\n"
        "⚠️ <i>Please type the Full Name (e.g., 'Dr. Abebe Kebede', not just 'Abebe').</i>"
    )

    PROMPT_RATING = "⭐ <b>How would you rate this instructor?</b>"

    PROMPT_CONTENT = (
        "📝 <b>Write your detailed feedback:</b>\n\n"
        "Please be constructive. Mention:\n"
        "👉 <b>Teaching Style?</b>\n"
        "👉 <b>Exam Difficulty?</b>\n"
        "👉 <b>Grading Fairness?</b>\n"
        "👉 <b>Attendance Policy?</b>\n"
        "👉 <b>Does he/she give quizzes without notice?</b>\n"
        "👉 <b>Does he/she add marks for participation?</b>\n\n"
        "<i>(Type your message below)</i>"
    )

    ERR_BANNED = f"🚫 <b>Access Denied.</b>\nYou have been banned from using this bot.\nContact {SUPPORT_CONTACT} for appeals."
    ERR_INVALID_SELECTION = "⚠️ Invalid selection. Please use the buttons provided."
    ERR_SHORT_NAME = "⚠️ Name is too short. Please enter the full name."
    ERR_SHORT_REVIEW = "⚠️ <b>Review Too Short!</b>\nPlease provide details about quizzes, exams, and grading."
    ERR_NO_DATA = "⚠️ Session expired or empty. Please type /start again."
    ERR_GENERIC = f"⚠️ <b>System Error.</b>\nPlease report this to {SUPPORT_CONTACT}."

    SUCCESS_BATCH_ADDED = "✅ <b>Review Saved to Drafts!</b>"
    SUCCESS_FINAL = (
        "🚀 <b>All Reviews Submitted!</b>\n\n"
        "Thank you for contributing to the community.\n"
        "🔔 You will receive a notification here once the Admin approves your post."
    )

    MSG_MATERIALS = f"📂 <b>Access Study Materials</b>\n\nJoin here: {PUBLIC_CHANNEL_LINK}"

# ==============================================================================
# 📊 ACADEMIC DATABASE
# ==============================================================================

ACADEMIC_DB = {
    # --- FRESHMAN PROGRAMS ---
    "🔬 Freshman Natural Science": {
        "Year 1 (Freshman)": [
            "Logic & Critical Thinking", "General Psychology", "Geography of Ethiopia", "Communicative English I",
            "Freshman Mathematics", "General Physics", "Emerging Technology", "Social Anthropology", 
            "History of Ethiopia", "Civics & Moral Education", "Global Trends", "Entrepreneurship", 
            "Economics", "Communicative English II", "Applied Mathematics I", "Computer Programming (Python)",
            "Physical Fitness"
        ]
    },
    "🌍 Freshman Social Science": {
        "Year 1 (Freshman)": [
            "Logic & Critical Thinking", "General Psychology", "Civics & Moral Education", "Global Trends",
            "Entrepreneurship", "Economics", "Social Anthropology", "Geography of Ethiopia", 
            "Communicative English I", "Emerging Technology", "Mathematics for Social Science", 
            "Communicative English II", "History of Ethiopia", "Physical Fitness"
        ]
    },

    # --- ENGINEERING ---
    "⚙️ Pre-Engineering & Engineering": {
        "Year 1 (Pre-Engineering Common)": [
            "Applied Math I", "Applied Math II", "Engineering Mechanics I (Statics)", 
            "Engineering Mechanics II (Dynamics)", "Engineering Drawing", "Workshop Practice", 
            "Introduction to Computing", "Communicative English", "Civics & Ethics", "Logic"
        ],
        "Year 2 (Mechanical Eng)": [
            "Applied Math III", "Strength of Materials", "Thermodynamics I", "Machine Drawing",
            "Materials Science", "Fluid Mechanics", "Thermodynamics II", "Manufacturing Processes",
            "Kinematics of Machinery", "Electrical Circuits"
        ],
        "Year 2 (Software Eng)": [
            "Applied Math III", "Physics for Engineers", "Programming Fundamentals", "Discrete Mathematics",
            "Digital Logic Design", "Probability & Statistics", "Data Structures & Algorithms",
            "Object-Oriented Programming", "Database Systems", "Computer Organization"
        ],
        "Year 2 (Electrical Eng)": [
            "Applied Math III", "Network Analysis", "Electronic Circuits I", "Digital Logic",
            "Electromagnetic Fields", "Signals and Systems", "Electrical Workshop", "Object Oriented Programming"
        ],
        "Year 2 (Civil Eng)": [
            "Theory of Structures I", "Surveying I", "Engineering Geology", "Construction Materials",
            "Strength of Materials", "Applied Math III", "Hydraulics I"
        ],
        "Year 3 (General)": [
            "Internship / Industrial Practice", "Research Methods", "Entrepreneurship for Engineers",
            "Operating Systems", "Computer Networks", "Software Engineering", "Machine Design",
            "Heat Transfer", "Control Systems", "Reinforced Concrete"
        ]
    },

    # --- MEDICINE & HEALTH ---
    "🩺 Medicine & Health Sciences": {
        "Year 1 (Pre-Medicine)": [
            "General Biology", "General Chemistry", "General Physics", "Introduction to Medicine",
            "Communicative English", "Medical Ethics", "Civics", "Information Technology"
        ],
        "Year 2 (Pre-Clinical)": [
            "Human Anatomy I", "Human Anatomy II", "Human Physiology I", "Human Physiology II",
            "Medical Biochemistry I", "Medical Biochemistry II", "Histology & Embryology", 
            "Public Health", "Microbiology"
        ],
        "Year 3 (Clinical Start)": [
            "Pathology I", "Pathology II", "Pharmacology I", "Pharmacology II",
            "Introduction to Clinical Medicine", "Immunology", "Parasitology", "Epidemiology"
        ],
        "Other Health (Nursing/Pharma)": [
            "Fundamentals of Nursing", "Pharmaceutics", "Medicinal Chemistry", 
            "Clinical Nursing", "Health Service Management"
        ]
    },

    # --- LAW & SOCIAL ---
    "⚖️ Law & Governance": {
        "Year 1": [
            "Introduction to Law", "Sociology of Law", "Legal History", "Constitutional Law I",
            "Logic", "English for Lawyers"
        ],
        "Year 2": [
            "Constitutional Law II", "Law of Contracts I", "Law of Contracts II", "Family Law",
            "Criminal Law I", "Criminal Law II", "Law of Persons"
        ],
        "Year 3": [
            "Law of Traders", "Business Organizations", "Administrative Law", "Property Law",
            "Law of Sales", "Human Rights Law", "Public International Law"
        ]
    }
}

EMOJI_MAP = {
    "Physics": "⚛️", "Math": "🧮", "Calculus": "∫", "Chemistry": "🧪", 
    "Biology": "🧬", "English": "🇬🇧", "Civics": "⚖️", "Logic": "🧠", 
    "Geography": "🌍", "Computer": "💻", "Programming": "⌨️", 
    "Psychology": "🧩", "Sociology": "👥", "Economics": "📉", 
    "History": "📜", "Anatomy": "🦴", "Accounting": "💰", 
    "Law": "⚖️", "Drawing": "📐", "Statics": "🏗️", "Dynamics": "🚀",
    "Software": "💾", "Network": "🌐", "Thermodynamics": "🔥"
}

# ==============================================================================
# 💾 PERSISTENT DATABASE SYSTEM (JSON)
# ==============================================================================

class JsonDatabase:
    """
    Handles saving and loading data to JSON files.
    Ensures data persists even if the bot restarts on Render.
    """
    def __init__(self):
        self.banned_users: Set[int] = set()
        self.context_cache: Dict[str, dict] = {} # {uuid: {teacher, subject, parent_msg_id}}
        self.vote_cache: Dict[str, dict] = {}    # {msg_id_str: {up: 0, down: 0}}
        
        # Load data on startup
        self.load_all()

    def load_all(self):
        # 1. Banned Users
        if os.path.exists(DATA_FILE_BANS):
            try:
                with open(DATA_FILE_BANS, 'r') as f:
                    self.banned_users = set(json.load(f))
            except Exception as e:
                logging.error(f"Error loading bans: {e}")

        # 2. Context Cache (Deep Links)
        if os.path.exists(DATA_FILE_CONTEXT):
            try:
                with open(DATA_FILE_CONTEXT, 'r') as f:
                    self.context_cache = json.load(f)
            except Exception as e:
                logging.error(f"Error loading contexts: {e}")

        # 3. Votes
        if os.path.exists(DATA_FILE_VOTES):
            try:
                with open(DATA_FILE_VOTES, 'r') as f:
                    self.vote_cache = json.load(f)
            except Exception as e:
                logging.error(f"Error loading votes: {e}")

    def save_bans(self):
        with open(DATA_FILE_BANS, 'w') as f:
            json.dump(list(self.banned_users), f)

    def save_contexts(self):
        with open(DATA_FILE_CONTEXT, 'w') as f:
            json.dump(self.context_cache, f)

    def save_votes(self):
        with open(DATA_FILE_VOTES, 'w') as f:
            json.dump(self.vote_cache, f)

    # API Methods
    def is_banned(self, user_id: int) -> bool:
        return user_id in self.banned_users

    def ban_user(self, user_id: int):
        self.banned_users.add(user_id)
        self.save_bans()

    def add_context(self, context_id: str, data: dict):
        self.context_cache[context_id] = data
        self.save_contexts()

    def get_context(self, context_id: str) -> Optional[dict]:
        return self.context_cache.get(context_id)

    def get_votes(self, msg_id: int) -> dict:
        mid = str(msg_id)
        if mid not in self.vote_cache:
            self.vote_cache[mid] = {'up': 0, 'down': 0}
        return self.vote_cache[mid]

    def add_vote(self, msg_id: int, vote_type: str):
        mid = str(msg_id)
        if mid not in self.vote_cache:
            self.vote_cache[mid] = {'up': 0, 'down': 0}
        
        if vote_type == 'up':
            self.vote_cache[mid]['up'] += 1
        elif vote_type == 'down':
            self.vote_cache[mid]['down'] += 1
        
        self.save_votes()

# Initialize Database
db = JsonDatabase()

# ==============================================================================
# 🧠 SESSION & STATE MANAGEMENT
# ==============================================================================

class ReviewDraft:
    """Represents a single review being written."""
    def __init__(self):
        self.id = str(uuid.uuid4())[:8]
        self.stream: str = ""
        self.year: str = ""
        self.subject: str = ""
        self.teacher: str = ""
        self.rating: int = 0
        self.content: str = ""
        self.is_additional: bool = False
        self.parent_msg_id: Optional[int] = None
        self.timestamp = datetime.now()

class UserSession:
    """Manages the user's batch of reviews."""
    def __init__(self, user_id):
        self.user_id = user_id
        self.current_draft: Optional[ReviewDraft] = None
        self.completed_reviews: List[ReviewDraft] = []
    
    def start_new_draft(self):
        self.current_draft = ReviewDraft()
    
    def save_draft(self):
        if self.current_draft:
            self.completed_reviews.append(self.current_draft)
            self.current_draft = None

# In-memory session store (Transient, cleared on restart is fine for sessions)
sessions: Dict[int, UserSession] = {}

def get_session(user_id: int) -> UserSession:
    if user_id not in sessions:
        sessions[user_id] = UserSession(user_id)
    return sessions[user_id]

def get_subject_emoji(subject_name: str) -> str:
    for k, v in EMOJI_MAP.items():
        if k.lower() in subject_name.lower():
            return v
    return "📚"

def build_keyboard(items: List[str], cols=1) -> ReplyKeyboardMarkup:
    menu = [items[i:i + cols] for i in range(0, len(items), cols)]
    return ReplyKeyboardMarkup(menu, resize_keyboard=True, one_time_keyboard=True)

# ==============================================================================
# 🚦 CONVERSATION STATES
# ==============================================================================

(
    SELECT_STREAM,
    SELECT_YEAR,
    SELECT_SUBJECT,
    INPUT_TEACHER,
    SELECT_RATING,
    INPUT_CONTENT,
    BATCH_DECISION
) = range(7)

# ==============================================================================
# 🚀 USER FLOW HANDLERS
# ==============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Entry point. Handles normal start AND deep linking (add_{uuid}).
    """
    user = update.effective_user
    
    # Check Ban
    if db.is_banned(user.id):
        await update.message.reply_text(Strings.ERR_BANNED, parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    # Reset Session
    sessions[user.id] = UserSession(user.id)
    session = get_session(user.id)

    # 🕵️ DEEP LINK CHECK
    # User clicked "Add Info" button in channel
    args = context.args
    if args and args[0].startswith("add_"):
        context_id = args[0].replace("add_", "")
        data = db.get_context(context_id)
        
        if data:
            # Pre-fill session with Context Data
            session.start_new_draft()
            session.current_draft.stream = data.get('stream', 'Linked Review')
            session.current_draft.year = data.get('year', 'Unknown')
            session.current_draft.subject = data.get('subject', 'Unknown')
            session.current_draft.teacher = data.get('teacher', 'Unknown')
            session.current_draft.parent_msg_id = data.get('parent_msg_id')
            session.current_draft.is_additional = True
            
            # Skip directly to Rating
            keyboard = [[
                InlineKeyboardButton("1 ⭐", callback_data="rate_1"),
                InlineKeyboardButton("2 ⭐", callback_data="rate_2"),
                InlineKeyboardButton("3 ⭐", callback_data="rate_3"),
                InlineKeyboardButton("4 ⭐", callback_data="rate_4"),
                InlineKeyboardButton("5 ⭐", callback_data="rate_5"),
            ]]
            
            msg = Strings.WELCOME_DEEP_LINK.format(
                teacher=html.escape(data['teacher']),
                subject=html.escape(data['subject'])
            )
            
            await update.message.reply_text(
                msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
            return SELECT_RATING
        else:
            await update.message.reply_text("⚠️ <b>Link Expired or Invalid.</b> Starting fresh.", parse_mode=ParseMode.HTML)

    # Normal Start Menu
    keyboard = [[Strings.BTN_START], [Strings.BTN_MATERIALS]]
    await update.message.reply_text(
        Strings.WELCOME,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

async def materials_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(Strings.MSG_MATERIALS, parse_mode=ParseMode.HTML)
    return ConversationHandler.END

async def start_review(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 1: Ask for Stream."""
    session = get_session(update.effective_user.id)
    session.start_new_draft()
    
    streams = list(ACADEMIC_DB.keys())
    markup = build_keyboard(streams + [Strings.BTN_CANCEL], cols=1)
    
    await update.message.reply_text(Strings.PROMPT_STREAM, reply_markup=markup, parse_mode=ParseMode.HTML)
    return SELECT_STREAM

async def select_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 2: Ask for Year."""
    text = update.message.text
    if text == Strings.BTN_CANCEL: return await cancel(update, context)
    
    if text not in ACADEMIC_DB:
        await update.message.reply_text(Strings.ERR_INVALID_SELECTION, parse_mode=ParseMode.HTML)
        return SELECT_STREAM

    session = get_session(update.effective_user.id)
    session.current_draft.stream = text
    
    # Load Years for this Stream
    years = list(ACADEMIC_DB[text].keys())
    markup = build_keyboard(years + [Strings.BTN_CANCEL], cols=1)
    
    await update.message.reply_text(Strings.PROMPT_YEAR, reply_markup=markup, parse_mode=ParseMode.HTML)
    return SELECT_YEAR

async def select_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 3: Ask for Subject."""
    text = update.message.text
    if text == Strings.BTN_CANCEL: return await cancel(update, context)
    
    session = get_session(update.effective_user.id)
    stream = session.current_draft.stream
    
    if text not in ACADEMIC_DB[stream]:
        await update.message.reply_text(Strings.ERR_INVALID_SELECTION, parse_mode=ParseMode.HTML)
        return SELECT_YEAR
        
    session.current_draft.year = text
    
    # Load Subjects
    subjects = ACADEMIC_DB[stream][text]
    # Arrange subjects nicely in 2 cols
    markup = build_keyboard(subjects + [Strings.BTN_CANCEL], cols=2)
    
    await update.message.reply_text(Strings.PROMPT_SUBJECT, reply_markup=markup, parse_mode=ParseMode.HTML)
    return SELECT_SUBJECT

async def select_subject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 4: Ask for Teacher."""
    text = update.message.text
    if text == Strings.BTN_CANCEL: return await cancel(update, context)
    
    session = get_session(update.effective_user.id)
    session.current_draft.subject = text
    
    await update.message.reply_text(
        Strings.PROMPT_TEACHER,
        reply_markup=ReplyKeyboardRemove(),
        parse_mode=ParseMode.HTML
    )
    return INPUT_TEACHER

async def input_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 5: Ask for Rating."""
    text = update.message.text
    if len(text) < 3:
        await update.message.reply_text(Strings.ERR_SHORT_NAME, parse_mode=ParseMode.HTML)
        return INPUT_TEACHER
        
    session = get_session(update.effective_user.id)
    session.current_draft.teacher = text
    
    # Inline Rating
    keyboard = [[
        InlineKeyboardButton("1 ⭐", callback_data="rate_1"),
        InlineKeyboardButton("2 ⭐", callback_data="rate_2"),
        InlineKeyboardButton("3 ⭐", callback_data="rate_3"),
        InlineKeyboardButton("4 ⭐", callback_data="rate_4"),
        InlineKeyboardButton("5 ⭐", callback_data="rate_5"),
    ]]
    
    safe_name = html.escape(text)
    await update.message.reply_text(
        f"👤 <b>{safe_name}</b>\n{Strings.PROMPT_RATING}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )
    return SELECT_RATING

async def rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 6: Ask for Written Content."""
    query = update.callback_query
    await query.answer()
    
    rating = int(query.data.split("_")[1])
    
    session = get_session(update.effective_user.id)
    session.current_draft.rating = rating
    
    await query.edit_message_text(
        f"⭐ <b>Rating Set: {rating}/5</b>\n\n{Strings.PROMPT_CONTENT}",
        parse_mode=ParseMode.HTML
    )
    return INPUT_CONTENT

async def input_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Step 7: Batch Decision."""
    text = update.message.text
    
    if len(text) < 10:
        await update.message.reply_text(Strings.ERR_SHORT_REVIEW, parse_mode=ParseMode.HTML)
        return INPUT_CONTENT
        
    session = get_session(update.effective_user.id)
    session.current_draft.content = text
    session.save_draft()
    
    # SPECIAL CASE: If this is an additional review (Deep Link), auto-submit
    if session.completed_reviews[-1].is_additional:
        return await submit_all_reviews(update, context)

    # Standard Flow: Ask to Add Another
    count = len(session.completed_reviews)
    msg = f"{Strings.SUCCESS_BATCH_ADDED}\n\n📊 Drafts Ready: <b>{count}</b>\n👇 <b>What next?</b>"
    
    keyboard = [[Strings.BTN_ADD_ANOTHER], [Strings.BTN_SUBMIT_ALL]]
    await update.message.reply_text(
        msg,
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    return BATCH_DECISION

async def batch_decision_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    choice = update.message.text
    
    if choice == Strings.BTN_SUBMIT_ALL:
        return await submit_all_reviews(update, context)
    
    elif choice == Strings.BTN_ADD_ANOTHER:
        # Loop back logic: Keep Stream, Restart Year selection
        session = get_session(update.effective_user.id)
        if not session.completed_reviews:
            return await start_review(update, context)
            
        last_stream = session.completed_reviews[-1].stream
        
        # New Draft
        session.start_new_draft()
        session.current_draft.stream = last_stream
        
        # Show Years
        years = list(ACADEMIC_DB[last_stream].keys())
        markup = build_keyboard(years + [Strings.BTN_CANCEL], cols=1)
        
        await update.message.reply_text(
            f"🔄 <b>Reviewing Another Teacher in:</b>\n🏫 {last_stream}\n\n{Strings.PROMPT_YEAR}",
            reply_markup=markup,
            parse_mode=ParseMode.HTML
        )
        return SELECT_YEAR
        
    else:
        await update.message.reply_text(Strings.ERR_INVALID_SELECTION, parse_mode=ParseMode.HTML)
        return BATCH_DECISION

async def submit_all_reviews(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends all drafts to Admin."""
    user = update.effective_user
    session = get_session(user.id)
    
    if not session.completed_reviews:
        await update.message.reply_text(Strings.ERR_NO_DATA, parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    
    await update.message.reply_text("⏳ <b>Transmitting to Admin...</b>", reply_markup=ReplyKeyboardRemove(), parse_mode=ParseMode.HTML)
    
    for review in session.completed_reviews:
        # Build Admin Message
        safe_name = html.escape(user.first_name)
        safe_teacher = html.escape(review.teacher)
        safe_subject = html.escape(review.subject)
        safe_content = html.escape(review.content)
        
        header = "🧵 <b>THREAD REPLY</b>" if review.is_additional else "📩 <b>NEW REVIEW</b>"
        parent_info = f"🔗 <b>Parent Msg ID:</b> {review.parent_msg_id}\n" if review.parent_msg_id else ""
        
        admin_text = (
            f"{header}\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"👤 <b>User:</b> {safe_name} (<code>{user.id}</code>)\n"
            f"🏫 <b>Stream:</b> {review.stream}\n"
            f"📅 <b>Year:</b> {review.year}\n"
            f"📚 <b>Subject:</b> {safe_subject}\n"
            f"👨‍🏫 <b>Teacher:</b> {safe_teacher}\n"
            f"⭐ <b>Rating:</b> {review.rating}/5\n"
            f"{parent_info}"
            f"🆔 <b>Ref ID:</b> <code>{review.id}</code>\n\n"
            f"📝 <b>Review Content:</b>\n{safe_content}"
        )
        
        # Buttons for Admin
        keyboard = [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"app_{user.id}_{review.id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"rej_{user.id}_{review.id}")
            ],
            [
                InlineKeyboardButton("🔨 BAN USER", callback_data=f"ban_{user.id}_{review.id}")
            ]
        ]
        
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=admin_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.error(f"Failed to send to admin: {e}")

    # Clear Session
    del sessions[user.id]
    
    # Success Menu
    menu = [[Strings.BTN_START], [Strings.BTN_MATERIALS]]
    await update.message.reply_text(
        Strings.SUCCESS_FINAL,
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in sessions: del sessions[user_id]
    
    menu = [[Strings.BTN_START], [Strings.BTN_MATERIALS]]
    await update.message.reply_text(
        Strings.BTN_CANCEL,
        reply_markup=ReplyKeyboardMarkup(menu, resize_keyboard=True),
        parse_mode=ParseMode.HTML
    )
    return ConversationHandler.END

# ==============================================================================
# 🛡️ ADMIN & CHANNEL CALLBACKS
# ==============================================================================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    action = data[0]
    target_uid = int(data[1])
    review_id = data[2]
    
    if action == "app":
        await approve_review(query, context, target_uid, review_id)
    elif action == "rej":
        await reject_review(query, context, target_uid)
    elif action == "ban":
        await ban_user(query, context, target_uid)

async def approve_review(query, context, user_id, review_id):
    """
    1. Parse Admin Msg.
    2. Generate Deep Link Context.
    3. Post to Channel (as New or Reply).
    4. Save Context for future threading.
    """
    try:
        # Extract Data from Admin Message
        original_text = query.message.text
        lines = original_text.split('\n')
        
        subject = "Unknown"
        teacher = "Unknown"
        rating = "5"
        stream = "General"
        year = "General"
        parent_msg_id = None
        
        for line in lines:
            if "Subject:" in line: subject = line.split(":", 1)[1].strip()
            if "Teacher:" in line: teacher = line.split(":", 1)[1].strip()
            if "Rating:" in line: rating = line.split(":", 1)[1].strip().split("/")[0]
            if "Stream:" in line: stream = line.split(":", 1)[1].strip()
            if "Year:" in line: year = line.split(":", 1)[1].strip()
            if "Parent Msg ID:" in line: 
                try: parent_msg_id = int(line.split(":", 1)[1].strip())
                except: pass
        
        feedback = "See post."
        if "Review Content:" in original_text:
            feedback = original_text.split("Review Content:", 1)[1].strip()

        # 1. GENERATE CONTEXT FOR DEEP LINK
        context_uuid = str(uuid.uuid4())[:8]
        
        # 2. PREPARE CHANNEL POST
        emoji = get_subject_emoji(subject)
        safe_subject = html.escape(subject)
        safe_teacher = html.escape(teacher)
        safe_feedback = html.escape(feedback)
        
        if parent_msg_id:
            # Child Post
            header = "📝 <b>ADDITIONAL FEEDBACK</b>\n<i>(Reply to thread)</i>"
        else:
            # Parent Post
            header = "📢 <b>NEW TEACHER REVIEW</b>"

        post = (
            f"{header}\n\n"
            f"📚 <b>Subject:</b> {safe_subject} {emoji}\n"
            f"👨‍🏫 <b>Teacher:</b> {safe_teacher}\n"
            f"⭐ <b>Rating:</b> {rating}/5\n\n"
            f"💬 <b>Feedback:</b>\n"
            f"<i>{safe_feedback}</i>\n\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"Join: {PUBLIC_CHANNEL_LINK}"
        )
        
        # 3. BUTTONS (VOTING + DEEP LINK)
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=add_{context_uuid}"
        
        keyboard = [
            [
                InlineKeyboardButton("👍 0", callback_data=f"vote_up_{review_id}"),
                InlineKeyboardButton("👎 0", callback_data=f"vote_down_{review_id}")
            ],
            [
                InlineKeyboardButton("➕ Add More About This Teacher", url=deep_link)
            ]
        ]
        
        # 4. SEND TO CHANNEL
        sent_msg = await context.bot.send_message(
            chat_id=CHANNEL_ID,
            text=post,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
            reply_to_message_id=parent_msg_id # Threading Magic
        )
        
        # 5. SAVE CONTEXT
        # If this was a new post, the 'parent' for the next review is this message.
        # If this was a reply, the 'parent' remains the original thread starter.
        next_parent = parent_msg_id if parent_msg_id else sent_msg.message_id
        
        db.add_context(context_uuid, {
            'stream': stream,
            'year': year,
            'subject': subject,
            'teacher': teacher,
            'parent_msg_id': next_parent
        })
        
        # 6. NOTIFY USER
        invite_link = PUBLIC_CHANNEL_LINK
        try:
            link = await context.bot.create_chat_invite_link(CHANNEL_ID, member_limit=1, name=f"Rwd-{user_id}")
            invite_link = link.invite_link
        except: pass

        await context.bot.send_message(
            user_id,
            f"✅ <b>Review Approved!</b>\n\n🔑 <b>Access Link:</b>\n{invite_link}",
            parse_mode=ParseMode.HTML
        )
        
        await query.edit_message_text(f"{original_text}\n\n✅ <b>POSTED</b>", parse_mode=ParseMode.HTML)

    except Exception as e:
        logging.error(f"Approval Error: {e}")
        await query.message.reply_text(f"⚠️ Error: {e}")

async def reject_review(query, context, user_id):
    await context.bot.send_message(
        user_id,
        "❌ <b>Review Declined.</b>\nPlease provide more constructive and detailed feedback.",
        parse_mode=ParseMode.HTML
    )
    await query.edit_message_text(f"{query.message.text}\n\n❌ <b>REJECTED</b>", parse_mode=ParseMode.HTML)

async def ban_user(query, context, user_id):
    if user_id == ADMIN_ID: return
    db.ban_user(user_id)
    await query.edit_message_text(f"{query.message.text}\n\n⛔ <b>BANNED</b>", parse_mode=ParseMode.HTML)

# ==============================================================================
# 🗳️ CHANNEL VOTING
# ==============================================================================

async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    msg_id = query.message.message_id
    data = query.data.split("_")
    vote_type = data[1] # up or down
    
    # Update DB
    db.add_vote(msg_id, vote_type)
    votes = db.get_votes(msg_id)
    
    # Update Buttons
    old_markup = query.message.reply_markup.inline_keyboard
    deep_link_url = old_markup[1][0].url # Preserve the Deep Link
    
    new_kb = [
        [
            InlineKeyboardButton(f"👍 {votes['up']}", callback_data=f"vote_up_0"),
            InlineKeyboardButton(f"👎 {votes['down']}", callback_data=f"vote_down_0")
        ],
        [
            InlineKeyboardButton("➕ Add More About This Teacher", url=deep_link_url)
        ]
    ]
    
    try:
        await query.edit_message_reply_markup(InlineKeyboardMarkup(new_kb))
        await query.answer("Vote recorded!")
    except BadRequest:
        await query.answer()

# ==============================================================================
# 🌐 FLASK KEEP-ALIVE SERVER (FOR RENDER)
# ==============================================================================

app = Flask('')

@app.route('/')
def home():
    return "I am alive and running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = threading.Thread(target=run_flask)
    t.daemon = True
    t.start()

# ==============================================================================
# 🔌 MAIN APPLICATION
# ==============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(msg="Exception handling update:", exc_info=context.error)

def main():
    print("-------------------------------------------------")
    print("🚀 AAU TEACHER REVIEW BOT V9.0 IS STARTING...")
    print(f"📍 Location: Addis Ababa, Ethiopia")
    print(f"💾 Database: JSON Persistence Enabled")
    print("-------------------------------------------------")

    # 1. Start Web Server
    keep_alive()

    # 2. Build Bot
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # 3. Conversation Handler
    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(f"^{Strings.BTN_START}$"), start_review),
            CommandHandler("start", start) # Handles both menu and deep links
        ],
        states={
            SELECT_STREAM: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_stream)],
            SELECT_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_year)],
            SELECT_SUBJECT: [MessageHandler(filters.TEXT & ~filters.COMMAND, select_subject)],
            INPUT_TEACHER: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_teacher)],
            SELECT_RATING: [CallbackQueryHandler(rating_callback, pattern="^rate_")],
            INPUT_CONTENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, input_content)],
            BATCH_DECISION: [MessageHandler(filters.TEXT & ~filters.COMMAND, batch_decision_handler)]
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(f"^{Strings.BTN_CANCEL}$"), cancel)
        ],
        per_user=True
    )

    # 4. Register Handlers
    application.add_handler(conv_handler)
    application.add_handler(MessageHandler(filters.Regex(f"^{Strings.BTN_MATERIALS}$"), materials_handler))
    
    # Admin & Voting Callbacks
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^(app|rej|ban)_"))
    application.add_handler(CallbackQueryHandler(vote_callback, pattern="^vote_"))
    
    # Errors
    application.add_error_handler(error_handler)

    # 5. Run
    application.run_polling()

if __name__ == "__main__":
    main()