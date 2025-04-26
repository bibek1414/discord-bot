import discord
from discord.ext import commands
import os
import re
import random
import time
import asyncio
import datetime
from dotenv import load_dotenv
import logging
import google.generativeai as genai
import threading
import http.server
import socketserver
import json
from typing import Optional

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('discord_bot')

# Load environment variables from .env file
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')  # Add your Google AI API key to .env file
PORT = int(os.environ.get('PORT', 8080))  # Get port from environment or use 8080 as default

# Configure Google AI Studio
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    # Try to use recommended model
    try:
        # Explicitly use gemini-1.5-flash as recommended in the error message
        model_name = "gemini-1.5-flash"
        model = genai.GenerativeModel(model_name)
        logger.info(f"Using model: {model_name}")
    except Exception as e:
        logger.error(f"Error initializing Google AI with specific model: {e}")
        # Fall back to dynamically finding an available model
        try:
            models = genai.list_models()
            # Filter out vision models and deprecated models
            text_model_names = [
                m.name for m in models 
                if 'generateContent' in m.supported_generation_methods 
                and 'vision' not in m.name.lower()
            ]
            
            if text_model_names:
                model_name = text_model_names[0]
                model = genai.GenerativeModel(model_name)
                logger.info(f"Fallback to model: {model_name}")
            else:
                logger.warning("No suitable text models found")
                model = None
        except Exception as e2:
            logger.error(f"Error finding alternative model: {e2}")
            model = None
else:
    model = None

# Define intents
intents = discord.Intents.default()
intents.message_content = True  # Need this to read message content
intents.members = True  # Need this for user commands

# Create bot instance with a command prefix and intents
bot = commands.Bot(command_prefix='!', intents=intents)

# Dictionary of keywords and responses
KEYWORDS = {
    'hello': 'Hello there!',
    'project': 'Need help with your project?',
    'python': 'Python is a great programming language!',
    'discord': 'Discord bots are fun to make!'
}

# File paths
POLLS_FILE = "polls.json"
REMINDERS_FILE = "reminders.json"
TICKETS_FILE = "tickets.json"

# Load data from files if they exist, otherwise create empty ones
def load_data(file_path, default=None):
    try:
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
        else:
            return default if default is not None else {}
    except Exception as e:
        logger.error(f"Error loading data from {file_path}: {e}")
        return default if default is not None else {}

def save_data(data, file_path):
    try:
        with open(file_path, 'w') as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Error saving data to {file_path}: {e}")

# Initialize data
polls = load_data(POLLS_FILE, {})
reminders = load_data(REMINDERS_FILE, [])
tickets = load_data(TICKETS_FILE, {})

# Simple HTTP request handler for the web server
class SimpleHTTPRequestHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        self.wfile.write(b'Discord bot is running!')
    
    def log_message(self, format, *args):
        # Suppress logging of HTTP requests to avoid cluttering the console
        return

def start_http_server():
    """Start an HTTP server to keep Render happy"""
    with socketserver.TCPServer(("", PORT), SimpleHTTPRequestHandler) as httpd:
        logger.info(f"HTTP server started on port {PORT}")
        httpd.serve_forever()

