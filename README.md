#Run

`DISCORD_TOKEN=<your discord bot token> BOT_OWNER_USER_NAME=<the discord user to send admin messages to> docker-compose up`

## All valid env vars
DISCORD_TOKEN
MONGO_URI
BOT_OWNER_USER_NAME

#Authorize Guilds

Whenever a new server tries to run the bot, it must be explicitly authorized. Connect to the mongo db instance using
mongo compass, retrieve the doc for the server in youtube_streamer.guilds, and set `authorized` to `True`.