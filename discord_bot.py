# ═══════════════════════════════════════════════════════════════════
# NutriAI Discord Bot — v4
# ═══════════════════════════════════════════════════════════════════
# v4 changes:
#   • 15s startup delay — prevents Render crash-restart loops from
#     hammering Discord's login endpoint and triggering 429 blocks
#   • Exponential backoff on login 429 — waits 15s, 30s, 60s before
#     each retry instead of immediately crashing and restarting
#
# v3 features (kept):
#   • safe_send() / safe_edit() retry automatically on 429
#   • Global on_message try/except catches all uncaught 429s
#
# v2 features (kept):
#   • Setup asks g/week rate for lose/gain goals
#   • 3-stage smart food search: DB → rephrase → Claude AI + web
#
# Commands:
#   !start / !help  — welcome
#   !setup          — profile wizard
#   !profile        — your stats + goal timeline
#   !summary        — today's meals + macro bars
#   !week           — last 7 days
#   !undo           — remove last entry
#   !clear          — wipe today
#   200g chicken    — log food (smart search)
#   [photo]         — AI estimates calories from photo
# ═══════════════════════════════════════════════════════════════════

import discord
import os
import sqlite3
import httpx
import base64
import json
import re
import asyncio
from datetime import date, datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TOKEN         = os.getenv("DISCORD_BOT_TOKEN")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

setup_sessions = {}   # {user_id: {"step": int, "data": dict}}


# ══ RATE LIMIT HELPERS ════════════════════════════════════════════════════════
# Discord returns HTTP 429 when you send too many messages too fast.
# These wrappers catch it, wait the required cooldown, then retry once.
# All sends and edits in this bot go through these functions.

async def safe_send(channel, content: str):
    """Send a message, retrying once if rate limited."""
    for attempt in range(2):
        try:
            return await channel.send(content)
        except discord.errors.HTTPException as e:
            if e.status == 429 and attempt == 0:
                wait = getattr(e, "retry_after", 5)
                print(f"⏳ Rate limited on send — waiting {wait:.1f}s")
                await asyncio.sleep(wait + 0.5)
            else:
                raise

async def safe_edit(msg, content: str):
    """Edit a message, retrying once if rate limited."""
    for attempt in range(2):
        try:
            return await msg.edit(content=content)
        except discord.errors.HTTPException as e:
            if e.status == 429 and attempt == 0:
                wait = getattr(e, "retry_after", 5)
                print(f"⏳ Rate limited on edit — waiting {wait:.1f}s")
                await asyncio.sleep(wait + 0.5)
            else:
                raise


# ══ DATABASE ══════════════════════════════════════════════════════════════════