# Background tasks
async def check_reminders():
    """Check for due reminders and send notifications"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        current_time = datetime.datetime.now().timestamp()
        reminders_due = [r for r in reminders if r['due_time'] <= current_time]
        
        for reminder in reminders_due:
            try:
                channel = bot.get_channel(reminder['channel_id'])
                user = await bot.fetch_user(reminder['user_id'])
                
                if channel and user:
                    await channel.send(f"{user.mention} Reminder: {reminder['message']}")
                    
                # Remove the reminder from the list
                reminders.remove(reminder)
                
                # Save updated reminders
                save_data(reminders, REMINDERS_FILE)
            except Exception as e:
                logger.error(f"Error processing reminder: {e}")
        
        # Check every 30 seconds
        await asyncio.sleep(30)

async def check_ticket_timeouts():
    """Check for inactive tickets and close them"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        current_time = datetime.datetime.now().timestamp()
        for guild_id, ticket_data in tickets.items():
            for ticket_id, ticket in ticket_data.items():
                if ticket['status'] == 'open' and current_time - ticket['last_activity'] > 86400:  # 24 hours
                    try:
                        guild = bot.get_guild(int(guild_id))
                        channel = guild.get_channel(int(ticket_id))
                        
                        if channel:
                            # Create a transcript before closing
                            transcript = []
                            async for message in channel.history(limit=None, oldest_first=True):
                                transcript.append(f"{message.author.display_name}: {message.content}")
                            
                            transcript_channel = discord.utils.get(guild.text_channels, name='ticket-transcripts')
                            if not transcript_channel:
                                transcript_channel = await guild.create_text_channel('ticket-transcripts')
                            
                            transcript_text = "\n".join(transcript)
                            if len(transcript_text) > 2000:
                                # Split transcript into multiple messages if too long
                                chunks = [transcript_text[i:i+2000] for i in range(0, len(transcript_text), 2000)]
                                for chunk in chunks:
                                    await transcript_channel.send(f"**Transcript for Ticket {ticket_id}**\n{chunk}")
                            else:
                                await transcript_channel.send(f"**Transcript for Ticket {ticket_id}**\n{transcript_text}")
                            
                            # Close the ticket
                            await channel.delete(reason="Ticket closed due to inactivity")
                            ticket['status'] = 'closed'
                            save_data(tickets, TICKETS_FILE)
                            
                            # Notify user
                            user = await bot.fetch_user(ticket['user_id'])
                            await user.send(f"Your ticket #{ticket_id} was closed due to inactivity.")
                    except Exception as e:
                        logger.error(f"Error closing inactive ticket: {e}")
        
        # Check every hour
        await asyncio.sleep(3600)

@bot.event
async def on_ready():
    """Event triggered when the bot is ready and connected to Discord"""
    logger.info(f'{bot.user.name} has connected to Discord!')
    logger.info(f'Bot is connected to {len(bot.guilds)} guilds')
    
    # Setting bot status
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.listening, 
        name="!help for commands"
    ))
    
    # Start background tasks
    bot.loop.create_task(check_reminders())
    bot.loop.create_task(check_ticket_timeouts())

# Basic Commands
@bot.command(name='hello')
async def hello(ctx):
    """Greets the user with a friendly message"""
    await ctx.send(f'Hello, {ctx.author.display_name}!')

@bot.command(name='ping')
async def ping(ctx):
    """Checks the bot's latency"""
    start_time = time.time()
    message = await ctx.send("Pinging...")
    end_time = time.time()
    
    # Calculate round-trip and API latency
    round_trip = (end_time - start_time) * 1000
    api_latency = bot.latency * 1000
    
    await message.edit(content=f"Pong! 🏓\nBot Latency: {round_trip:.2f}ms\nAPI Latency: {api_latency:.2f}ms")

@bot.command(name='info')
async def info(ctx):
    """Provides information about the bot"""
    info_embed = discord.Embed(
        title="Bot Information",
        description="I'm a helpful Discord bot created with Discord.py!",
        color=discord.Color.blue()
    )
    
    info_embed.add_field(name="Creator", value="Your Name", inline=True)
    info_embed.add_field(name="Version", value="1.0", inline=True)
    info_embed.add_field(name="Library", value=f"discord.py {discord.__version__}", inline=True)
    info_embed.add_field(name="Commands", value="Use `!help` for a list of commands", inline=False)
    
    if model:
        info_embed.add_field(name="AI Integration", value=f"Google AI ({model_name})", inline=False)
    
    info_embed.set_footer(text=f"Running since {bot.user.created_at.strftime('%Y-%m-%d')}")
    
    await ctx.send(embed=info_embed)

@bot.command(name='serverinfo')
async def server_info(ctx):
    """Shows information about the server"""
    guild = ctx.guild
    
    # Getting counts with type filters
    text_channels = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
    voice_channels = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
    categories = len(guild.categories)
    
    # Create a nice embed
    embed = discord.Embed(
        title=f"{guild.name} Server Information",
        description=guild.description or "No description",
        color=discord.Color.green()
    )
    
    # Set the server icon as the embed thumbnail
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    
    # Add server information
    embed.add_field(name="Owner", value=guild.owner.mention, inline=True)
    embed.add_field(name="Created On", value=guild.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="Server ID", value=guild.id, inline=True)
    
    # Add member information
    embed.add_field(name="Members", value=guild.member_count, inline=True)
    embed.add_field(name="Roles", value=len(guild.roles), inline=True)
    embed.add_field(name="Emojis", value=len(guild.emojis), inline=True)
    
    # Add channel information
    embed.add_field(name="Categories", value=categories, inline=True)
    embed.add_field(name="Text Channels", value=text_channels, inline=True)
    embed.add_field(name="Voice Channels", value=voice_channels, inline=True)
    
    # Add server boost information
    embed.add_field(name="Boost Level", value=guild.premium_tier, inline=True)
    embed.add_field(name="Boosts", value=guild.premium_subscription_count, inline=True)
    
    # Set footer with timestamp
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await ctx.send(embed=embed)

