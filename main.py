import discord
from discord.ext import commands
import os
from flask import Flask, request, redirect, session, url_for
from threading import Thread
import datetime
import json
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import psycopg2
import logging
import traceback

# ==============================================================================
# 1. BOT & SERVER SETUP (RENDER)
# ==============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TOKEN = os.environ.get('DISCORD_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL')

if not all([TOKEN, DATABASE_URL, FLASK_SECRET_KEY, RENDER_EXTERNAL_URL]):
    raise ValueError("One or more required environment variables are missing.")

intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents) 
app = Flask('')
app.secret_key = FLASK_SECRET_KEY

# ==============================================================================
# 2. DATABASE SETUP (POSTGRESQL)
# ==============================================================================
def init_db():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS google_tokens (
                user_id BIGINT PRIMARY KEY,
                token_json TEXT NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        logging.info("Database initialized successfully.")
    except Exception as e:
        logging.error(f"Error initializing database: {e}")

def save_user_token(user_id, token_json):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO google_tokens (user_id, token_json) VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET token_json = %s;
    """, (user_id, token_json, token_json))
    conn.commit()
    cur.close()
    conn.close()
    logging.info(f"Successfully saved token for user {user_id}")

# ==============================================================================
# 3. GOOGLE CALENDAR SETUP (MULTI-USER WITH POSTGRESQL)
# ==============================================================================
SCOPES = ['https://www.googleapis.com/auth/calendar']

# Fixed the typo in the function name here
def get_calendar_service(user_id):
    conn = psycopg2.connect(DATABASE_URL)
    cur = conn.cursor()
    cur.execute("SELECT token_json FROM google_tokens WHERE user_id = %s;", (user_id,))
    result = cur.fetchone()
    
    if not result:
        cur.close()
        conn.close()
        return None

    creds_json = result[0]
    creds_info = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(creds_info, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                save_user_token(user_id, creds.to_json())
            except Exception as e:
                logging.error(f"Could not refresh token for user {user_id}: {e}")
                cur.execute("DELETE FROM google_tokens WHERE user_id = %s;", (user_id,))
                conn.commit()
                return None
        else:
             return None # Needs re-authentication
    
    cur.close()
    conn.close()
    service = build('calendar', 'v3', credentials=creds)
    return service

# ==============================================================================
# 4. WEB ROUTES FOR OAUTH
# ==============================================================================
@app.route('/')
def home():
    return "Bot is alive!"
    
@app.route('/connect_google')
def connect_google():
    user_id = request.args.get('user_id')
    if not user_id:
        return "<h1>Error: Missing user ID.</h1><p>Please try the `/connect` command again from Discord.</p>", 400
    
    session['user_id'] = user_id
    redirect_uri = f"{RENDER_EXTERNAL_URL}{url_for('oauth2callback')}"

    flow = Flow.from_client_secrets_file(
        'credentials.json',
        scopes=SCOPES,
        redirect_uri=redirect_uri
    )
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )
    session['state'] = state
    return redirect(authorization_url)

@app.route('/oauth2callback')
def oauth2callback():
    try:
        state = session['state']
        redirect_uri = f"{RENDER_EXTERNAL_URL}{url_for('oauth2callback')}"

        flow = Flow.from_client_secrets_file(
            'credentials.json',
            scopes=SCOPES,
            state=state,
            redirect_uri=redirect_uri
        )
        authorization_response = request.url
        flow.fetch_token(authorization_response=authorization_response)
        
        credentials = flow.credentials
        user_id = session.get('user_id')

        if not user_id:
            return "<h1>Authentication failed: User session not found.</h1>", 400

        save_user_token(user_id, credentials.to_json())
        
        return "<h1>Authentication successful!</h1><p>You can now close this window and use the `/events` command in Discord.</p>"
    except Exception as e:
        logging.error(f"An error occurred in the OAuth callback:\n{traceback.format_exc()}")
        return "<h1>An error occurred during authentication.</h1><p>Please try again.</p>", 500

# ==============================================================================
# 5. BOT EVENTS & SLASH COMMANDS
# ==============================================================================
@bot.event
async def on_ready():
    logging.info(f'Success! We have logged in as {bot.user}')
    init_db()

@bot.tree.command(name="connect", description="Connect your Google Calendar to the bot.")
async def connect(interaction: discord.Interaction):
    auth_url = f"{RENDER_EXTERNAL_URL}/connect_google?user_id={interaction.user.id}"
    try:
        await interaction.user.send(f"Please use this link to connect your Google Calendar: {auth_url}")
        await interaction.response.send_message("I've sent you a private message with your connection link.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("I couldn't send you a DM. Please check your server privacy settings.", ephemeral=True)

@bot.tree.command(name="events", description="Shows your next 10 upcoming Google Calendar events.")
async def events(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    
    # Using the corrected function name here
    service = get_calendar_service(interaction.user.id)
    
    if not service:
        await interaction.followup.send(f"You haven't connected your Google Calendar yet! Please use the `/connect` command.")
        return

    try:
        now = datetime.datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(calendarId='primary', timeMin=now,
                                            maxResults=10, singleEvents=True,
                                            orderBy='startTime').execute()
        events = events_result.get('items', [])
    except Exception as e:
        await interaction.followup.send(f"An error occurred while trying to fetch your calendar events: {e}")
        return

    if not events:
        await interaction.followup.send('You have no upcoming events found.')
        return
    
    response = "📅 **Your upcoming events:**\n\n"
    for event in events:
        start = event['start'].get('dateTime', event['start'].get('date'))
        if 'T' in start:
            start_formatted = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%A, %B %d at %I:%M %p')
        else:
            start_formatted = datetime.datetime.fromisoformat(start).strftime('%A, %B %d (All Day)')
        response += f"**- {event['summary']}** on {start_formatted}\n"
    
    await interaction.followup.send(response)

@bot.command()
@commands.is_owner()
async def sync(ctx):
    await bot.tree.sync()
    await ctx.send("Commands synced successfully!")

# ==============================================================================
# 6. START THE BOT IN A BACKGROUND THREAD
# ==============================================================================
def run_bot():
    bot.run(TOKEN)

bot_thread = Thread(target=run_bot)
bot_thread.start()

