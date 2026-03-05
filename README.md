🥗 NutriAI Discord Bot
A calorie tracking bot that lives in your Discord server. Log meals by typing, send food photos for AI calorie estimates, track macros, and monitor your weekly progress — all without giving away your phone number.

Features

Smart food search — searches Open Food Facts database, auto-rephrases if not found, falls back to Claude AI + web search as last resort
Photo logging — send a photo of any meal, Claude Vision estimates the calories and macros
Profile setup — calculates your BMI, TDEE, daily calorie target, and macros based on your stats
Weekly rate control — choose exactly how many grams per week to lose or gain, not just a hardcoded deficit
Goal timeline — tells you exactly how many weeks until you reach your target weight
Rate limit safe — handles Discord 429 errors automatically with retry and backoff


Commands
CommandWhat it does!setupProfile wizard — gender, weight, goal, weekly rate!summaryToday's meals + calorie total + macro progress bars!weekLast 7 days with calorie bars!profileYour stats, goal, and timeline!undoRemove the last logged meal!clearWipe today's entire log!helpShow command list200g chicken breastLog food by grams — smart search📷 PhotoAI estimates and logs calories from the photo
Food entry tags shown in replies:

No tag — found in Open Food Facts database directly
`~` — found after auto-simplifying your search
`AI` — found via Claude AI web search
`📷` — logged from a photo


Tech Stack
PartWhatLanguagePython 3.11Discord librarydiscord.pyDatabaseSQLite (single file, no server needed)Food dataOpen Food Facts API (free, no key)AIAnthropic Claude API (photo analysis + food search fallback)HostingRender (free tier)

Project Structure
nutriai-discord/
├── discord_bot.py      ← the entire bot
├── requirements.txt    ← Python packages
├── runtime.txt         ← pins Python 3.11 for Render
├── .env                ← your secret tokens (never commit this)
├── .gitignore          ← keeps .env and database off GitHub
└── nutriai.db          ← SQLite database (auto-created on first run)

Setup
1. Clone and install
powershellgit clone https://github.com/prodevmod/nutriai-discord.git
cd nutriai-discord
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
2. Create your .env file
DISCORD_BOT_TOKEN=your_discord_bot_token_here
ANTHROPIC_API_KEY=your_anthropic_api_key_here
3. Get your tokens
Discord Bot Token:

Go to discord.com/developers/applications
New Application → Bot → Reset Token → copy it
Enable Message Content Intent under Privileged Gateway Intents
Invite bot to your server via OAuth2 → URL Generator

Anthropic API Key:

Go to console.anthropic.com
API Keys → Create Key → copy it
Free $5 credit included — enough for ~1,600 photo analyses

4. Run locally
powershell.\venv\Scripts\activate
python discord_bot.py

Deploy to Render (free 24/7 hosting)

Push code to GitHub
Go to render.com → New → Web Service → connect your repo
Build command: pip install -r requirements.txt
Start command: python discord_bot.py
Add environment variables: DISCORD_BOT_TOKEN and ANTHROPIC_API_KEY
Set PYTHON_VERSION = 3.11.9 in environment variables
Turn Auto-Deploy OFF — deploy manually to avoid Discord rate limits
Click Deploy


Note: If you see a 429 error on first deploy, wait 10 minutes then redeploy manually. This is Discord temporarily blocking Render's IP after multiple rapid login attempts during setup. It clears on its own.


How Food Search Works
When you type 200g grilled skinless chicken breast:
Stage 1 — Search Open Food Facts as typed
        ↓ not found
Stage 2 — Strip cooking words (grilled, skinless...)
          Try: "chicken breast fillet" → "chicken breast" → "chicken"
        ↓ still not found
Stage 3 — Ask Claude AI with web search enabled
          Claude searches the internet for the macros
          Returns structured data → logged with AI badge

How the Calorie Target is Calculated
BMR  = Mifflin-St Jeor formula (based on gender, age, weight, height)
TDEE = BMR × activity multiplier
Daily change = (weekly_rate_g × 7.7 kcal) ÷ 7 days

Goal = lose   → target = TDEE - daily_change
Goal = gain   → target = TDEE + daily_change
Goal = maintain → target = TDEE

Floor: never below 1500 kcal (male) or 1200 kcal (female)

License
Personal project — free to use and modify.
