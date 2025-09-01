import discord
import os
from flask import Flask
from threading import Thread
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from replit import db

# ==============================================================================
# 1. BOT & SERVER SETUP (REPLIT)
# ==============================================================================

# Load your Discord Bot Token from Replit's Secrets.
TOKEN = os.environ['DISCORD_TOKEN']

# These "intents" are permissions for your bot.
intents = discord.Intents.default()
intents.message_content = True

# This creates the main connection to Discord.
client = discord.Client(intents=intents)

# This is the keep-alive web server for Replit.
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
# 2. GOOGLE CALENDAR SETUP (NOW MULTI-USER)
# ==============================================================================

SCOPES = ['https://www.googleapis.com/auth/calendar.readonly']

def get_calendar_service(user_id):
    """
    Authenticates with Google for a SPECIFIC USER and returns a service object.
    It now uses the Replit DB to find the user's token.
    """
    user_token_key = f"google_token_{user_id}"

    # Check if the user's token exists in our database
    if user_token_key not in db:
        print(f"DEBUG: No token found for user {user_id}")
        return None # No token for this user

    # Load the credentials from the database
    creds_json = db[user_token_key]
    creds = Credentials.from_authorized_user_info(creds_json, SCOPES)
    
    # If credentials are not valid or expired, refresh them
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save the refreshed credentials back to the database
                db[user_token_key] = creds.to_json()
                print(f"DEBUG: Refreshed token for user {user_id}")
            except Exception as e:
                print(f"DEBUG: Could not refresh token for user {user_id}: {e}")
                del db[user_token_key] # Delete the bad token
                return None
        else:
            # This should ideally not happen if the auth flow is correct
            return None

    service = build('calendar', 'v3', credentials=creds)
    return service

# ==============================================================================
# 3. BOT EVENTS
# ==============================================================================

@client.event
async def on_ready():
  print(f'Success! We have logged in as {client.user}')

@client.event
async def on_message(message):
    # Ignore messages from the bot itself
    if message.author == client.user:
        return

    # Test command to fetch and display upcoming events
    if message.content.startswith('!events'):
        # Get the Discord ID of the user who sent the message
        user_id = message.author.id
        await message.channel.send("Fetching your upcoming events...")
        
        print(f"DEBUG: Getting calendar service for user {user_id}...")
        service = get_calendar_service(user_id)
        
        # If no service is returned, it means the user hasn't connected their calendar yet.
        if not service:
            await message.channel.send("You haven't connected your Google Calendar yet! Please use the `/connect_calendar` command.")
            print(f"DEBUG: User {user_id} is not authenticated.")
            return
        
        print(f"DEBUG: Successfully got calendar service for user {user_id}.")

        # ... (The rest of the event fetching logic is the same as before) ...
        try:
            now = datetime.datetime.utcnow().isoformat() + 'Z'
            events_result = service.events().list(calendarId='primary', timeMin=now,
                                                maxResults=10, singleEvents=True,
                                                orderBy='startTime').execute()
            events = events_result.get('items', [])
            print(f"DEBUG: Found {len(events)} events for user {user_id}.")
        except Exception as e:
            print(f"DEBUG: An error occurred while fetching events for {user_id}: {e}")
            await message.channel.send("An error occurred while trying to fetch your calendar events.")
            return

        if not events:
            await message.channel.send('You have no upcoming events found.')
            return
        
        response = f"📅 **Here are the next 10 upcoming events for {message.author.mention}:**\n\n"
        for event in events:
            start = event['start'].get('dateTime', event['start'].get('date'))
            try:
                if 'T' in start:
                    start_formatted = datetime.datetime.fromisoformat(start.replace('Z', '+00:00')).strftime('%A, %B %d at %I:%M %p')
                else:
                    start_formatted = datetime.datetime.fromisoformat(start).strftime('%A, %B %d (All Day)')
                response += f"**- {event['summary']}** on {start_formatted}\n"
            except Exception as e:
                print(f"DEBUG: Error formatting date for event '{event['summary']}': {e}")
        
        await message.channel.send(response)

# ==============================================================================
# 4. START THE BOT
# ==============================================================================

# This turns on the keep-alive server.
keep_alive()

# This is the last line and it runs the bot.
client.run(TOKEN)