@bot.command(name='userinfo')
async def user_info(ctx, member: discord.Member = None):
    """Shows information about a user"""
    if member is None:
        member = ctx.author
        
    roles = [role.mention for role in member.roles if role.name != "@everyone"]
    
    # Create embed
    embed = discord.Embed(
        title=f"User Information - {member.display_name}",
        color=member.color
    )
    
    # Basic user info
    embed.add_field(name="Username", value=str(member), inline=True)
    embed.add_field(name="User ID", value=member.id, inline=True)
    embed.add_field(name="Created On", value=member.created_at.strftime("%b %d, %Y"), inline=True)
    embed.add_field(name="Joined Server On", value=member.joined_at.strftime("%b %d, %Y"), inline=True)
    
    # Add roles if user has any
    if roles:
        embed.add_field(name=f"Roles [{len(roles)}]", value=" ".join(roles[:10]), inline=False)
        if len(roles) > 10:
            embed.add_field(name="Note", value="Only showing first 10 roles", inline=False)
    else:
        embed.add_field(name="Roles", value="No roles", inline=False)
    
    # Set the user's avatar as the embed thumbnail
    if member.avatar:
        embed.set_thumbnail(url=member.avatar.url)
    
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    embed.timestamp = datetime.datetime.utcnow()
    
    await ctx.send(embed=embed)

# Moderation Commands
@bot.command(name='clear')
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: int = 5):
    """Clears a specified number of messages"""
    if amount <= 0:
        await ctx.send("Please specify a positive number of messages to delete.")
        return
    
    # Add a limit to prevent accidental mass deletions
    if amount > 100:
        await ctx.send("You can only delete up to 100 messages at once.")
        return
        
    try:
        # Delete the command message first
        await ctx.message.delete()
        # Then delete the specified number of messages
        deleted = await ctx.channel.purge(limit=amount)
        
        # Send confirmation message
        confirm_message = await ctx.send(f"Deleted {len(deleted)} messages.")
        # Delete the confirmation message after a few seconds
        await asyncio.sleep(3)
        await confirm_message.delete()
    except discord.Forbidden:
        await ctx.send("I don't have permission to delete messages.")
    except Exception as e:
        logger.error(f"Error clearing messages: {e}")
        await ctx.send(f"An error occurred while clearing messages: {str(e)}")

