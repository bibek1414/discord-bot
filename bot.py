import bot
from discord.ext import commands
import os
import re
from dotenv import load_dotenv
import logging
import google.generativeai as genai

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('discord_bot')

# Load environment variables from .env file
load_dotenv()
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
GOOGLE_API_KEY = os.getenv('GOOGLE_API_KEY')  # Add your Google AI API key to .env file

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
intents = bot.Intents.default()
intents.message_content = True  # Need this to read message content

# Create bot instance with a command prefix and intents
bot = commands.Bot(command_prefix='!', intents=intents)

# Dictionary of keywords and responses
KEYWORDS = {
    'hello': 'Hello there!',
    'project': 'Need help with your project?',
    'python': 'Python is a great programming language!',
    'discord': 'Discord bots are fun to make!'
}

@bot.event
async def on_ready():
    """Event triggered when the bot is ready and connected to Discord"""
    logger.info(f'{bot.user.name} has connected to Discord!')
    logger.info(f'Bot is connected to {len(bot.guilds)} guilds')
    
    # Setting bot status
    await bot.change_presence(activity=bot.Activity(
        type=bot.ActivityType.listening, 
        name="!commands for help"
    ))

@bot.command(name='hello')
async def hello(ctx):
    """Command that responds with a greeting"""
    await ctx.send(f'Hello, {ctx.author.display_name}!')

@bot.command(name='commands')
async def commands_list(ctx):
    """Custom command that lists available commands"""
    help_text = """
**Bot Commands:**
`!hello` - Get a friendly greeting
`!commands` - Show this help message
`!info` - Learn about this bot
"""
    
    # Add AI command if Google AI is configured
    if model:
        help_text += "`!ask <question>` - Ask the bot a question using Google AI\n"
    
    help_text += "\nThe bot also responds to keywords like: python, discord, project, and hello."
    await ctx.send(help_text)

@bot.command(name='info')
async def info(ctx):
    """Command that provides information about the bot"""
    info_text = """
I'm a helpful Discord bot created with Discord.py!
I can respond to commands and detect keywords in your messages.
Use `!commands` to see what I can do.
"""
    
    # Add AI info if Google AI is configured
    if model:
        info_text += "I can also answer general questions using Google AI with the `!ask` command."
    
    await ctx.send(info_text)

@bot.command(name='ask')
async def ask(ctx, *, question=None):
    """Command that uses Google AI to answer questions"""
    if not model:
        await ctx.send("Sorry, Google AI integration is not available at the moment.")
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
                await ctx.send(response_text)
    
    except Exception as e:
        logger.error(f"Error with Google AI: {e}")
        await ctx.send(f"Sorry, I encountered an error while processing your question: {str(e)}")

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
            await ask(ctx, question=content)
            return
    
    # Check for keywords in the message
    for keyword, response in KEYWORDS.items():
        # Case-insensitive search for whole words
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, message.content.lower()):
            await message.channel.send(response)
            # Only respond to the first matched keyword to avoid spam
            break
    
    # Process commands (this is necessary when overriding on_message)
    await bot.process_commands(message)

@bot.event
async def on_command_error(ctx, error):
    """Handle command errors"""
    if isinstance(error, commands.CommandNotFound):
        await ctx.send("Command not found. Type `!commands` to see available commands.")
    else:
        logger.error(f'Error occurred: {error}')
        await ctx.send(f"An error occurred: {error}")

# Run the bot
if __name__ == "__main__":
    if not DISCORD_TOKEN:
        logger.error("No Discord token found. Please set DISCORD_TOKEN in your .env file.")
    else:
        bot.run(DISCORD_TOKEN)