def get_conn():
    conn = sqlite3.connect("nutriai.db")
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            user_id          TEXT PRIMARY KEY,
            username         TEXT,
            gender           TEXT,
            age              INTEGER,
            weight_kg        REAL,
            height_cm        REAL,
            activity         TEXT,
            goal             TEXT,
            weekly_rate_g    REAL,
            target_weight_kg REAL,
            bmi              REAL,
            tdee             INTEGER,
            daily_target     INTEGER,
            daily_change     INTEGER,
            macro_protein    INTEGER,
            macro_carbs      INTEGER,
            macro_fat        INTEGER,
            weeks_to_goal    REAL,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS meals (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      TEXT,
            name         TEXT,
            grams        REAL,
            calories     REAL,
            protein      REAL,
            carbs        REAL,
            fat          REAL,
            logged_date  DATE,
            logged_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            from_photo   INTEGER DEFAULT 0,
            from_ai      INTEGER DEFAULT 0
        );
    """)
    conn.commit()
    conn.close()
    print("✅ Database ready")


def save_profile(user_id, username, data):
    kg       = data["weight_kg"]
    cm       = data["height_cm"]
    age      = data["age"]
    gender   = data["gender"]
    activity = data["activity"]
    goal     = data["goal"]
    rate_g   = data.get("weekly_rate_g", 0)
    target_w = data.get("target_weight_kg")

    # BMI
    bmi = round(kg / ((cm / 100) ** 2), 1)

    # BMR — Mifflin-St Jeor
    bmr = (10 * kg + 6.25 * cm - 5 * age + 5) if gender == "male" \
          else (10 * kg + 6.25 * cm - 5 * age - 161)

    # TDEE
    multipliers = {
        "sedentary": 1.2, "light": 1.375,
        "moderate": 1.55, "active": 1.725, "veryactive": 1.9
    }
    tdee = int(bmr * multipliers.get(activity, 1.55))

    # Daily calorie change — 1g fat ≈ 7.7 kcal
    # weekly_rate_g × 7.7 ÷ 7 = daily deficit or surplus
    daily_change = int((rate_g * 7.7) / 7) if rate_g else 0

    if goal == "lose":
        target = tdee - daily_change
    elif goal == "gain":
        target = tdee + daily_change
    else:
        target = tdee
        daily_change = 0

    # Safety floor
    floor  = 1500 if gender == "male" else 1200
    target = max(target, floor)

    # Macros
    macro_protein = int(kg * 2)
    macro_fat     = int((target * 0.25) / 9)
    macro_carbs   = max(0, int((target - macro_protein * 4 - macro_fat * 9) / 4))

    # Weeks to goal
    weeks_to_goal = None
    if target_w and rate_g > 0:
        weeks_to_goal = round((abs(kg - target_w) * 1000) / rate_g, 1)

    conn = get_conn()
    conn.execute("""
        INSERT INTO profiles
          (user_id, username, gender, age, weight_kg, height_cm, activity,
           goal, weekly_rate_g, target_weight_kg, bmi, tdee, daily_target,
           daily_change, macro_protein, macro_carbs, macro_fat, weeks_to_goal)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
          username=excluded.username, gender=excluded.gender,
          age=excluded.age, weight_kg=excluded.weight_kg,
          height_cm=excluded.height_cm, activity=excluded.activity,
          goal=excluded.goal, weekly_rate_g=excluded.weekly_rate_g,
          target_weight_kg=excluded.target_weight_kg,
          bmi=excluded.bmi, tdee=excluded.tdee,
          daily_target=excluded.daily_target,
          daily_change=excluded.daily_change,
          macro_protein=excluded.macro_protein,
          macro_carbs=excluded.macro_carbs, macro_fat=excluded.macro_fat,
          weeks_to_goal=excluded.weeks_to_goal
    """, (
        user_id, username, gender, age, kg, cm, activity,
        goal, rate_g, target_w, bmi, tdee, target,
        daily_change, macro_protein, macro_carbs, macro_fat, weeks_to_goal
    ))
    conn.commit()
    conn.close()

    return {
        "bmi": bmi, "tdee": tdee, "target": target,
        "daily_change": daily_change, "rate_g": rate_g,
        "macro_protein": macro_protein, "macro_carbs": macro_carbs,
        "macro_fat": macro_fat, "weeks_to_goal": weeks_to_goal,
    }


def get_profile(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def log_meal(user_id, name, grams, cal, protein, carbs, fat,
             from_photo=False, from_ai=False):
    conn = get_conn()
    conn.execute(
        "INSERT INTO meals "
        "(user_id,name,grams,calories,protein,carbs,fat,"
        " logged_date,from_photo,from_ai) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (user_id, name, grams, cal, protein, carbs, fat,
         date.today().isoformat(), int(from_photo), int(from_ai))
    )
    conn.commit()
    conn.close()

def get_today_meals(user_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM meals WHERE user_id=? AND logged_date=? ORDER BY logged_at",
        (user_id, date.today().isoformat())
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_week_meals(user_id):
    conn = get_conn()
    since = (date.today() - timedelta(days=6)).isoformat()
    rows = conn.execute(
        "SELECT logged_date, SUM(calories) as cal FROM meals"
        " WHERE user_id=? AND logged_date>=?"
        " GROUP BY logged_date ORDER BY logged_date",
        (user_id, since)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def undo_last(user_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT id,name,grams,calories FROM meals"
        " WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)
    ).fetchone()
    if row:
        conn.execute("DELETE FROM meals WHERE id=?", (row["id"],))
        conn.commit()
        conn.close()
        return dict(row)
    conn.close()
    return None

def clear_today(user_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM meals WHERE user_id=? AND logged_date=?",
        (user_id, date.today().isoformat())
    )
    conn.commit()
    conn.close()


# ══ SMART FOOD SEARCH — 3-STAGE FALLBACK ══════════════════════════════════════
#
#  Stage 1 — Search Open Food Facts with original query
#  Stage 2 — Strip cooking adjectives, try shorter versions
#  Stage 3 — Ask Claude AI with web_search tool
#
# ══════════════════════════════════════════════════════════════════════════════

def rephrase_queries(query: str) -> list:
    """Generate simplified alternatives to try if the original query fails."""
    cooking_words = [
        "grilled", "fried", "baked", "roasted", "steamed", "boiled",
        "sauteed", "sautéed", "pan-fried", "deep-fried", "deep fried",
        "air-fried", "air fried", "smoked", "braised", "poached",
        "raw", "fresh", "cooked", "uncooked", "whole", "sliced",
        "diced", "chopped", "minced", "grated", "mashed",
        "homemade", "home-made", "home made", "canned", "tinned",
        "frozen", "organic", "natural", "plain", "simple",
        "low-fat", "low fat", "fat-free", "fat free", "reduced-fat",
        "reduced fat", "light", "lean", "extra lean", "extra-lean",
        "boneless", "skinless", "unsalted", "salted",
        "whole grain", "whole-grain", "wholegrain",
    ]
    q       = query.lower().strip()
    cleaned = q
    for w in cooking_words:
        cleaned = re.sub(r'\b' + re.escape(w) + r'\b', '', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    words   = cleaned.split() if cleaned else q.split()

    attempts = []
    if cleaned and cleaned != q:
        attempts.append(cleaned)
    if len(words) >= 3:
        attempts.append(" ".join(words[:3]))
    if len(words) >= 2:
        attempts.append(" ".join(words[:2]))
    if words:
        attempts.append(words[0])

    seen, result = {q}, []
    for a in attempts:
        a = a.strip()
        if a and a not in seen:
            seen.add(a)
            result.append(a)
    return result


async def _query_off(query: str, grams: float):
    """Single Open Food Facts lookup. Returns macro dict or None."""
    try:
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://world.openfoodfacts.org/cgi/search.pl",
                params={
                    "search_terms": query, "search_simple": 1,
                    "action": "process", "json": 1, "page_size": 5,
                    "fields": "product_name,nutriments",
                },
                timeout=8,
            )
        for p in r.json().get("products", []):
            n    = p.get("nutriments", {})
            kcal = n.get("energy-kcal_100g", 0)
            if kcal and kcal > 0:
                f = grams / 100
                return {
                    "name":     p.get("product_name", query),
                    "calories": round(kcal * f, 1),
                    "protein":  round(n.get("proteins_100g",      0) * f, 1),
                    "carbs":    round(n.get("carbohydrates_100g", 0) * f, 1),
                    "fat":      round(n.get("fat_100g",           0) * f, 1),
                }
        return None
    except Exception:
        return None


async def _query_ai(food: str, grams: float):
    """Ask Claude AI (with web search) for macros. Last resort fallback."""
    if not ANTHROPIC_KEY:
        return None
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 400,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": [{
                        "role": "user",
                        "content": (
                            f"Find the nutritional values for: {food}\n"
                            f"Portion size: {grams}g\n\n"
                            f"Search the web if needed. "
                            f"Reply ONLY with a single valid JSON object, no markdown:\n"
                            f'{{"name":"{food}","grams":{grams},'
                            f'"calories":0,"protein":0,"carbs":0,"fat":0}}\n\n'
                            f"All values must be numbers for the exact {grams}g portion."
                        ),
                    }],
                },
                timeout=40,
            )
        content = r.json().get("content", [])
        text    = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
        match   = re.search(r'\{[^{}]+\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            for f in ("calories", "protein", "carbs", "fat"):
                data.setdefault(f, 0)
            return data
        return None
    except Exception:
        return None


async def search_food(query: str, grams: float):
    """
    3-stage smart search. Returns (result_dict, method).
    method: "db" | "rephrased" | "ai" | "failed"
    """
    result = await _query_off(query, grams)
    if result:
        return result, "db"

    for alt in rephrase_queries(query):
        result = await _query_off(alt, grams)
        if result:
            result["name"] = query
            return result, "rephrased"

    result = await _query_ai(query, grams)
    if result:
        return result, "ai"

    return None, "failed"


# ══ PHOTO ANALYSIS ════════════════════════════════════════════════════════════

async def analyze_photo(img_bytes: bytes):
    if not ANTHROPIC_KEY:
        return None
    b64 = base64.b64encode(img_bytes).decode()
    try:
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key":         ANTHROPIC_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type":      "application/json",
                },
                json={
                    "model":      "claude-sonnet-4-20250514",
                    "max_tokens": 300,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {
                                "type": "base64", "media_type": "image/jpeg", "data": b64,
                            }},
                            {"type": "text", "text": (
                                "Identify the food in this photo and estimate portion size. "
                                "Reply ONLY with valid JSON, no markdown:\n"
                                '{"name":"food name","grams":100,'
                                '"calories":0,"protein":0,"carbs":0,"fat":0}'
                            )},
                        ],
                    }],
                },
                timeout=30,
            )
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        return json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception:
        return None


# ══ DISPLAY HELPERS ═══════════════════════════════════════════════════════════

def parse_food(text: str):
    t  = text.strip().lower()
    m  = re.match(r'^(\d+(?:\.\d+)?)\s*g\s+(.+)$', t)
    if m:
        return float(m.group(1)), m.group(2).strip()
    m2 = re.match(r'^(.+?)\s+(\d+(?:\.\d+)?)\s*g$', t)
    if m2:
        return float(m2.group(2)), m2.group(1).strip()
    return None, None

def bmi_label(bmi):
    if bmi < 18.5: return "Underweight 📉"
    if bmi < 25:   return "Normal ✅"
    if bmi < 30:   return "Overweight ⚠️"
    return "Obese 🔴"

def progress_bar(current, maximum, width=10):
    pct    = min(current / maximum, 1.0) if maximum else 0
    filled = int(pct * width)
    return f"`{'█' * filled}{'░' * (width - filled)}` {current:.0f}/{maximum}"

def build_summary(meals, profile):
    if not meals:
        return "📭 No meals logged today.\nType `200g oatmeal` to start."
    total_cal = sum(m["calories"] for m in meals)
    total_p   = sum(m["protein"]  for m in meals)
    total_c   = sum(m["carbs"]    for m in meals)
    total_f   = sum(m["fat"]      for m in meals)

    lines = [f"📊 **Today — {date.today().strftime('%A %d %B')}**\n"]
    for m in meals:
        tag = " `📷`" if m["from_photo"] else (" `AI`" if m["from_ai"] else "")
        lines.append(
            f"• **{m['name'].title()}** ({m['grams']}g){tag}"
            f" — {m['calories']:.0f} kcal"
        )
    lines.append("\n━━━━━━━━━━━━━━━━")
    lines.append(f"🔥 **Total: {total_cal:.0f} kcal**")
    if profile:
        remaining = profile["daily_target"] - total_cal
        emoji     = "✅" if remaining >= 0 else "⚠️"
        direction = "Remaining" if remaining >= 0 else "Over by"
        lines.append(
            f"{emoji} {direction}: **{abs(remaining):.0f} kcal**"
            f" (target: {profile['daily_target']})"
        )
        lines.append(f"\n💪 Protein  {progress_bar(total_p, profile['macro_protein'])}")
        lines.append(f"🍞 Carbs    {progress_bar(total_c, profile['macro_carbs'])}")
        lines.append(f"🧈 Fat      {progress_bar(total_f, profile['macro_fat'])}")
    else:
        lines.append(f"💪 P:{total_p:.1f}g  🍞 C:{total_c:.1f}g  🧈 F:{total_f:.1f}g")
        lines.append("_Type `!setup` to set your calorie target_")
    return "\n".join(lines)


# ══ SETUP FLOW ════════════════════════════════════════════════════════════════

BASE_STEPS = [
    ("gender",   "**Step 1 — Gender**\nType `male` or `female`"),
    ("age",      "**Step 2 — Age**\nType your age in years (e.g. `28`)"),
    ("weight",   "**Step 3 — Weight**\nType your current weight in kg (e.g. `80`)"),
    ("height",   "**Step 4 — Height**\nType your height in cm (e.g. `175`)"),
    ("activity", "**Step 5 — Activity Level**\nType one of:\n"
                 "`sedentary` — desk job, little movement\n"
                 "`light` — 1-3 workouts/week\n"
                 "`moderate` — 3-5 workouts/week\n"
                 "`active` — 6-7 workouts/week\n"
                 "`veryactive` — athlete / 2x per day"),
    ("goal",     "**Step 6 — Goal**\nType one of:\n"
                 "`lose` — lose body fat\n"
                 "`maintain` — maintain current weight\n"
                 "`gain` — build muscle / gain weight"),
]

RATE_LOSE_STEP = (
    "weekly_rate_g",
    "**Step 7 — How fast do you want to lose weight?**\n"
    "Type how many **grams per week** you want to lose:\n\n"
    "`250` — slow & sustainable (~0.25 kg/week, barely any hunger)\n"
    "`500` — standard (-500 kcal/day, ~0.5 kg/week) ✅ **recommended**\n"
    "`750` — faster (~0.75 kg/week, noticeable hunger)\n"
    "`1000` — aggressive (~1 kg/week, hard to sustain)\n\n"
    "_Any number from 100 to 1000_"
)

RATE_GAIN_STEP = (
    "weekly_rate_g",
    "**Step 7 — How fast do you want to gain weight?**\n"
    "Type how many **grams per week** you want to gain:\n\n"
    "`250` — lean bulk, minimal fat gain ✅ **recommended**\n"
    "`500` — standard bulk (~0.5 kg/week)\n"
    "`750` — aggressive bulk (faster, more fat alongside muscle)\n\n"
    "_Any number from 100 to 1000_"
)

TARGET_WEIGHT_STEP = (
    "target_weight_kg",
    "**Step 8 — Target weight (optional)**\n"
    "Type your goal weight in kg to see how many weeks it will take.\n"
    "Or type `skip`."
)

VALID_CHOICES = {
    "gender":   ["male", "female"],
    "activity": ["sedentary", "light", "moderate", "active", "veryactive"],
    "goal":     ["lose", "maintain", "gain"],
}

def build_steps(data: dict) -> list:
    steps = list(BASE_STEPS)
    goal  = data.get("goal", "")
    if goal == "lose":
        steps.append(RATE_LOSE_STEP)
        steps.append(TARGET_WEIGHT_STEP)
    elif goal == "gain":
        steps.append(RATE_GAIN_STEP)
    return steps


async def handle_setup(message, user_id, text):
    session  = setup_sessions[user_id]
    data     = session["data"]
    step_idx = session["step"]
    steps    = build_steps(data)

    if step_idx >= len(steps):
        await finish_setup(message, user_id)
        return

    key, _ = steps[step_idx]
    t      = text.strip().lower()

    # Validate
    if key in VALID_CHOICES:
        if t not in VALID_CHOICES[key]:
            opts = " / ".join(f"`{v}`" for v in VALID_CHOICES[key])
            await safe_send(message.channel, f"❌ Please type one of: {opts}")
            return
        data[key] = t

    elif key in ("age", "weight", "height"):
        try:
            val = float(text.replace(",", "."))
            if val <= 0:
                raise ValueError
        except ValueError:
            await safe_send(message.channel, "❌ Please enter a positive number.")
            return
        data[key] = int(val) if key == "age" else val

    elif key == "weekly_rate_g":
        try:
            val = int(float(text.replace(",", ".")))
            if not (100 <= val <= 1000):
                raise ValueError
        except ValueError:
            await safe_send(message.channel,
                "❌ Please enter a number between **100** and **1000** (grams per week).")
            return
        data["weekly_rate_g"] = val

    elif key == "target_weight_kg":
        if t == "skip":
            data["target_weight_kg"] = None
        else:
            try:
                val = float(text.replace(",", "."))
                if val <= 0:
                    raise ValueError
                data["target_weight_kg"] = val
            except ValueError:
                await safe_send(message.channel, "❌ Enter a weight in kg, or type `skip`.")
                return

    # Advance
    session["step"] += 1
    steps = build_steps(data)

    if session["step"] < len(steps):
        _, prompt = steps[session["step"]]
        total     = len(steps)
        await safe_send(message.channel,
            f"✅ Got it!\n\n**Step {session['step'] + 1}/{total}**\n{prompt}")
    else:
        await finish_setup(message, user_id)


async def finish_setup(message, user_id):
    session = setup_sessions.pop(user_id)
    d       = session["data"]
    d.setdefault("weekly_rate_g", 0)

    result = save_profile(user_id, str(message.author), {
        "gender":           d["gender"],
        "age":              d["age"],
        "weight_kg":        d["weight"],
        "height_cm":        d["height"],
        "activity":         d["activity"],
        "goal":             d["goal"],
        "weekly_rate_g":    d["weekly_rate_g"],
        "target_weight_kg": d.get("target_weight_kg"),
    })

    goal_text = {
        "lose":     "Lose Weight 🔻",
        "maintain": "Maintain ⚖️",
        "gain":     "Build Muscle 📈",
    }[d["goal"]]

    rate_g = result["rate_g"]
    if d["goal"] == "lose" and rate_g:
        rate_line = (
            f"📉 Losing **{rate_g}g/week** = **{rate_g/1000:.3g} kg/week**"
            f" ({result['daily_change']} kcal/day deficit)\n"
        )
    elif d["goal"] == "gain" and rate_g:
        rate_line = (
            f"📈 Gaining **{rate_g}g/week** = **{rate_g/1000:.3g} kg/week**"
            f" ({result['daily_change']} kcal/day surplus)\n"
        )
    else:
        rate_line = ""

    timeline = ""
    if result["weeks_to_goal"] and d.get("target_weight_kg"):
        arrive   = date.today() + timedelta(weeks=result["weeks_to_goal"])
        timeline = (
            f"🏁 Reach **{d['target_weight_kg']}kg** in"
            f" ~**{result['weeks_to_goal']} weeks**"
            f" ({arrive.strftime('%B %Y')})\n"
        )

    await safe_send(message.channel,
        f"🎉 **Profile saved!**\n\n"
        f"📏 BMI: **{result['bmi']}** — {bmi_label(result['bmi'])}\n"
        f"⚡ Maintenance (TDEE): **{result['tdee']} kcal/day**\n"
        f"🎯 Daily target: **{result['target']} kcal** ({goal_text})\n"
        f"{rate_line}"
        f"{timeline}"
        f"\n💪 Protein: **{result['macro_protein']}g**  "
        f"🍞 Carbs: **{result['macro_carbs']}g**  "
        f"🧈 Fat: **{result['macro_fat']}g**\n\n"
        f"All set! Try: `200g chicken breast`"
    )


# ══ BOT EVENTS ════════════════════════════════════════════════════════════════

@client.event
async def on_ready():
    print(f"🥗 NutriAI Bot v3 online as {client.user}")
    print(f"   Servers: {[g.name for g in client.guilds]}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    user_id  = str(message.author.id)
    text     = message.content.strip()
    text_low = text.lower()

    try:
        await _handle_message(message, user_id, text, text_low)
    except discord.errors.HTTPException as e:
        if e.status == 429:
            wait = getattr(e, "retry_after", 10)
            print(f"⏳ Global rate limit hit — waiting {wait:.1f}s")
            await asyncio.sleep(wait + 1)
        else:
            print(f"Discord error in on_message: {e}")
    except Exception as e:
        print(f"Unexpected error: {e}")


async def _handle_message(message, user_id, text, text_low):
    """All message handling logic — called from on_message with error wrapping."""

    # ── Active setup session ───────────────────────────────────────────────────
    if user_id in setup_sessions:
        if text_low in ("!cancel", "!exit"):
            del setup_sessions[user_id]
            await safe_send(message.channel, "❌ Setup cancelled.")
            return
        await handle_setup(message, user_id, text)
        return

    # ── Commands ───────────────────────────────────────────────────────────────
    if text_low in ("!start", "!help"):
        await safe_send(message.channel,
            "🥗 **NutriAI v3 — Calorie Tracker**\n\n"
            "**Log food:**\n"
            "`200g chicken breast` — smart search (DB → rephrase → AI web)\n"
            "📷 Send a photo — AI identifies food + estimates calories\n\n"
            "**Commands:**\n"
            "`!setup` — profile wizard (includes weekly rate)\n"
            "`!summary` — today's meals + macro progress bars\n"
            "`!week` — last 7 days\n"
            "`!profile` — your stats and goal timeline\n"
            "`!undo` — remove last entry\n"
            "`!clear` — wipe today's log\n"
            "`!help` — this message\n\n"
            "_Tags: no tag = database  `~` = rephrased  `AI` = web search  `📷` = photo_"
        )
        return

    if text_low == "!setup":
        setup_sessions[user_id] = {"step": 0, "data": {}}
        _, prompt = BASE_STEPS[0]
        await safe_send(message.channel,
            f"👋 **NutriAI Setup**  (type `!cancel` to stop)\n\n"
            f"**Step 1/{len(BASE_STEPS)}+**\n{prompt}"
        )
        return

    if text_low == "!profile":
        p = get_profile(user_id)
        if not p:
            await safe_send(message.channel, "❌ No profile found. Type `!setup` first.")
            return
        goal_map  = {"lose": "Lose Weight 🔻", "maintain": "Maintain ⚖️", "gain": "Build Muscle 📈"}
        rate_line = ""
        if p.get("weekly_rate_g") and p["goal"] != "maintain":
            verb      = "Losing" if p["goal"] == "lose" else "Gaining"
            sign      = "-" if p["goal"] == "lose" else "+"
            rate_line = (
                f"📊 Rate: {verb} **{p['weekly_rate_g']}g/week**"
                f" ({sign}{p['daily_change']} kcal/day)\n"
            )
        timeline = ""
        if p.get("weeks_to_goal") and p.get("target_weight_kg"):
            arrive   = date.today() + timedelta(weeks=p["weeks_to_goal"])
            timeline = (
                f"🏁 Goal: **{p['target_weight_kg']}kg** in"
                f" ~{p['weeks_to_goal']} weeks ({arrive.strftime('%b %Y')})\n"
            )
        await safe_send(message.channel,
            f"👤 **Your Profile**\n\n"
            f"{p['gender'].title()} | {p['age']}y | {p['weight_kg']}kg | {p['height_cm']}cm\n"
            f"📏 BMI: **{p['bmi']}** — {bmi_label(p['bmi'])}\n"
            f"⚡ TDEE: **{p['tdee']} kcal/day**\n"
            f"🎯 Goal: {goal_map.get(p['goal'], p['goal'])}\n"
            f"{rate_line}{timeline}"
            f"🔥 Daily target: **{p['daily_target']} kcal**\n"
            f"💪 {p['macro_protein']}g protein | "
            f"🍞 {p['macro_carbs']}g carbs | "
            f"🧈 {p['macro_fat']}g fat\n\n"
            f"_Type `!setup` to update_"
        )
        return

    if text_low == "!summary":
        await safe_send(message.channel,
            build_summary(get_today_meals(user_id), get_profile(user_id)))
        return

    if text_low == "!week":
        rows   = get_week_meals(user_id)
        p      = get_profile(user_id)
        target = p["daily_target"] if p else None
        if not rows:
            await safe_send(message.channel, "No meals logged this week yet.")
            return
        lines = ["📅 **Last 7 Days**\n"]
        for row in rows:
            d    = datetime.fromisoformat(row["logged_date"]).strftime("%a %d %b")
            cal  = row["cal"]
            bar  = progress_bar(cal, target) if target else f"{cal:.0f} kcal"
            flag = " ⚠️" if target and cal > target else ""
            lines.append(f"**{d}** — {bar}{flag}")
        await safe_send(message.channel, "\n".join(lines))
        return

    if text_low == "!undo":
        removed = undo_last(user_id)
        if removed:
            await safe_send(message.channel,
                f"❌ Removed: **{removed['name'].title()}**"
                f" ({removed['grams']}g — {removed['calories']:.0f} kcal)"
            )
        else:
            await safe_send(message.channel, "Nothing to undo.")
        return

    if text_low == "!clear":
        clear_today(user_id)
        await safe_send(message.channel, "🗑️ Today's log cleared.")
        return

    # ── Photo ──────────────────────────────────────────────────────────────────
    if message.attachments:
        att = message.attachments[0]
        if not any(att.filename.lower().endswith(e)
                   for e in (".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return
        if not ANTHROPIC_KEY:
            await safe_send(message.channel,
                "❌ Add `ANTHROPIC_API_KEY` to `.env` for photo analysis.")
            return
        thinking = await safe_send(message.channel, "📸 Analyzing photo with AI...")
        async with httpx.AsyncClient() as c:
            img_bytes = (await c.get(att.url)).content
        data = await analyze_photo(img_bytes)
        if not data:
            await safe_edit(thinking,
                "❌ Couldn't analyze that photo.\n"
                "Try a clearer shot or log manually: `200g pizza`"
            )
            return
        log_meal(user_id, data["name"], data["grams"],
                 data["calories"], data["protein"], data["carbs"], data["fat"],
                 from_photo=True)
        meals  = get_today_meals(user_id)
        total  = sum(m["calories"] for m in meals)
        p      = get_profile(user_id)
        t_line = (f"\n📈 Today: **{total:.0f}** / {p['daily_target']} kcal"
                  if p else f"\n📈 Today: **{total:.0f} kcal**")
        await safe_edit(thinking,
            f"📸 **{data['name'].title()}** (~{data['grams']}g)\n\n"
            f"🔥 {data['calories']} kcal\n"
            f"💪 P:{data['protein']}g  🍞 C:{data['carbs']}g  🧈 F:{data['fat']}g"
            f"{t_line}\n\n_`!undo` if estimate is wrong_"
        )
        return

    # ── Text food entry: "200g chicken breast" ─────────────────────────────────
    grams, food = parse_food(text)
    if grams is None:
        return   # not a food message — silently ignore

    thinking = await safe_send(message.channel, "🔍 Searching...")
    result, method = await search_food(food, grams)

    if not result:
        await safe_edit(thinking,
            f"❌ Couldn't find **{food}** anywhere —"
            f" tried the database, rephrased versions, and AI web search.\n"
            f"Try a simpler name, e.g. `chicken` or `rice`."
        )
        return

    method_note = {
        "db":        "",
        "rephrased": "\n_Matched after simplifying your search_ `~`",
        "ai":        "\n_Macros found via AI web search_ `AI`",
    }.get(method, "")

    log_meal(user_id, food, grams,
             result["calories"], result["protein"], result["carbs"], result["fat"],
             from_ai=(method == "ai"))

    meals  = get_today_meals(user_id)
    total  = sum(m["calories"] for m in meals)
    p      = get_profile(user_id)
    t_line = (f"\n📈 Today: **{total:.0f}** / {p['daily_target']} kcal"
              if p else f"\n📈 Today: **{total:.0f} kcal**")

    await safe_edit(thinking,
        f"✅ **{food.title()}** — {grams}g\n\n"
        f"🔥 {result['calories']} kcal\n"
        f"💪 Protein: {result['protein']}g\n"
        f"🍞 Carbs:   {result['carbs']}g\n"
        f"🧈 Fat:     {result['fat']}g"
        f"{t_line}"
        f"{method_note}\n\n"
        f"_`!undo` to remove  •  `!summary` for full log_"
    )


# ══ START ═════════════════════════════════════════════════════════════════════
#
# Why the sleep(15) is here:
#   Render restarts the bot immediately when it crashes. If Discord's login
#   endpoint is rate-limited (429), the bot crashes, Render restarts instantly,
#   it tries to login again, gets 429 again, crashes again — infinite loop that
#   extends the block indefinitely. The 15s delay breaks that loop by giving
#   Discord's rate limit time to expire before each login attempt.
#
# Why the retry loop is here:
#   On first deploy or after a long gap, Discord sometimes returns 429 on
#   the very first login. Instead of crashing and letting Render restart
#   immediately, we catch it here and wait progressively longer (15s, 30s,
#   60s) before retrying — exponential backoff.

import time

if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_BOT_TOKEN not found in .env")
        print("Add DISCORD_BOT_TOKEN to your .env file or Render environment variables.")
        exit(1)

    init_db()

    # Startup delay — prevents crash-restart loop from hammering Discord
    print("⏳ Waiting 15s before connecting (prevents rate limit loops)...")
    time.sleep(15)

    # Retry loop with exponential backoff for login 429s
    MAX_RETRIES   = 5
    retry_delays  = [15, 30, 60, 120, 300]   # seconds between retries

    for attempt in range(MAX_RETRIES):
        try:
            print(f"🥗 Starting NutriAI Discord Bot v4... (attempt {attempt + 1}/{MAX_RETRIES})")
            client.run(TOKEN)
            break   # clean exit — don't retry
        except discord.errors.HTTPException as e:
            if e.status == 429:
                if attempt < MAX_RETRIES - 1:
                    wait = retry_delays[attempt]
                    print(f"⏳ Discord login rate limited (429) — waiting {wait}s before retry...")
                    time.sleep(wait)
                else:
                    print("❌ Max retries reached. Discord is still rate limiting this IP.")
                    print("   Wait 10-15 minutes then manually redeploy on Render.")
                    exit(1)
            else:
                print(f"❌ Discord HTTP error on login: {e}")
                exit(1)
        except Exception as e:
            print(f"❌ Unexpected error on startup: {e}")
            exit(1)