@clear.error
async def clear_error(ctx, error):
    """Handle errors for the clear command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Please provide a valid number of messages to delete.")
    else:
        logger.error(f"Clear command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

@bot.command(name='kick')
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason="No reason provided"):
    """Kicks a member from the server"""
    try:
        await member.kick(reason=reason)
        await ctx.send(f"{member.mention} has been kicked.\nReason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to kick members.")
    except Exception as e:
        logger.error(f"Error kicking member: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

@kick.error
async def kick_error(ctx, error):
    """Handle errors for the kick command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to kick members.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Member not found. Please @mention the member or use their ID.")
    else:
        logger.error(f"Kick command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

@bot.command(name='ban')
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason="No reason provided"):
    """Bans a member from the server"""
    try:
        await member.ban(reason=reason)
        await ctx.send(f"{member.mention} has been banned.\nReason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to ban members.")
    except Exception as e:
        logger.error(f"Error banning member: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

@ban.error
async def ban_error(ctx, error):
    """Handle errors for the ban command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to ban members.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Member not found. Please @mention the member or use their ID.")
    else:
        logger.error(f"Ban command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

@bot.command(name='unban')
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, member):
    """Unbans a member from the server"""
    try:
        banned_users = [entry async for entry in ctx.guild.bans()]
        member_name, member_discriminator = member.split('#', 1)
        
        for ban_entry in banned_users:
            user = ban_entry.user
            
            # Check if this is the user we want to unban
            if (user.name, user.discriminator) == (member_name, member_discriminator):
                await ctx.guild.unban(user)
                await ctx.send(f"{user.mention} has been unbanned.")
                return
                
        await ctx.send(f"Could not find {member} in the ban list.")
    except ValueError:
        await ctx.send("Please specify the member as `username#discriminator`.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to unban members.")
    except Exception as e:
        logger.error(f"Error unbanning member: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

@unban.error
async def unban_error(ctx, error):
    """Handle errors for the unban command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to unban members.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("Please specify the member to unban as `username#discriminator`.")
    else:
        logger.error(f"Unban command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

@bot.command(name='mute')
@commands.has_permissions(manage_roles=True)
async def mute(ctx, member: discord.Member, *, reason="No reason provided"):
    """Mutes a member in the server"""
    # Check for mute role or create one
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    
    if not mute_role:
        try:
            # Create mute role if it doesn't exist
            mute_role = await ctx.guild.create_role(name="Muted", reason="Created for muting members")
            
            # Set permissions for the mute role
            for channel in ctx.guild.channels:
                await channel.set_permissions(mute_role, speak=False, send_messages=False, 
                                              add_reactions=False)
                
            await ctx.send("Created Muted role.")
        except discord.Forbidden:
            await ctx.send("I don't have permission to create roles.")
            return
        except Exception as e:
            logger.error(f"Error creating mute role: {e}")
            await ctx.send(f"An error occurred while creating the Muted role: {str(e)}")
            return
    
    # Add the mute role to the member
    try:
        if mute_role in member.roles:
            await ctx.send(f"{member.mention} is already muted.")
            return
            
        await member.add_roles(mute_role, reason=reason)
        await ctx.send(f"{member.mention} has been muted.\nReason: {reason}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to manage roles.")
    except Exception as e:
        logger.error(f"Error muting member: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

@mute.error
async def mute_error(ctx, error):
    """Handle errors for the mute command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to mute members.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Member not found. Please @mention the member or use their ID.")
    else:
        logger.error(f"Mute command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

@bot.command(name='unmute')
@commands.has_permissions(manage_roles=True)
async def unmute(ctx, member: discord.Member):
    """Unmutes a member in the server"""
    mute_role = discord.utils.get(ctx.guild.roles, name="Muted")
    
    if not mute_role:
        await ctx.send("There is no Muted role set up.")
        return
        
    try:
        if mute_role not in member.roles:
            await ctx.send(f"{member.mention} is not muted.")
            return
            
        await member.remove_roles(mute_role)
        await ctx.send(f"{member.mention} has been unmuted.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to manage roles.")
    except Exception as e:
        logger.error(f"Error unmuting member: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

@unmute.error
async def unmute_error(ctx, error):
    """Handle errors for the unmute command"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to unmute members.")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.send("Member not found. Please @mention the member or use their ID.")
    else:
        logger.error(f"Unmute command error: {error}")
        await ctx.send(f"An error occurred: {str(error)}")

# Fun Commands
@bot.command(name='roll')
async def roll(ctx, dice: str = "1d6"):
    """Rolls dice in NdN format"""
    try:
        rolls, limit = map(int, dice.split('d'))
    except Exception:
        await ctx.send("Format has to be in NdN format, e.g. 3d6")
        return
        
    if rolls > 100:
        await ctx.send("You can roll a maximum of 100 dice at once.")
        return
        
    if limit > 1000:
        await ctx.send("Dice cannot have more than 1000 sides.")
        return
        
    results = [random.randint(1, limit) for _ in range(rolls)]
    total = sum(results)
    
    # For a single die, just show the result
    if rolls == 1:
        await ctx.send(f"🎲 You rolled a {total}")
    else:
        await ctx.send(f"🎲 You rolled {rolls}d{limit} and got: {', '.join(map(str, results))}\nTotal: {total}")

@bot.command(name='8ball')
async def eight_ball(ctx, *, question=None):
    """Ask the magic 8ball a question"""
    if question is None:
        await ctx.send("Please ask a question.")
        return
        
    responses = [
        "It is certain.",
        "It is decidedly so.",
        "Without a doubt.",
        "Yes - definitely.",
        "You may rely on it.",
        "As I see it, yes.",
        "Most likely.",
        "Outlook good.",
        "Yes.",
        "Signs point to yes.",
        "Reply hazy, try again.",
        "Ask again later.",
        "Better not tell you now.",
        "Cannot predict now.",
        "Concentrate and ask again.",
        "Don't count on it.",
        "My reply is no.",
        "My sources say no.",
        "Outlook not so good.",
        "Very doubtful."
    ]
    
    response = random.choice(responses)
    
    # Create a nice embed
    embed = discord.Embed(title="🎱 Magic 8-Ball", color=discord.Color.purple())
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=response, inline=False)
    embed.set_footer(text=f"Asked by {ctx.author.display_name}")
    
    await ctx.send(embed=embed)

@bot.command(name='flip')
async def flip(ctx):
    """Flips a coin"""
    result = random.choice(["Heads", "Tails"])
    
    # Create a nice embed
    embed = discord.Embed(title="Coin Flip", color=discord.Color.gold())
    embed.description = f"The coin landed on: **{result}**"
    
    if result == "Heads":
        embed.set_thumbnail(url="https://i.imgur.com/HavOS7J.png")
    else:
        embed.set_thumbnail(url="https://i.imgur.com/9oQmdnB.png")
        
    embed.set_footer(text=f"Flipped by {ctx.author.display_name}")
    
    await ctx.send(embed=embed)

@bot.command(name='choose')
async def choose(ctx, *, options=None):
    """Choose randomly from a list of options"""
    if options is None:
        await ctx.send("Please provide options separated by commas or spaces.")
        return
        
    # First try splitting by commas, then by spaces if only one option was found
    choices = [opt.strip() for opt in options.split(',')]
    if len(choices) <= 1:
        choices = [opt.strip() for opt in options.split()]
        
    if len(choices) <= 1:
        await ctx.send("Please provide at least 2 options to choose from.")
        return
        
    choice = random.choice(choices)
    await ctx.send(f"🤔 I choose: **{choice}**")

# Poll Commands
@bot.command(name='poll')
async def create_poll(ctx, *, question=None):
    """Creates a simple reaction poll"""
    if question is None:
        await ctx.send("Please provide a question for the poll.")
        return
        
    # Create a poll embed
    embed = discord.Embed(
        title="📊 Poll",
        description=question,
        color=discord.Color.blue()
    )
    
    embed.set_footer(text=f"Poll created by {ctx.author.display_name}")
    
    # Send the poll and add reactions
    poll_message = await ctx.send(embed=embed)
    await poll_message.add_reaction("👍")  # Yes/Agree
    await poll_message.add_reaction("👎")  # No/Disagree
    await poll_message.add_reaction("🤷")  # Not sure/Neutral

@bot.command(name='advpoll')
@commands.has_permissions(manage_messages=True)
async def advanced_poll(ctx, title, *options):
    """Creates an advanced poll with multiple options"""
    if len(options) < 2:
        await ctx.send("Please provide at least 2 options for the poll.")
        return
        
    if len(options) > 10:
        await ctx.send("You can only have up to 10 options in a poll.")
        return
        
    # Emoji options (numbers 1-10)
    emoji_options = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    
    # Create the poll embed
    embed = discord.Embed(
        title=f"📊 {title}",
        description="React with the corresponding emoji to vote!",
        color=discord.Color.blue()
    )
    
    # Add each option to the embed
    for i, option in enumerate(options):
        embed.add_field(name=f"{emoji_options[i]} Option {i+1}", value=option, inline=False)
    
    embed.set_footer(text=f"Poll created by {ctx.author.display_name}")
    
    # Send the poll
    poll_message = await ctx.send(embed=embed)
    
    # Add reactions for each option
    for i in range(len(options)):
        await poll_message.add_reaction(emoji_options[i])
    
    # Save poll information
    poll_id = str(poll_message.id)
    polls[poll_id] = {
        "title": title,
        "options": list(options),
        "created_by": ctx.author.id,
        "channel_id": ctx.channel.id,
        "message_id": poll_message.id,
        "emoji_options": emoji_options[:len(options)]
    }
    
    save_data(polls, POLLS_FILE)

@bot.command(name='endpoll')
@commands.has_permissions(manage_messages=True)
async def end_poll(ctx, message_id=None):
    """Ends a poll and displays the results"""
    if message_id is None:
        await ctx.send("Please provide the message ID of the poll to end.")
        return
        
    # Check if the poll exists
    if message_id not in polls:
        await ctx.send("That poll doesn't exist or has already ended.")
        return
        
    poll = polls[message_id]
    
    try:
        # Get the poll message
        channel = bot.get_channel(poll["channel_id"])
        poll_message = await channel.fetch_message(poll["message_id"])
        
        # Count the reactions
        results = []
        for i, emoji in enumerate(poll["emoji_options"]):
            reaction = discord.utils.get(poll_message.reactions, emoji=emoji)
            count = reaction.count - 1  # Subtract 1 to exclude the bot's reaction
            results.append((poll["options"][i], count))
        
        # Sort results by votes (descending)
        results.sort(key=lambda x: x[1], reverse=True)
        
        # Create results embed
        embed = discord.Embed(
            title=f"📊 Poll Results: {poll['title']}",
            color=discord.Color.green()
        )
        
        # Add results to embed
        for i, (option, votes) in enumerate(results):
            embed.add_field(name=f"{i+1}. {option}", value=f"{votes} vote(s)", inline=False)
        
        embed.set_footer(text="Poll ended")
        
        await ctx.send(embed=embed)
        
        # Remove the poll from active polls
        del polls[message_id]
        save_data(polls, POLLS_FILE)
    except Exception as e:
        logger.error(f"Error ending poll: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

# Reminder System
@bot.command(name='remind')
async def set_reminder(ctx, time, *, message=None):
    """Sets a reminder for a specified time"""
    if message is None:
        await ctx.send("Please specify what you want to be reminded about.")
        return
        
    try:
        # Parse the time argument
        time_value = int(time[:-1])
        time_unit = time[-1].lower()
        
        # Convert to seconds
        seconds = 0
        if time_unit == "s":
            seconds = time_value
        elif time_unit == "m":
            seconds = time_value * 60
        elif time_unit == "h":
            seconds = time_value * 3600
        elif time_unit == "d":
            seconds = time_value * 86400
        else:
            await ctx.send("Invalid time format. Use `<number><s/m/h/d>`, e.g. 30s, 5m, 2h, 1d")
            return
            
        if seconds <= 0:
            await ctx.send("Time must be positive.")
            return
            
        # Calculate due time
        due_time = datetime.datetime.now().timestamp() + seconds
        
        # Create reminder
        reminder = {
            "user_id": ctx.author.id,
            "channel_id": ctx.channel.id,
            "message": message,
            "due_time": due_time,
            "set_time": datetime.datetime.now().timestamp()
        }
        
        reminders.append(reminder)
        save_data(reminders, REMINDERS_FILE)
        
        # Calculate human-readable time
        time_units = {
            "s": "second(s)",
            "m": "minute(s)",
            "h": "hour(s)",
            "d": "day(s)"
        }
        
        await ctx.send(f"I'll remind you in {time_value} {time_units[time_unit]} about: {message}")
    except ValueError:
        await ctx.send("Invalid time format. Use `<number><s/m/h/d>`, e.g. 30s, 5m, 2h, 1d")
    except Exception as e:
        logger.error(f"Error setting reminder: {e}")
        await ctx.send(f"An error occurred: {str(e)}")

# Ticket System
@bot.command(name='ticket')
async def create_ticket(ctx, *, reason=None):
    """Creates a support ticket"""
    if reason is None:
        await ctx.send("Please provide a reason for creating this ticket.")
        return
    
    guild = ctx.guild
    
    # Check if ticket category exists, create if not
    ticket_category = discord.utils.get(guild.categories, name="Tickets")
    if not ticket_category:
        try:
            ticket_category = await guild.create_category("Tickets")
            # Set permissions for @everyone in the category
            await ticket_category.set_permissions(
                guild.default_role,
                view_channel=False,
                send_messages=False
            )
        except Exception as e:
            logger.error(f"Error creating ticket category: {e}")
            await ctx.send("An error occurred while setting up the ticket system.")
            return
    
    # Create the ticket channel
    ticket_number = len(tickets.get(str(guild.id), {})) + 1
    ticket_name = f"ticket-{ticket_number}"
    
    try:
        ticket_channel = await ticket_category.create_text_channel(ticket_name)
        
        # Set permissions for the ticket creator
        await ticket_channel.set_permissions(
            ctx.author,
            view_channel=True,
            send_messages=True,
            read_message_history=True
        )
        
        # Create the ticket embed
        embed = discord.Embed(
            title=f"Ticket #{ticket_number}",
            description=f"**Reason:** {reason}",
            color=discord.Color.blue()
        )
        embed.add_field(name="Created By", value=ctx.author.mention, inline=True)
        embed.add_field(name="Created At", value=datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), inline=True)
        embed.set_footer(text="Use !closeticket to close this ticket")
        
        # Send the embed and add reactions
        ticket_message = await ticket_channel.send(embed=embed)
        
        # Save ticket information
        if str(guild.id) not in tickets:
            tickets[str(guild.id)] = {}
            
        tickets[str(guild.id)][str(ticket_channel.id)] = {
            "user_id": ctx.author.id,
            "reason": reason,
            "status": "open",
            "created_at": datetime.datetime.now().timestamp(),
            "last_activity": datetime.datetime.now().timestamp()
        }
        save_data(tickets, TICKETS_FILE)
        
        # Notify user
        await ctx.send(f"Ticket created: {ticket_channel.mention}")
        await ticket_channel.send(f"{ctx.author.mention}, support will be with you shortly.")
    except Exception as e:
        logger.error(f"Error creating ticket: {e}")
        await ctx.send("An error occurred while creating your ticket.")

@bot.command(name='closeticket')
async def close_ticket(ctx):
    """Closes a support ticket"""
    if not isinstance(ctx.channel, discord.TextChannel):
        await ctx.send("This command can only be used in a ticket channel.")
        return
    
    guild = ctx.guild
    channel_id = str(ctx.channel.id)
    
    # Check if this is a ticket channel
    if str(guild.id) not in tickets or channel_id not in tickets[str(guild.id)]:
        await ctx.send("This is not a ticket channel.")
        return
        
    ticket = tickets[str(guild.id)][channel_id]
    
    # Check if user has permission to close the ticket
    if ctx.author.id != ticket['user_id'] and not ctx.author.guild_permissions.manage_channels:
        await ctx.send("You don't have permission to close this ticket.")
        return
    
    try:
        # Create a transcript before closing
        transcript = []
        async for message in ctx.channel.history(limit=None, oldest_first=True):
            transcript.append(f"{message.author.display_name}: {message.content}")
        
        transcript_channel = discord.utils.get(guild.text_channels, name='ticket-transcripts')
        if not transcript_channel:
            transcript_channel = await guild.create_text_channel('ticket-transcripts')
        
        transcript_text = "\n".join(transcript)
        if len(transcript_text) > 2000:
            # Split transcript into multiple messages if too long
            chunks = [transcript_text[i:i+2000] for i in range(0, len(transcript_text), 2000)]
            for chunk in chunks:
                await transcript_channel.send(f"**Transcript for Ticket {channel_id}**\n{chunk}")
        else:
            await transcript_channel.send(f"**Transcript for Ticket {channel_id}**\n{transcript_text}")
        
        # Close the ticket
        await ctx.channel.delete(reason="Ticket closed by user")
        
        # Update ticket status
        ticket['status'] = 'closed'
        ticket['closed_by'] = ctx.author.id
        ticket['closed_at'] = datetime.datetime.now().timestamp()
        save_data(tickets, TICKETS_FILE)
        
        # Notify user
        user = await bot.fetch_user(ticket['user_id'])
        await user.send(f"Your ticket #{channel_id} has been closed.")
    except Exception as e:
        logger.error(f"Error closing ticket: {e}")
        await ctx.send(f"An error occurred while closing the ticket: {str(e)}")

# AI Commands
@bot.command(name='ask')
async def ask_ai(ctx, *, question=None):
    """Ask the AI a question"""
    if not model:
        await ctx.send("Sorry, AI integration is not available at the moment.")
        return
        
    if question is None:
        await ctx.send("Please provide a question. Example: `!ask what is the capital of France?`")
        return
    
    try:
        # Let the user know the bot is processing
        async with ctx.typing():
            # Generate response from Google AI
            generation_config = {
                "temperature": 0.7,
                "top_p": 0.95,
                "top_k": 40,
                "max_output_tokens": 1024,
            }
            
            response = model.generate_content(question, generation_config=generation_config)
            
            # Handle different response formats depending on library version
            if hasattr(response, 'text'):
                response_text = response.text
            elif hasattr(response, 'parts'):
                response_text = ''.join(part.text for part in response.parts)
            else:
                response_text = str(response)
            
            # Check if the response is too long for Discord
            if len(response_text) > 2000:
                # Split the response into chunks of 1900 characters (leave room for formatting)
                chunks = [response_text[i:i+1900] for i in range(0, len(response_text), 1900)]
                for chunk in chunks:
                    await ctx.send(chunk)
            else:
                # Create an embed for the response
                embed = discord.Embed(
                    title="AI Response",
                    description=response_text,
                    color=discord.Color.blue()
                )
                embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                
                await ctx.send(embed=embed)
    
    except Exception as e:
        logger.error(f"Error with Google AI: {e}")
        await ctx.send(f"Sorry, I encountered an error while processing your question: {str(e)}")

# Event Handlers
@bot.event
async def on_message(message):
    """Event triggered when a message is sent in a channel the bot can see"""
    # Don't respond to our own messages
    if message.author == bot.user:
        return
    
    # Check if the bot was mentioned and it's not a command
    if bot.user.mentioned_in(message) and not message.content.startswith('!'):
        # Get the content without the mention
        content = re.sub(r'<@!?(\d+)>', '', message.content).strip()
        
        # If there's content after removing the mention, treat it as a question
        if content and model:
            ctx = await bot.get_context(message)
            await ask_ai(ctx, question=content)
            return
    
    # Check for keywords in the message
    for keyword, response in KEYWORDS.items():
        # Case-insensitive search for whole words
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, message.content.lower()):
            await message.channel.send(response)
            # Only respond to the first matched keyword to avoid spam
            break
    
    # Update last activity for tickets
    if isinstance(message.channel, discord.TextChannel):
        guild_id = str(message.guild.id)
        channel_id = str(message.channel.id)
        
        if guild_id in tickets and channel_id in tickets[guild_id]:
            tickets[guild_id][channel_id]['last_activity'] = datetime.datetime.now().timestamp()
            save_data(tickets, TICKETS_FILE)
    
    # Process commands (this is necessary when overriding on_message)
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Command not found. Type `!help` to see available commands.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"Missing required argument: {error.param.name}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("Invalid argument provided.")
    else:
        logger.error(f'Error occurred: {error}')
        await ctx.send(f"An error occurred: {error}")

