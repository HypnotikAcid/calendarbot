import discord
import os
from flask import Flask
from threading import Thread
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
import psycopg2
from dotenv import load_dotenv

# ==============================================================================
# 1. BOT & SERVER SETUP (RENDER)
# ==============================================================================
load_dotenv() # Load environment variables from .env file or Render's environment

# Securely load secrets from Render's environment variables
TOKEN = os.environ.get('DISCORD_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')

# Check if the secrets are loaded correctly
if not TOKEN:
    raise ValueError("CRITICAL ERROR: DISCORD_TOKEN not found in environment variables. Go to the 'Environment' tab in Render and add it.")
if not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: DATABASE_URL not found in environment variables. Go to the 'Environment' tab in Render and add it.")

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

app = Flask('')
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
        # Create table if it doesn't exist
        cur.execute("""
            CREATE TABLE IF NOT EXISTS google_tokens (
                user_id BIGINT PRIMARY KEY,
                token_json TEXT NOT NULL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully.")
    except Exception as e:
        print(f"Error initializing database: {e}")

# ==============================================================================
# 3. GOOGLE CALENDAR SETUP (MULTI-USER WITH POSTGRESQL)
# ==============================================================================
SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

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
    # Important: Convert the JSON string from the DB into a dictionary
    import json
    creds_info = json.loads(creds_json)
    creds = Credentials.from_authorized_user_info(creds_info, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save the refreshed credentials back to the database
                cur.execute(
                    "UPDATE google_tokens SET token_json = %s WHERE user_id = %s;",
                    (creds.to_json(), user_id)
                )
                conn.commit()
            except Exception as e:
                print(f"Could not refresh token for user {user_id}: {e}")
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
# 4. BOT EVENTS
# ==============================================================================
@client.event
async def on_ready():
    print(f'Success! We have logged in as {client.user}')
    init_db() # Initialize the database when the bot starts

@client.event
async def on_message(message):
    if message.author == client.user:
        return

    if message.content.startswith('!events'):
        user_id = message.author.id
        await message.channel.send("Fetching your upcoming events...")
        
        service = get_calendar_service(user_id)
        
        if not service:
            await message.channel.send("You haven't connected your Google Calendar yet! Please use a command to connect.")
            return

        # (Event fetching and formatting logic remains the same as before)
        try:
            now = datetime.datetime.utcnow().isoformat() + 'Z'
            events_result = service.events().list(calendarId='primary', timeMin=now,
                                                maxResults=10, singleEvents=True,
                                                orderBy='startTime').execute()
            events = events_result.get('items', [])
        except Exception as e:
            await message.channel.send("An error occurred while trying to fetch your calendar events.")
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
# 5. START THE BOT
# ==============================================================================
keep_alive()
client.run(TOKEN)