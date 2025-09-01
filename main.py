import discord
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
from dotenv import load_dotenv
import logging
import traceback

# ==============================================================================
# 1. BOT & SERVER SETUP (RENDER)
# ==============================================================================
# Configure logging to ensure we see all messages
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

load_dotenv()

# Securely load secrets from Render's environment variables
TOKEN = os.environ.get('DISCORD_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
FLASK_SECRET_KEY = os.environ.get('FLASK_SECRET_KEY')
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL')

# Check if the secrets are loaded correctly
if not TOKEN:
    raise ValueError("CRITICAL ERROR: DISCORD_TOKEN not found in environment variables.")
if not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: DATABASE_URL not found in environment variables.")
if not FLASK_SECRET_KEY:
    raise ValueError("CRITICAL ERROR: FLASK_SECRET_KEY not found in environment variables.")
if not RENDER_EXTERNAL_URL:
    logging.warning("RENDER_EXTERNAL_URL not found. Connection links may not work.")


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

app = Flask('')
app.secret_key = FLASK_SECRET_KEY

@app.route('/')
def home():
    return "Bot is alive!"
def run():
    app.run(host='0.0.0.0', port=8080)
def keep_alive():
    t = Thread(target=run)
    t.start()

# ==============================================================================
# 2. DATABASE SETUP (POSTGRESQL)
# ==============================================================================
def init_db():
    """Initializes the database and creates the tokens table if it doesn't exist."""
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
    """Saves or updates a user's Google token in the database."""
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

def get_calendar_service(user_id):
    """Authenticates with Google for a specific user using tokens from PostgreSQL."""
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
                cur.close()
                conn.close()
                return None
    
    cur.close()
    conn.close()
    service = build('calendar', 'v3', credentials=creds)
    return service

# ==============================================================================
# 4. WEB ROUTES FOR OAUTH
# ==============================================================================
@app.route('/connect_google')
def connect_google():
    """Initiates the Google OAuth2 flow."""
    user_id = request.args.get('user_id')
    if not user_id:
        return "<h1>Error: Missing user ID.</h1><p>Please try the `!connect` command again from Discord.</p>", 400
    
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
    """Callback route for Google OAuth2. Finishes the process."""
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
        
        return "<h1>Authentication successful!</h1><p>You can now close this window and use the `!events` command in Discord.</p>"
    except Exception as e:
        # This will now log the FULL traceback to the Render logs
        logging.error(f"An error occurred in the OAuth callback:\n{traceback.format_exc()}")
        return "<h1>An error occurred during authentication.</h1><p>Please try again.</p>", 500

# ==============================================================================
# 5. BOT EVENTS
# ==============================================================================
@client.event
async def on_ready():
    logging.info(f'Success! We have logged in as {client.user}')
    init_db()

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith('!connect'):
        if not RENDER_EXTERNAL_URL:
            await message.channel.send("Sorry, the connection service is not configured correctly by the bot admin.")
            return

        auth_url = f"{RENDER_EXTERNAL_URL}/connect_google?user_id={message.author.id}"
        try:
            await message.author.send(f"Please use this link to connect your Google Calendar: {auth_url}")
            await message.channel.send(f"{message.author.mention}, I've sent you a private message with your connection link.")
        except discord.Forbidden:
            await message.channel.send(f"{message.author.mention}, I couldn't send you a DM. Please check your server privacy settings.")
        return

    if message.content.startswith('!events'):
        user_id = message.author.id
        await message.channel.send("Fetching your upcoming events...")
        
        service = get_calendar_service(user_id)
        
        if not service:
            await message.channel.send(f"You haven't connected your Google Calendar yet! Please use the `!connect` command.")
            return

        try:
            now = datetime.datetime.utcnow().isoformat() + 'Z'
            events_result = service.events().list(calendarId='primary', timeMin=now,
                                                maxResults=10, singleEvents=True,
                                                orderBy='startTime').execute()
            events = events_result.get('items', [])
        except Exception as e:
            await message.channel.send(f"An error occurred while trying to fetch your calendar events: {e}")
            return

        if not events:
            await message.channel.send('You have no upcoming events found.')
            return
        
        response = f"📅 **Upcoming events for {message.author.mention}:**\n\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            if 'T' in start:
                start_formatted = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%A, %B %d at %I:%M %p')
            else:
                start_formatted = datetime.datetime.fromisoformat(start).strftime('%A, %B %d (All Day)')
            response += f"**- {event['summary']}** on {start_formatted}\n"
        
        await message.channel.send(response)

# ==============================================================================
# 6. START THE BOT
# ==============================================================================
keep_alive()
client.run(TOKEN)

