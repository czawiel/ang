import io
import re
import os
import base64
import time
import random
import sqlite3
import traceback
from datetime import date, timedelta
from collections import Counter
import requests
from bs4 import BeautifulSoup
from youtube_transcript_api import YouTubeTranscriptApi
import pandas as pd
import streamlit as st
from gtts import gTTS

# --- KONFIGURACJA TURSO HTTP ---
DEFAULT_TURSO_URL = "https://ang-cesar87.aws-eu-north-1.turso.io"
DEFAULT_TURSO_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTE3NTU4MTksImlkIjoiMDFhMTJjZjctZjEwMS03NDliLWFlYjMtOWJlZTZiODEzOWYzIiwia2lkIjoibVU3UXpkTWNJemp0cy00b3ZpUkZNWmx5QUh5ZVU1RmRPVXdhdHhyTDI3RSIsInJpZCI6IjFhYzAxMWI0LTE2OTUtNDdlMC04NGYyLWExYmZjOGZhZjE2YSJ9.HFPs163xNdb-a5FVh6uRIjhLl36Z_4cFcLUJRwewL7Bqa08e66iQ8cr9MjKXBn7A2lFXmpPJgUK_YNByL9KCCw"
DB_FILE = "flashcards.db"

BOX_INTERVALS = {
    1: 1,    # Pudełko 1: 1 dzień
    2: 3,    # Pudełko 2: 3 dni
    3: 7,    # Pudełko 3: 7 dni
    4: 14,   # Pudełko 4: 14 dni
    5: 30,   # Pudełko 5: 30 dni
}

DEFAULT_CATEGORIES = ["Ogólne", "Biznes", "Jedzenie", "Podróże", "Poziom B2", "Z Internetu", "YouTube", "Top 1000"]

STOP_WORDS = {
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i", "it", "for", "not", "on", 
    "with", "he", "as", "you", "do", "at", "this", "but", "his", "by", "from", "they", "we", 
    "say", "her", "she", "or", "an", "will", "my", "one", "all", "would", "there", "their", 
    "what", "so", "up", "out", "if", "about", "who", "get", "which", "go", "me", "when", 
    "make", "can", "like", "time", "no", "just", "him", "know", "take", "people", "into", 
    "year", "your", "good", "some", "could", "them", "see", "other", "than", "then", "now", 
    "look", "only", "come", "its", "over", "think", "also", "back", "after", "use", "two", 
    "how", "our", "work", "first", "well", "way", "even", "new", "want", "because", "any", 
    "these", "give", "day", "most", "us", "are", "was", "were", "been", "has", "had", "is",
    "really", "very", "much", "more", "said", "thing", "things", "going", "many", "where",
    "yeah", "okay", "gonna", "wanna", "right", "sure", "well", "actually", "something"
}


# --- BEZPOŚREDNIE POŁĄCZENIE Z TURSO PRZEZ HTTPS (OMINIĘCIE WEBSOCKET) ---
def get_turso_credentials():
    try:
        url = st.secrets.get("turso", {}).get("url", DEFAULT_TURSO_URL)
        token = st.secrets.get("turso", {}).get("auth_token", DEFAULT_TURSO_TOKEN)
    except Exception:
        url = DEFAULT_TURSO_URL
        token = DEFAULT_TURSO_TOKEN

    if url.startswith("libsql://"):
        url = url.replace("libsql://", "https://")
    return url.rstrip("/"), token