@bot.event
async def on_member_join(member):
    """Event triggered when a new member joins the server"""
    # Find the welcome channel (usually named 'welcome' or 'general')
    welcome_channel = discord.utils.get(member.guild.text_channels, name='welcome')
    if not welcome_channel:
        welcome_channel = discord.utils.get(member.guild.text_channels, name='general')
    
    if welcome_channel:
        # Create welcome embed
        embed = discord.Embed(
            title=f"Welcome {member.display_name}!",
            description=f"Thanks for joining {member.guild.name}!",
            color=discord.Color.green()
        )
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        embed.add_field(name="Account Created", value=member.created_at.strftime("%b %d, %Y"), inline=True)
        embed.add_field(name="Server Member Count", value=member.guild.member_count, inline=True)
        embed.set_footer(text="Enjoy your stay!")
        
        await welcome_channel.send(embed=embed)
        
        # Send DM to new member
        try:
            await member.send(f"Welcome to {member.guild.name}! Be sure to read the rules and enjoy your stay.")
        except discord.Forbidden:
            logger.info(f"Could not send DM to {member.display_name}")

@bot.event
async def on_member_remove(member):
    """Event triggered when a member leaves the server"""
    # Find the goodbye channel (usually named 'goodbye' or 'general')
    goodbye_channel = discord.utils.get(member.guild.text_channels, name='goodbye')
    if not goodbye_channel:
        goodbye_channel = discord.utils.get(member.guild.text_channels, name='general')
    
    if goodbye_channel:
        # Create goodbye embed
        embed = discord.Embed(
            title=f"Goodbye {member.display_name}!",
            description=f"Sorry to see you leave {member.guild.name}.",
            color=discord.Color.red()
        )
        embed.set_thumbnail(url=member.avatar.url if member.avatar else member.default_avatar.url)
        embed.add_field(name="Joined Server", value=member.joined_at.strftime("%b %d, %Y"), inline=True)
        embed.add_field(name="Server Member Count", value=member.guild.member_count, inline=True)
        
        await goodbye_channel.send(embed=embed)

# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("No Discord token found. Please set DISCORD_TOKEN in your .env file.")
    else:
        # Start HTTP server in a separate thread
        server_thread = threading.Thread(target=start_http_server, daemon=True)
        server_thread.start()
        logger.info(f"Starting Discord bot")
        bot.run(DISCORD_TOKEN)