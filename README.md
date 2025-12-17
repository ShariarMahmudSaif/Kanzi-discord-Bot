# Kanzi Bot

A comprehensive Discord bot with music streaming, fun APIs, AI assistance, and more.

## Features

- 🎵 **Music Streaming**: Play from YouTube, SoundCloud, and more using yt-dlp
- 🤖 **AI Integration**: Get help, fix errors, and song suggestions with OpenAI GPT
- 🎮 **Fun Commands**: Anime, games, jokes, memes, and quizzes
- 👤 **Profiles**: Custom themes, banners, and stats
- 🔧 **Admin Tools**: Manage roles, send messages, and voice controls
- 📊 **Metrics**: Prometheus integration for monitoring

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/yourusername/kanzi-bot.git
   cd kanzi-bot
   ```

2. Create a virtual environment:
   ```bash
   python -m venv .venv
   ```

3. Activate the virtual environment:
   - Windows: `.venv\Scripts\activate`
   - Linux/Mac: `source .venv/bin/activate`

4. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

5. Set up environment variables in `.env`:
   ```
   DISCORD_TOKEN=your_discord_bot_token
   SPOTIFY_CLIENT_ID=your_spotify_id
   SPOTIFY_CLIENT_SECRET=your_spotify_secret
   OPENAI_API_KEY=your_openai_key
   ```

## Running the Bot

```bash
python kanzi_bot.py
```

## Commands

### Music
- `/play <song name or link>` - Play music
- `/stop` - Stop playback
- `/skip` - Skip track
- `/pause` / `/resume` - Control playback

### AI
- `/ai_help <query>` - Ask AI for help
- `/ai_fix <error>` - Get AI fix for errors
- `/ai_play <mood>` - Song suggestion by mood

### Fun
- `/anime_search <query>` - Search anime
- `/joke` - Get a joke
- `/meme` - Random meme

### Profile
- `/profile` - View your profile
- `/theme_set <theme>` - Change theme

### Admin
- `/admin_send <channel> <message>` - Send message as bot
- `/admin_join <channel>` - Join voice channel

## Architecture

- **Main File**: `kanzi_bot.py` - Core bot logic
- **Data Storage**: `data/` - JSON files for profiles, scores, etc.
- **Config**: `.env` - Environment variables
- **Dependencies**: `requirements.txt` - Python packages

## Contributing

1. Fork the repo
2. Create a feature branch
3. Commit changes
4. Push and create PR

## License

MIT License - see LICENSE file

## Author

Shariar Mahmud Saif</content>
<parameter name="filePath">..\kanzi Bot\README.md