def query_turso_http(sql: str, params: list = None):
    url, token = get_turso_credentials()
    if not url or not token:
        return None, "Brak danych dostępowych do bazy Turso"

    args = []
    for p in (params or []):
        if p is None:
            args.append({"type": "null"})
        elif isinstance(p, int):
            args.append({"type": "integer", "value": str(p)})
        elif isinstance(p, float):
            args.append({"type": "float", "value": p})
        else:
            args.append({"type": "text", "value": str(p)})

    payload = {
        "requests": [
            {
                "type": "execute",
                "stmt": {
                    "sql": sql,
                    "args": args
                }
            },
            {"type": "close"}
        ]
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    try:
        res = requests.post(f"{url}/v2/pipeline", json=payload, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            resp_data = data["results"][0]["response"]["result"]
            rows = []
            for row in resp_data.get("rows", []):
                parsed_row = [col.get("value", None) for col in row]
                rows.append(tuple(parsed_row))
            return rows, None
        return None, f"Błąd Turso HTTP {res.status_code}: {res.text}"
    except Exception as e:
        return None, str(e)


def execute_db_query(sql: str, params: list = None):
    params = params or []
    rows, err = query_turso_http(sql, params)
    if err is None and rows is not None:
        return rows

    # Fallback na lokalne sqlite w razie braku połączenia
    with sqlite3.connect(DB_FILE) as conn:
        cur = conn.cursor()
        cur.execute(sql, params)
        return cur.fetchall()


def execute_db_commit(sql: str, params: list = None):
    params = params or []
    _, err = query_turso_http(sql, params)
    if err is None:
        return True, "Zapisano"

    # Fallback na lokalne sqlite
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
            return True, "Zapisano"
    except Exception as e:
        return False, str(e)


# --- MECHANIZM TŁUMACZENIA ---
def translate_single_word(word: str, src="en", dest="pl") -> str:
    clean_word = word.strip().lower()

    try:
        url = "https://api.mymemory.translated.net/get"
        params = {"q": clean_word, "langpair": f"{src}|{dest}"}
        res = requests.get(url, params=params, timeout=5)
        if res.status_code == 200:
            data = res.json()
            translated = data.get("responseData", {}).get("translatedText", "").strip().lower()
            if translated and translated != clean_word and "mymemory" not in translated:
                return translated
    except Exception:
        pass

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        url = "https://translate.googleapis.com/translate_a/single"
        params = {"client": "gtx", "sl": src, "tl": dest, "dt": "t", "q": clean_word}
        res = requests.get(url, params=params, headers=headers, timeout=5)
        if res.status_code == 200:
            data = res.json()
            if data and data[0] and data[0][0] and data[0][0][0]:
                translated = data[0][0][0].strip().lower()
                if translated and translated != clean_word:
                    return translated
    except Exception:
        pass

    return clean_word


def translate_words_batch(words_list: list, src="en", dest="pl") -> dict:
    translations = {}
    for w in words_list:
        translations[w] = translate_single_word(w, src=src, dest=dest)
        time.sleep(0.08)
    return translations


# --- BAZA DANYCH ---
def init_db():
    today_str = date.today().isoformat()
    execute_db_commit(
        """
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word TEXT NOT NULL UNIQUE,
            translation TEXT NOT NULL,
            example_sentence TEXT,
            category TEXT DEFAULT 'Ogólne',
            level INTEGER DEFAULT 1,
            interval INTEGER DEFAULT 1,
            next_review_date TEXT
        )
        """
    )
    execute_db_commit(
        """
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER,
            review_date TEXT NOT NULL,
            is_correct INTEGER NOT NULL,
            FOREIGN KEY (word_id) REFERENCES words (id)
        )
        """
    )

    count_rows = execute_db_query("SELECT COUNT(*) FROM words")
    if count_rows and int(count_rows[0][0]) == 0:
        default_words = [
            ("apple", "jabłko", "I eat a red apple every morning.", "Jedzenie", 1, 1, today_str),
            ("negotiation", "negocjacje", "The contract negotiation took three hours.", "Biznes", 1, 1, today_str),
            ("boarding pass", "karta pokładowa", "Please show your boarding pass at gate 4.", "Podróże", 1, 1, today_str),
            ("inevitable", "nieunikniony", "Change is inevitable in modern technology.", "Poziom B2", 1, 1, today_str),
            ("house", "dom", "They bought a new house near the forest.", "Ogólne", 1, 1, today_str),
        ]
        for item in default_words:
            execute_db_commit(
                """
                INSERT OR IGNORE INTO words 
                (word, translation, example_sentence, category, level, interval, next_review_date) 
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                list(item)
            )


def get_categories():
    db_cats = [
        r[0] for r in execute_db_query(
            "SELECT DISTINCT category FROM words WHERE category IS NOT NULL AND category != ''"
        )
    ]
    return sorted(list(set(DEFAULT_CATEGORIES + db_cats)))


def get_words(category_filter="Wszystkie", due_only=False):
    today_str = date.today().isoformat()
    query = """
        SELECT id, word, translation, example_sentence, category, level, interval, next_review_date 
        FROM words 
        WHERE word IS NOT NULL AND word != ''
    """
    params = []

    if category_filter != "Wszystkie":
        query += " AND category = ?"
        params.append(category_filter)

    if due_only:
        query += " AND (next_review_date <= ? OR next_review_date IS NULL)"
        params.append(today_str)

    query += " ORDER BY level ASC, next_review_date ASC"
    rows = execute_db_query(query, params)
    return [
        {
            "id": int(r[0]),
            "word": str(r[1]),
            "translation": str(r[2]),
            "example_sentence": str(r[3] or ""),
            "category": str(r[4] or "Ogólne"),
            "level": int(r[5] or 1),
            "interval": int(r[6] or 1),
            "next_review_date": str(r[7] or today_str),
        }
        for r in rows
    ]


def record_review(word_id, is_correct):
    today_str = date.today().isoformat()
    execute_db_commit(
        "INSERT INTO reviews (word_id, review_date, is_correct) VALUES (?, ?, ?)",
        [word_id, today_str, 1 if is_correct else 0],
    )
    rows = execute_db_query("SELECT level FROM words WHERE id = ?", [word_id])
    current_level = int(rows[0][0]) if rows else 1

    new_level = min(5, current_level + 1) if is_correct else 1
    new_interval = BOX_INTERVALS[new_level]
    next_date = (date.today() + timedelta(days=new_interval)).isoformat()

    execute_db_commit(
        "UPDATE words SET level = ?, interval = ?, next_review_date = ? WHERE id = ?",
        [new_level, new_interval, next_date, word_id],
    )


def get_analytics():
    rows = execute_db_query("SELECT review_date, is_correct FROM reviews")
    if not rows:
        return {"streak": 0, "weekly_total": 0, "accuracy": 0, "chart_df": None}

    df_reviews = pd.DataFrame(rows, columns=["review_date", "is_correct"])
    df_reviews["is_correct"] = pd.to_numeric(df_reviews["is_correct"])

    unique_dates = set(df_reviews["review_date"].unique())
    today = date.today()
    streak = 0
    check_date = today

    if check_date.isoformat() not in unique_dates:
        check_date = today - timedelta(days=1)

    while check_date.isoformat() in unique_dates:
        streak += 1
        check_date -= timedelta(days=1)

    start_7d = (today - timedelta(days=6)).isoformat()
    df_7d = df_reviews[df_reviews["review_date"] >= start_7d]
    weekly_total = len(df_7d)
    accuracy = (df_7d["is_correct"].mean() * 100) if weekly_total > 0 else 0.0

    last_7_days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    chart_data = []

    for d in last_7_days:
        subset = df_reviews[df_reviews["review_date"] == d]
        correct = int((subset["is_correct"] == 1).sum())
        wrong = int((subset["is_correct"] == 0).sum())
        chart_data.append({"Data": d[5:], "Poprawne": correct, "Błędne": wrong})

    chart_df = pd.DataFrame(chart_data).set_index("Data")

    return {
        "streak": streak,
        "weekly_total": weekly_total,
        "accuracy": round(accuracy, 1),
        "chart_df": chart_df,
    }


def insert_word(word, translation, example="", category="Ogólne"):
    today_str = date.today().isoformat()
    category = category.strip() if category.strip() else "Ogólne"
    ok, msg = execute_db_commit(
        """
        INSERT INTO words 
        (word, translation, example_sentence, category, level, interval, next_review_date) 
        VALUES (?, ?, ?, ?, 1, 1, ?)
        """,
        [word.strip(), translation.strip(), example.strip(), category, today_str]
    )
    if ok:
        return True, "Dodano słówko!"
    if "UNIQUE" in msg.upper() or "constraint" in msg.lower():
        return False, "Słówko już istnieje w bazie!"
    return False, f"Błąd bazy: {msg}"


def delete_word(word_id):
    execute_db_commit("DELETE FROM words WHERE id = ?", [word_id])
    execute_db_commit("DELETE FROM reviews WHERE word_id = ?", [word_id])


# --- POBIERANIE ZE STRON WWW ---
def fetch_words_from_article_url(url, max_words=15, min_len=5):
    clean_url = url.strip()
    full_text = ""

    if "wikipedia.org/wiki/" in clean_url:
        title = clean_url.split("/wiki/")[-1].split("#")[0].split("?")[0]
        headers = {"User-Agent": "Mozilla/5.0"}
        api_url = f"https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1&titles={title}&format=json"
        try:
            r = requests.get(api_url, headers=headers, timeout=10)
            if r.status_code == 200:
                pages = r.json().get("query", {}).get("pages", {})
                for _, page in pages.items():
                    full_text = page.get("extract", "")
                    if full_text:
                        break
        except Exception:
            full_text = ""

    if not full_text:
        headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
        resp = requests.get(clean_url, headers=headers, timeout=12)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "svg", "form"]):
            tag.decompose()

        content_elements = soup.find_all(["p", "td", "th", "li", "dd", "h1", "h2", "h3", "span"])
        collected_chunks = [el.get_text(separator=" ", strip=True) for el in content_elements]
        meaningful_chunks = [c for c in collected_chunks if len(c) >= 15]

        if meaningful_chunks:
            full_text = " ".join(meaningful_chunks)
        elif soup.body:
            full_text = soup.body.get_text(separator=" ", strip=True)
        else:
            full_text = soup.get_text(separator=" ", strip=True)

    if not full_text.strip():
        raise ValueError("Serwer zwrócił pusty tekst lub nie udało się odczytać treści artykułu.")

    sentences = re.split(r'(?<=[.!?])\s+', full_text)
    raw_words = re.findall(r'\b[a-zA-Z]{' + str(min_len) + r',}\b', full_text.lower())

    candidate_words = []
    seen = set()
    for w in raw_words:
        if w not in STOP_WORDS and w not in seen:
            seen.add(w)
            candidate_words.append(w)
        if len(candidate_words) >= max_words:
            break

    if not candidate_words:
        raise ValueError(f"Nie znaleziono słówek spełniających kryteria długości (min. {min_len} liter).")

    translations_map = translate_words_batch(candidate_words, src="en", dest="pl")
    results = []

    for word in candidate_words:
        example = ""
        for s in sentences:
            clean_s = s.strip().replace("\n", " ")
            if re.search(r'\b' + re.escape(word) + r'\b', clean_s, re.IGNORECASE) and 30 <= len(clean_s) <= 250:
                example = clean_s
                break

        results.append({
            "word": word,
            "translation": translations_map.get(word, word),
            "example": example
        })

    return results


# --- POBIERANIE Z YOUTUBE ---
def extract_youtube_video_id(url: str) -> str:
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/|v\/|shorts\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, url.strip())
        if match:
            return match.group(1)
    return None


def fetch_transcript_fallback(video_id: str):
    instances = [
        f"https://api.invidious.io/api/v1/captions/{video_id}",
        f"https://pipedapi.kavin.rocks/streams/{video_id}",
    ]
    for inst in instances:
        try:
            r = requests.get(inst, timeout=6)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, list):
                    for cap in data:
                        if cap.get("language_code", "").startswith("en"):
                            cap_url = cap.get("url")
                            if cap_url:
                                sub_res = requests.get(cap_url, timeout=5)
                                if sub_res.status_code == 200:
                                    lines = [
                                        re.sub(r'<[^>]+>', '', line).strip()
                                        for line in sub_res.text.splitlines()
                                        if line.strip() and not line.startswith("WEBVTT")
                                    ]
                                    return [{"text": l} for l in lines if len(l) > 2]
        except Exception:
            continue
    return None


def fetch_and_translate_youtube_transcript(video_url: str, max_words=15, min_len=5):
    video_id = extract_youtube_video_id(video_url)
    if not video_id:
        raise ValueError("Niepoprawny link do filmu YouTube.")

    raw_transcript = None
    last_err = None

    try:
        ytt_api = YouTubeTranscriptApi()
        raw_transcript = ytt_api.fetch(video_id, languages=['en', 'en-US', 'en-GB'])
    except Exception as e:
        last_err = e

    if raw_transcript is None:
        try:
            get_fn = getattr(YouTubeTranscriptApi, "get_transcript", None)
            if callable(get_fn):
                raw_transcript = get_fn(video_id, languages=['en', 'en-US', 'en-GB'])
        except Exception as e:
            last_err = e

    if raw_transcript is None:
        raw_transcript = fetch_transcript_fallback(video_id)

    if raw_transcript is None:
        raise RuntimeError(
            f"YouTube zablokował zapytanie z serwera chmurowego ({last_err}). "
            "Skorzystaj z pola poniżej i wklej tekst transkrypcji ręcznie!"
        )

    text_segments = []
    for item in raw_transcript:
        txt = item.get("text", "") if isinstance(item, dict) else getattr(item, "text", str(item))
        txt = re.sub(r'\[.*?\]', '', txt)
        txt = txt.replace('\n', ' ').strip()
        if txt:
            text_segments.append(txt)

    return process_text_into_vocabulary(" ".join(text_segments), text_segments, max_words, min_len)


def process_text_into_vocabulary(full_text: str, segments: list, max_words: int, min_len: int):
    tokens = re.findall(r'\b[a-zA-Z]{' + str(min_len) + r',}\b', full_text.lower())
    filtered_tokens = [w for w in tokens if w not in STOP_WORDS]
    word_counts = Counter(filtered_tokens).most_common(max_words * 2)

    candidate_words = []
    for w, _ in word_counts:
        candidate_words.append(w)
        if len(candidate_words) >= max_words:
            break

    if not candidate_words:
        return []

    translations_map = translate_words_batch(candidate_words, src="en", dest="pl")
    results = []

    for word in candidate_words:
        example_sentence = ""
        for seg in segments:
            if re.search(r'\b' + re.escape(word) + r'\b', seg, re.IGNORECASE):
                example_sentence = seg
                break

        results.append({
            "word": word,
            "translation": translations_map.get(word, word),
            "example": example_sentence
        })

    return results


@st.cache_data(show_spinner=False)
def speak_text(text, lang="en"):
    tts = gTTS(text=text, lang=lang)
    fp = io.BytesIO()
    tts.write_to_fp(fp)
    fp.seek(0)
    return fp.getvalue()


# --- INICJALIZACJA STRONY I STYLÓW CSS ---
st.set_page_config(page_title="Fiszki & Narzędzia Nauki", page_icon="🎓", layout="centered")

st.markdown(
    """
    <style>
    /* Główne poprawki kontenera */
    .block-container {
        padding-top: 2rem;
        padding-bottom: 3rem;
        max-width: 860px;
    }
    
    /* Stylizacja zakładek (Tabs) */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        background-color: rgba(128, 128, 128, 0.08);
        padding: 6px;
        border-radius: 12px;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        padding: 8px 14px;
        font-weight: 600;
        border: none !important;
    }
    .stTabs [aria-selected="true"] {
        background-color: rgba(79, 70, 229, 0.15) !important;
        color: #4f46e5 !important;
    }

    /* Karta Fiszki */
    .flashcard-box {
        background: linear-gradient(145deg, rgba(255,255,255,0.05), rgba(128,128,128,0.04));
        border: 1px solid rgba(128, 128, 128, 0.2);
        border-radius: 18px;
        padding: 28px 24px;
        margin-top: 10px;
        margin-bottom: 16px;
        box-shadow: 0 8px 24px rgba(0, 0, 0, 0.06);
        text-align: center;
    }
    .flashcard-badges {
        display: flex;
        justify-content: center;
        gap: 10px;
        margin-bottom: 18px;
        flex-wrap: wrap;
    }
    .badge-pill {
        background: rgba(79, 70, 229, 0.12);
        color: #6366f1;
        padding: 4px 14px;
        border-radius: 999px;
        font-size: 0.82rem;
        font-weight: 600;
        border: 1px solid rgba(99, 102, 241, 0.25);
    }
    .flashcard-word {
        font-size: 2.3rem;
        font-weight: 800;
        margin: 10px 0;
        letter-spacing: -0.5px;
    }
    .flashcard-translation {
        font-size: 2rem;
        font-weight: 700;
        color: #10b981;
        margin: 10px 0;
    }
    .flashcard-context {
        background: rgba(128, 128, 128, 0.07);
        border-left: 4px solid #6366f1;
        padding: 12px 16px;
        border-radius: 6px 12px 12px 6px;
        font-style: italic;
        margin: 18px auto 8px auto;
        max-width: 90%;
        font-size: 0.98rem;
        opacity: 0.9;
    }

    /* Zaokrąglone przyciski i metryki */
    .stButton > button {
        border-radius: 10px;
        font-weight: 600;
        transition: all 0.15s ease-in-out;
    }
    div[data-testid="stMetric"] {
        background: rgba(128, 128, 128, 0.05);
        border: 1px solid rgba(128, 128, 128, 0.15);
        border-radius: 14px;
        padding: 14px 18px;
    }
    </style>
    """,
    unsafe_allow_html=True
)

if "db_initialized" not in st.session_state:
    init_db()
    st.session_state.db_initialized = True

all_categories = get_categories()

if "selected_category" not in st.session_state:
    st.session_state.selected_category = "Wszystkie"
if "show_translation" not in st.session_state:
    st.session_state.show_translation = False
if "current_card" not in st.session_state:
    st.session_state.current_card = None


def pick_next_card():
    cat = st.session_state.selected_category
    due = get_words(category_filter=cat, due_only=True)
    all_w = get_words(category_filter=cat, due_only=False)
    if due:
        st.session_state.current_card = random.choice(due)
    elif all_w:
        st.session_state.current_card = random.choice(all_w)
    else:
        st.session_state.current_card = None
    st.session_state.show_translation = False


# --- PASEK BOCZNY ---
with st.sidebar:
    logo_path = "logo3.svg"
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            svg_data = base64.b64encode(f.read()).decode("utf-8")
        st.markdown(
            f"""
            <div style="text-align: center; margin-bottom: 16px;">
                <a href="https://fabryka.tech" target="_blank" rel="noopener noreferrer">
                    <img src="data:image/svg+xml;base64,{svg_data}" alt="Logo" style="max-width: 100%; height: auto; max-height: 65px;"/>
                </a>
            </div>
            """,
            unsafe_allow_html=True
        )
    else:
        st.markdown(
            """
            <div style="text-align: center; margin-bottom: 16px;">
                <a href="https://fabryka.tech" target="_blank" rel="noopener noreferrer" style="text-decoration: none; font-size: 22px; font-weight: 800; color: inherit;">
                    🏭 Fabryka.tech
                </a>
            </div>
            """,
            unsafe_allow_html=True
        )

    st.caption("☁️ Baza danych: **Turso (Direct HTTPS)**")
    st.divider()

    st.subheader("🎯 Kategoria nauki")
    filter_opts = ["Wszystkie"] + all_categories
    selected = st.selectbox("Wybierz temat do nauki:", options=filter_opts)

    cat_all_words = get_words(category_filter=selected, due_only=False)
    cat_due_words = get_words(category_filter=selected, due_only=True)
    total_count = len(cat_all_words)
    due_count = len(cat_due_words)

    col_s1, col_s2 = st.columns(2)
    col_s1.metric("📚 Wszystkie", total_count)
    col_s2.metric("🔥 Na dziś", due_count)

    if selected != st.session_state.selected_category:
        st.session_state.selected_category = selected
        pick_next_card()
        st.rerun()

    st.divider()
    st.subheader("➕ Dodaj pojedyncze słówko")
    
    c_choice = st.selectbox("Kategoria:", options=all_categories + ["+ Inna..."])
    final_c = st.text_input("Wpisz nową kategorię:") if c_choice == "+ Inna..." else c_choice

    with st.form("add_word_form", clear_on_submit=True):
        nw = st.text_input("Słówko (angielski):")
        nt = st.text_input("Tłumaczenie (polski):")
        ne = st.text_input("Zdanie przykładowe:")
        submitted = st.form_submit_button("💾 Zapisz w bazie", use_container_width=True, type="primary")
        if submitted:
            if nw and nt and final_c:
                ok, msg = insert_word(nw, nt, ne, final_c)
                if ok:
                    st.success(msg)
                    pick_next_card()
                    st.rerun()
                else:
                    st.warning(msg)
            else:
                st.error("Uzupełnij wymagane pola!")

    st.divider()
    with st.expander(f"🗑️ Zarządzaj słówkami ({total_count})"):
        words_in_cat = get_words(category_filter=st.session_state.selected_category, due_only=False)
        if not words_in_cat:
            st.caption("Brak słówek w wybranej kategorii.")
        for item in words_in_cat:
            c1, c2 = st.columns([3, 1])
            c1.markdown(f"**{item['word']}** <small style='opacity:0.7'>(P.{item['level']})</small>", unsafe_allow_html=True)
            if c2.button("✖", key=f"del_{item['id']}", help="Usuń słówko"):
                delete_word(item["id"])
                pick_next_card()
                st.rerun()

    st.divider()
    st.link_button("📩 Formularz kontaktowy", "https://fabryka.tech/kontakt", use_container_width=True)


# --- NAGŁÓWEK GŁÓWNY ---
st.markdown("## 🎓 Inteligentne Fiszki & Ekstraktor Słów")
st.caption("Ucz się metodą pudełek Leitnera i automatycznie buduj bazę słownictwa z filmów YouTube oraz artykułów.")

# --- WIDOK GŁÓWNY: ZAKŁADKI ---
tab_learn, tab_yt, tab_scraper, tab_import_export, tab_stats = st.tabs([
    "🧠 Nauka Fiszek",
    "🎬 Z filmów YouTube",
    "🌐 Ze stron WWW",
    "📁 Pliki (Excel / Anki)", 
    "📊 Statystyki"
])

# ZAKŁADKA 1: NAUKA FISZEK (SYSTEM LEITNERA)
with tab_learn:
    current_due = get_words(category_filter=st.session_state.selected_category, due_only=True)
    current_total = get_words(category_filter=st.session_state.selected_category, due_only=False)

    if not current_total:
        st.info("💡 Brak słówek w tej kategorii. Pobierz słówka z YouTube, stron WWW lub dodaj je w pasku bocznym!")
    else:
        if not st.session_state.current_card or st.session_state.current_card not in current_total:
            pick_next_card()

        card = st.session_state.current_card

        if len(current_due) == 0:
            st.success(f"🎉 Wszystkie powtórki na dziś w kategorii **{st.session_state.selected_category}** zrobione! Możesz kontynuować naukę w trybie swobodnym.")

        # Wizualna Karta Fiszki
        context_html = (
            f'<div class="flashcard-context">„{card["example_sentence"]}”</div>'
            if card.get("example_sentence") else ""
        )

        st.markdown(
            f"""
            <div class="flashcard-box">
                <div class="flashcard-badges">
                    <span class="badge-pill">🏷️ {card['category']}</span>
                    <span class="badge-pill">📦 Pudełko {card['level']}/5 (co {card['interval']} dni)</span>
                </div>
                <div class="flashcard-word">🇬🇧 {card['word']}</div>
                {context_html}
            </div>
            """,
            unsafe_allow_html=True
        )

        # Odtwarzacz audio dla języka angielskiego
        col_audio_en1, col_audio_en2, col_audio_en3 = st.columns([1, 2, 1])
        with col_audio_en2:
            try:
                st.audio(speak_text(card["word"], lang="en"), format="audio/mp3")
            except Exception:
                st.caption("🔇 Brak audio")

        st.write("")
        col_act1, col_act2 = st.columns([3, 1])
        with col_act1:
            btn_label = "🙈 Ukryj tłumaczenie" if st.session_state.show_translation else "👁️ Pokaż tłumaczenie"
            if st.button(btn_label, use_container_width=True, type="primary" if not st.session_state.show_translation else "secondary"):
                st.session_state.show_translation = not st.session_state.show_translation
                st.rerun()
        with col_act2:
            if st.button("Następne ⏭️", use_container_width=True):
                pick_next_card()
                st.rerun()

        # Sekcja odpowiedzi (po odkryciu karty)
        if st.session_state.show_translation:
            with st.container(border=True):
                st.markdown(
                    f'<div class="flashcard-translation" style="text-align:center;">🇵🇱 {card["translation"]}</div>',
                    unsafe_allow_html=True
                )
                col_audio_pl1, col_audio_pl2, col_audio_pl3 = st.columns([1, 2, 1])
                with col_audio_pl2:
                    try:
                        st.audio(speak_text(card["translation"], lang="pl"), format="audio/mp3")
                    except Exception:
                        st.caption("🔇 Brak audio")

                st.write("")
                col_ok, col_nok = st.columns(2)
                with col_ok:
                    next_box = min(5, card["level"] + 1)
                    days = BOX_INTERVALS[next_box]
                    if st.button(f"✅ Znam (Pudełko {next_box} za {days} d.)", use_container_width=True, type="primary"):
                        record_review(card["id"], is_correct=True)
                        pick_next_card()
                        st.rerun()
                with col_nok:
                    if st.button("❌ Powtórz (Cofnij do Pudełka 1)", use_container_width=True):
                        record_review(card["id"], is_correct=False)
                        pick_next_card()
                        st.rerun()


# ZAKŁADKA 2: POBIERANIE I TŁUMACZENIE Z YOUTUBE
with tab_yt:
    with st.container(border=True):
        st.subheader("🎬 Ekstrakcja słówek z transkrypcji YouTube")
        st.caption("Wklej link do filmu YouTube lub wklej bezpośrednio tekst transkrypcji.")

        yt_url = st.text_input("Link do filmu YouTube:", placeholder="https://www.youtube.com/watch?v=HLDPA3FTUJ4")
        
        col_y1, col_y2, col_y3 = st.columns(3)
        with col_y1:
            yt_cat = st.text_input("Przypisz kategorię:", value="YouTube")
        with col_y2:
            yt_limit = st.slider("Liczba słówek z filmu:", min_value=5, max_value=30, value=15, key="yt_limit")
        with col_y3:
            yt_min_len = st.slider("Minimalna długość słowa:", min_value=4, max_value=8, value=5, key="yt_min_len")

        if st.button("🎥 Pobierz napisy i przetłumacz", type="primary", use_container_width=True):
            if not yt_url:
                st.error("Wklej link do filmu!")
            else:
                with st.spinner("Pobieranie transkrypcji i tłumaczenie słówek..."):
                    try:
                        yt_data = fetch_and_translate_youtube_transcript(yt_url, max_words=yt_limit, min_len=yt_min_len)
                        if yt_data:
                            st.session_state.yt_preview = yt_data
                            st.success(f"Znaleziono i przetłumaczono {len(yt_data)} słówek!")
                        else:
                            st.warning("Nie znaleziono pasujących słówek.")
                    except Exception as e:
                        st.error(f"Błąd pobierania automatycznego: {e}")

    with st.expander("📝 Alternatywa: Wklej transkrypcję ręcznie (omija blokady serwera)"):
        st.caption("Jeśli serwer chmurowy YouTube zablokował adres IP, skopiuj tekst transkrypcji z YouTube i wklej go tutaj:")
        manual_sub_text = st.text_area("Wklej transkrypcję / tekst angielski:", height=130)
        if st.button("Wyodrębnij słówka z wklejonego tekstu", use_container_width=True):
            if manual_sub_text.strip():
                with st.spinner("Analizowanie tekstu i tłumaczenie..."):
                    lines = [l.strip() for l in manual_sub_text.splitlines() if l.strip()]
                    extracted = process_text_into_vocabulary(manual_sub_text, lines, yt_limit, yt_min_len)
                    if extracted:
                        st.session_state.yt_preview = extracted
                        st.success(f"Przetworzono i przetłumaczono {len(extracted)} słówek!")
                        st.rerun()
            else:
                st.warning("Pole tekstu jest puste!")

    if "yt_preview" in st.session_state and st.session_state.yt_preview:
        with st.container(border=True):
            st.markdown("#### 📋 Podgląd przetłumaczonych słówek:")
            df_yt = pd.DataFrame(st.session_state.yt_preview)
            st.dataframe(df_yt, use_container_width=True, hide_index=True)

            if st.button("💾 Zapisz słówka z YouTube do bazy SQL", use_container_width=True, type="primary"):
                saved = 0
                for item in st.session_state.yt_preview:
                    ok, _ = insert_word(
                        word=item["word"],
                        translation=item["translation"],
                        example=item["example"],
                        category=yt_cat
                    )
                    if ok:
                        saved += 1
                st.success(f"Dodano {saved} słówek do bazy fiszek!")
                del st.session_state.yt_preview
                pick_next_card()
                st.rerun()


# ZAKŁADKA 3: ZE STRON WWW
with tab_scraper:
    with st.container(border=True):
        st.subheader("🌐 Ekstrakcja słówek z artykułu internetowego")
        st.caption("Wklej link do artykułu w języku angielskim. Aplikacja pobierze treść, wyodrębni słowa i przetłumaczy je na język polski.")

        article_url = st.text_input("Adres URL strony:", placeholder="https://en.wikipedia.org/wiki/Artificial_intelligence")
        
        col_opt1, col_opt2, col_opt3 = st.columns(3)
        with col_opt1:
            scrape_category = st.text_input("Przypisz kategorię:", value="Z Internetu", key="scrape_cat")
        with col_opt2:
            words_limit = st.slider("Maksymalna liczba słówek:", min_value=5, max_value=30, value=10, key="scr_limit")
        with col_opt3:
            min_word_len = st.slider("Minimalna długość słówka:", min_value=4, max_value=8, value=5, key="scr_min_len")

        if st.button("🔍 Pobierz i przetłumacz słówka ze strony", type="primary", use_container_width=True):
            if not article_url:
                st.error("Podaj prawidłowy adres URL!")
            else:
                with st.spinner("Pobieranie danych i analiza..."):
                    try:
                        scraped_data = fetch_words_from_article_url(article_url, max_words=words_limit, min_len=min_word_len)
                        if scraped_data:
                            st.session_state.scraped_preview = scraped_data
                            st.success(f"Znaleziono i przetłumaczono {len(scraped_data)} słówek!")
                        else:
                            st.warning("Nie znaleziono słówek spełniających kryteria.")
                    except Exception as err:
                        st.error(f"❌ Błąd wykonania: {err}")
                        with st.expander("Szczegóły błędu"):
                            st.code(traceback.format_exc())

    if "scraped_preview" in st.session_state and st.session_state.scraped_preview:
        with st.container(border=True):
            st.markdown("#### 📋 Podgląd znalezionych słówek:")
            df_preview = pd.DataFrame(st.session_state.scraped_preview)
            st.dataframe(df_preview, use_container_width=True, hide_index=True)

            if st.button("💾 Zapisz słówka ze strony do bazy SQL", use_container_width=True, type="primary"):
                saved_count = 0
                for item in st.session_state.scraped_preview:
                    ok, _ = insert_word(
                        word=item["word"],
                        translation=item["translation"],
                        example=item["example"],
                        category=scrape_category
                    )
                    if ok:
                        saved_count += 1
                st.success(f"Zapisano {saved_count} nowych słówek!")
                del st.session_state.scraped_preview
                pick_next_card()
                st.rerun()


# ZAKŁADKA 4: IMPORT I EKSPORT
with tab_import_export:
    with st.container(border=True):
        st.subheader("📥 Masowy import słówek")
        st.caption("Obsługiwane kolumny: `word`, `translation`, `example` (opcjonalnie), `category` (opcjonalnie).")
        uploaded_file = st.file_uploader("Wybierz plik CSV lub Excel (.xlsx)", type=["csv", "xlsx"])

        if uploaded_file is not None:
            try:
                df_imported = (
                    pd.read_csv(uploaded_file, sep=None, engine="python")
                    if uploaded_file.name.endswith(".csv")
                    else pd.read_excel(uploaded_file)
                )
                df_imported = df_imported.fillna("")
                st.dataframe(df_imported.head(5), use_container_width=True, hide_index=True)

                if st.button("💾 Zapisz plik do bazy SQL", type="primary", use_container_width=True):
                    today_str = date.today().isoformat()
                    added = 0
                    for _, row in df_imported.iterrows():
                        w = str(row.get("word", "")).strip()
                        t = str(row.get("translation", "")).strip()
                        ex = str(row.get("example", "")).strip()
                        cat = str(row.get("category", "Ogólne")).strip() or "Ogólne"
                        if w and t:
                            ok, _ = execute_db_commit(
                                "INSERT OR IGNORE INTO words (word, translation, example_sentence, category, level, interval, next_review_date) VALUES (?, ?, ?, ?, 1, 1, ?)",
                                [w, t, ex, cat, today_str]
                            )
                            if ok:
                                added += 1
                    st.success(f"Zaimportowano {added} nowych słówek!")
                    pick_next_card()
                    st.rerun()
            except Exception as e:
                st.error(f"Błąd pliku: {e}")

    with st.container(border=True):
        st.subheader("📤 Eksport do Anki i CSV")
        st.caption("Pobierz całą swoją bazę słówek do pliku tekstowego kompatybilnego z Anki lub arkusza CSV.")
        all_export_words = get_words(category_filter="Wszystkie", due_only=False)
        if all_export_words:
            df_export = pd.DataFrame(all_export_words)
            col_anki, col_csv = st.columns(2)
            with col_anki:
                anki_df = pd.DataFrame({
                    "Front": df_export["word"],
                    "Back": df_export["translation"],
                    "Context": df_export["example_sentence"],
                    "Tags": df_export["category"],
                })
                st.download_button(
                    "🗂️ Pobierz dla Anki (.txt)",
                    data=anki_df.to_csv(sep="\t", index=False, header=False, encoding="utf-8"),
                    file_name="fiszki_anki.txt",
                    mime="text/tab-separated-values",
                    use_container_width=True
                )
            with col_csv:
                st.download_button(
                    "📊 Pobierz bazę CSV",
                    data=df_export.to_csv(index=False, encoding="utf-8"),
                    file_name="fiszki_baza.csv",
                    mime="text/csv",
                    use_container_width=True
                )
        else:
            st.info("Baza jest pusta – brak danych do eksportu.")


# ZAKŁADKA 5: STATYSTYKI
with tab_stats:
    st.subheader("📈 Raport Aktywności i Postępów")
    stats = get_analytics()

    m1, m2, m3 = st.columns(3)
    m1.metric("🔥 Daily Streak", f"{stats['streak']} dni")
    m2.metric("🎯 Skuteczność (7 dni)", f"{stats['accuracy']}%")
    m3.metric("📚 Powtórki (7 dni)", f"{stats['weekly_total']} słówek")

    st.write("")
    with st.container(border=True):
        st.markdown("#### 📅 Aktywność w ostatnich 7 dniach")
        if stats["chart_df"] is not None and stats["weekly_total"] > 0:
            st.bar_chart(stats["chart_df"], color=["#10b981", "#ef4444"], stack=True)
        else:
            st.info("Wykonaj pierwsze powtórki, aby zobaczyć wykres aktywności!")

    with st.container(border=True):
        st.markdown("#### 📦 Rozkład słówek w pudełkach Leitnera")
        all_current = get_words(category_filter="Wszystkie", due_only=False)
        box_counts = {
            f"Pudełko {b} ({BOX_INTERVALS[b]}d)": sum(1 for w in all_current if int(w["level"]) == b)
            for b in range(1, 6)
        }
        st.bar_chart(
            pd.DataFrame.from_dict(box_counts, orient="index", columns=["Liczba słówek"]),
            color="#6366f1"
        )
