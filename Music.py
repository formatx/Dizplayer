import discord
from discord import Option
from discord.ext import commands
import random
import asyncio
import itertools
import sys
import traceback
from async_timeout import timeout
from functools import partial
import youtube_dl
from youtube_dl import YoutubeDL

from decorators import copy_doc


# Suppress noise about console usage from errors
youtube_dl.utils.bug_reports_message = lambda: ''

ytdlopts = {
    'format': 'bestaudio/best',
    'outtmpl': 'downloads/%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'  # ipv6 addresses cause issues sometimes
}

ffmpegopts = {
    'before_options': '-nostdin',
    'options': '-vn'
}

ytdl = YoutubeDL(ytdlopts)


class VoiceConnectionError(commands.CommandError):
    """Custom Exception class for connection errors."""


class InvalidVoiceChannel(VoiceConnectionError):
    """Exception for cases of invalid Voice Channels."""


class GuildNotAuthorized(VoiceConnectionError):
    """This guild is not authorized to use the bot"""


class YTDLSource(discord.PCMVolumeTransformer):

    def __init__(self, source, *, data, requester):
        super().__init__(source)
        self.requester = requester

        self.title = data.get('title')
        self.web_url = data.get('webpage_url')
        self.duration = data.get('duration')

        # YTDL info dicts (data) have other useful information you might want
        # https://github.com/rg3/youtube-dl/blob/master/README.md

    def __getitem__(self, item: str):
        """Allows us to access attributes similar to a dict.
        This is only useful when you are NOT downloading.
        """
        return self.__getattribute__(item)

    @classmethod
    async def create_source(cls, ctx, search: str, *, loop, download=False):
        loop = loop or asyncio.get_event_loop()

        to_run = partial(ytdl.extract_info, url=search, download=download)
        data = await loop.run_in_executor(None, to_run)

        if 'entries' in data:
            # take first item from a playlist
            data = data['entries'][0]

        embed = discord.Embed(title="",
                              description=f"Queued [{data['title']}]({data['webpage_url']}) [{ctx.author.mention}]",
                              color=discord.Color.green())
        await ctx.send(embed=embed)

        if download:
            source = ytdl.prepare_filename(data)
        else:
            return {'webpage_url': data['webpage_url'], 'requester': ctx.author, 'title': data['title']}

        return cls(discord.FFmpegPCMAudio(source), data=data, requester=ctx.author)

    @classmethod
    async def regather_stream(cls, data, *, loop):
        """Used for preparing a stream, instead of downloading.
        Since Youtube Streaming links expire."""
        loop = loop or asyncio.get_event_loop()
        requester = data['requester']

        to_run = partial(ytdl.extract_info, url=data['webpage_url'], download=False)
        data = await loop.run_in_executor(None, to_run)

        return cls(discord.FFmpegPCMAudio(data['url']), data=data, requester=requester)


class MusicPlayer:
    """A class which is assigned to each guild using the bot for Music.
    This class implements a queue and loop, which allows for different guilds to listen to different playlists
    simultaneously.
    When the bot disconnects from the Voice it's instance will be destroyed.
    """

    __slots__ = ('bot', '_guild', '_channel', '_cog', 'queue', 'next', 'current', 'np', 'volume', 'ctx', 'repeat', 'current_source')

    def __init__(self, ctx):
        self.bot = ctx.bot
        self._guild = ctx.guild
        self._channel = ctx.channel
        self._cog = ctx.cog

        self.queue = asyncio.Queue()
        self.next = asyncio.Event()

        self.np = None  # Now playing message
        self.volume = .5
        self.current = None
        self.ctx = ctx
        self.repeat = False
        self.current_source = None

        ctx.bot.loop.create_task(self.player_loop())

    async def player_loop(self):
        """Our main player loop."""
        await self.bot.wait_until_ready()

        while not self.bot.is_closed():
            self.next.clear()

            try:
                # Wait for the next song. If we timeout cancel the player and disconnect...
                async with timeout(300):  # 5 minutes...
                    source = await self.queue.get()
            except asyncio.TimeoutError:
                return self.destroy(self._guild)
            self.current_source = source
            if not isinstance(source, YTDLSource):
                # Source was probably a stream (not downloaded)
                # So we should regather to prevent stream expiration
                try:
                    gathered_source = await YTDLSource.regather_stream(source, loop=self.bot.loop)
                except Exception as e:
                    await self.ctx.send(f'There was an error processing your song.\n'
                                             f'```css\n[{e}]\n```')
                    continue

            gathered_source.volume = self.volume
            self.current = gathered_source
            if self.repeat:
                # new_source = await YTDLSource.create_source(self.ctx, source['search'], loop=self.bot.loop, download=False)
                # new_source.search = search
                self.queue._queue.appendleft(source)
            self._guild.voice_client.play(gathered_source, after=lambda _: self.bot.loop.call_soon_threadsafe(self.next.set))
            embed = discord.Embed(title="Now playing",
                                  description=f"[{gathered_source.title}]({gathered_source.web_url}) [{gathered_source.requester.mention}]",
                                  color=discord.Color.green())
            self.np = await self.ctx.send(embed=embed)
            await self.next.wait()

            # Make sure the FFmpeg process is cleaned up.
            gathered_source.cleanup()
            self.current = None

    def destroy(self, guild):
        """Disconnect and cleanup the player."""
        return self.bot.loop.create_task(self._cog.cleanup(guild))


class Music(commands.Cog):
    """Music related commands."""

    __slots__ = ('bot', 'players')

    def __init__(self, bot):
        self.bot = bot
        self.players = {}
        self.bot.application_command(name="join", cls=discord.SlashCommand)(self.join_slash)
        self.bot.application_command(name="play", cls=discord.SlashCommand)(self.play_slash)
        self.bot.application_command(name="pause", cls=discord.SlashCommand)(self.pause_slash)
        self.bot.application_command(name="resume", cls=discord.SlashCommand)(self.resume_slash)
        self.bot.application_command(name="skip", cls=discord.SlashCommand)(self.skip_slash)
        self.bot.application_command(name="volume", cls=discord.SlashCommand)(self.volume_slash)
        self.bot.application_command(name="queue", cls=discord.SlashCommand)(self.queue_slash)
        self.bot.application_command(name="now_playing", cls=discord.SlashCommand)(self.now_playing_slash)
        self.bot.application_command(name="repeat", cls=discord.SlashCommand)(self.repeat_slash)
        self.bot.application_command(name="remove", cls=discord.SlashCommand)(self.remove_slash)
        self.bot.application_command(name="clear", cls=discord.SlashCommand)(self.clear_slash)
        self.bot.application_command(name="leave", cls=discord.SlashCommand)(self.leave_slash)



    async def cleanup(self, guild):
        try:
            await guild.voice_client.disconnect()
        except AttributeError:
            pass

        try:
            del self.players[guild.id]
        except KeyError:
            pass

    async def __local_check(self, ctx):
        """A local check which applies to all commands in this cog."""
        if not ctx.guild:
            raise commands.NoPrivateMessage
        return True

    async def __error(self, ctx, error):
        """A local error handler for all errors arising from commands in this cog."""
        if isinstance(error, commands.NoPrivateMessage):
            try:
                return await ctx.send('This command can not be used in Private Messages.')
            except discord.HTTPException:
                pass
        elif isinstance(error, InvalidVoiceChannel):
            await ctx.send('Error connecting to Voice Channel. '
                           'Please make sure you are in a valid channel or provide me with one')

        print('Ignoring exception in command {}:'.format(ctx.command), file=sys.stderr)
        traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)

    async def get_player(self, ctx):
        """Retrieve the guild player, or generate one."""
        try:
            player = self.players[ctx.guild.id]
        except KeyError:
            guilds = self.bot.mongo_db.guilds
            this_guild = guilds.find_one({"guild_id": ctx.guild.id})

            if not this_guild or not this_guild['authorized']:
                embed = discord.Embed(title="Error",
                                      description=f'{ctx.guild.name} ({ctx.guild.id}) has not been authorized to use the streamer. Please request authorization.',                                      color=discord.Color.green())
                await ctx.send(embed=embed)
                raise GuildNotAuthorized(
                    f'{ctx.guild.name} ({ctx.guild.id}) has not been authorized to use the streamer. Please request authorization.')

            player = MusicPlayer(ctx)
            self.players[ctx.guild.id] = player
            default_volume_percentage = this_guild['volume']
            player.volume = default_volume_percentage / 100
            embed = discord.Embed(title="", description=f'**Player Spun Up** setting the default volume to **{default_volume_percentage}%**',
                                  color=discord.Color.green())
            await ctx.send(embed=embed)
        return player

    @commands.command(name='join', aliases=['connect', 'j'], description="connects to voice")
    async def connect_(self, ctx, *, channel: discord.VoiceChannel = None):
        """Connect to voice.
        Parameters
        ------------
        channel: discord.VoiceChannel [Optional]
            The channel to connect to. If a channel is not specified, an attempt to join the voice channel you are in
            will be made.
        This command also handles moving the bot to different channels.
        """
        if not channel:
            try:
                channel = ctx.author.voice.channel
            except AttributeError:
                embed = discord.Embed(title="",
                                      description="No channel to join. Please call `,join` from a voice channel.",
                                      color=discord.Color.green())
                await ctx.send(embed=embed)
                raise InvalidVoiceChannel('No channel to join. Please either specify a valid channel or join one.')

        vc = ctx.voice_client

        if vc:
            if vc.channel.id == channel.id:
                return
            try:
                await vc.move_to(channel)
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Moving to channel: <{channel}> timed out.')
        else:
            try:
                await channel.connect()
            except asyncio.TimeoutError:
                raise VoiceConnectionError(f'Connecting to channel: <{channel}> timed out.')

        await ctx.send(f'**Joined `{channel}`**')

    async def join_slash(self,
                         ctx: commands.Context,
                         channel: Option(discord.VoiceChannel,
                                        description="The channel to connect to. By default, the bot will attempt to join your current voice channel.",
                                        required=False,
                                        default=None)):
        """Connect the bot to a voice channel"""
        await ctx.respond(f'Request to join {channel} received. Processing...', ephemeral=True)
        await self.connect_(ctx, channel=channel)

    @commands.command(name='play', aliases=['sing', 'p'], description="streams music")
    async def play_(self, ctx, *, search: str):
        """Request a song and add it to the queue.
        This command attempts to join a valid voice channel if the bot is not already in one.
        Uses YTDL to automatically search and retrieve a song.

        Args:
        search (str): The song to search and retrieve using YTDL. This could be a simple search, an ID or URL.
        """
        await ctx.trigger_typing()

        vc = ctx.voice_client

        if not vc:
            await ctx.invoke(self.connect_)

        player = await self.get_player(ctx)

        # If download is False, source will be a dict which will be used later to regather the stream.
        # If download is True, source will be a discord.FFmpegPCMAudio with a VolumeTransformer.
        source = await YTDLSource.create_source(ctx, search, loop=self.bot.loop, download=False)

        await player.queue.put(source)

    async def play_slash(self,
                         ctx: commands.Context,
                         *,
                         search: Option(str,
                                        description="The song to search and retrieve. This could be a simple search, an ID or URL.",
                                        required=True)):
        """Request a song and add it to the queue."""
        await ctx.respond(f'Request to play {search} received. Processing...', ephemeral=True)
        await self.play_(ctx, search=search)

    @commands.command(name='repeat', description="repeats song until called again")
    async def repeat_(self, ctx):
        """Repeat the currently paused song."""
        player = await self.get_player(ctx)
        player.repeat = not player.repeat
        if not player.repeat:
            player.queue._queue.popleft()
        if player.repeat:
            player.queue._queue.appendleft(player.current_source)

        await ctx.send(f"Repeat 🔁️ toggled to {player.repeat}")

    async def repeat_slash(self, ctx):
        """Repeat the current song until 'repeat' called again."""
        await ctx.respond(f'Toggling repeat...', ephemeral=True)
        await self.repeat_(ctx)

    @commands.command(name='pause', description="pauses music")
    async def pause_(self, ctx):
        """Pause the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_playing():
            embed = discord.Embed(title="", description="I am currently not playing anything",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif vc.is_paused():
            return

        vc.pause()
        await ctx.send("Paused ⏸️")

    async def pause_slash(self, ctx):
        """Pause the currently playing song."""
        await ctx.respond(f'Pausing...', ephemeral=True)
        await self.pause_(ctx)

    @commands.command(name='resume', description="resumes music")
    async def resume_(self, ctx):
        """Resume the currently paused song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)
        elif not vc.is_paused():
            return

        vc.resume()
        await ctx.send("Resuming ⏯️")

    async def resume_slash(self, ctx):
        """Resume the currently paused song."""
        await ctx.respond(f'Attempting to resume...', ephemeral=True)
        await self.resume_(ctx)

    @commands.command(name='skip', description="skips to next song in queue")
    async def skip_(self, ctx):
        """Skip the song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        if vc.is_paused():
            pass
        elif not vc.is_playing():
            return

        vc.stop()

    async def skip_slash(self, ctx):
        """Skip the song."""
        await ctx.respond(f'Attempting to skip...', ephemeral=True)
        await self.skip_(ctx)

    @commands.command(name='remove', aliases=['rm', 'rem'], description="removes specified song from queue")
    async def remove_(self, ctx, pos: int = None):
        """Removes specified song from queue"""

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = await self.get_player(ctx)
        if pos == None:
            player.queue._queue.pop()
        else:
            try:
                s = player.queue._queue[pos - 1]
                del player.queue._queue[pos - 1]
                embed = discord.Embed(title="",
                                      description=f"Removed [{s['title']}]({s['webpage_url']}) [{s['requester'].mention}]",
                                      color=discord.Color.green())
                await ctx.send(embed=embed)
            except:
                embed = discord.Embed(title="", description=f'Could not find a track for "{pos}"',
                                      color=discord.Color.green())
                await ctx.send(embed=embed)

    async def remove_slash(self, ctx, pos: Option(int,
                                        description="The position of the song in the queue to remove.",
                                        required=True)):
        """Removes specified song from queue"""
        await ctx.respond(f'Attempting to remove song at position {pos}...', ephemeral=True)
        await self.remove_(ctx, pos=pos)

    @commands.command(name='clear', aliases=['clr', 'cl', 'cr'], description="clears entire queue")
    async def clear_(self, ctx):
        """Deletes entire queue of upcoming songs."""

        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = await self.get_player(ctx)
        player.queue._queue.clear()
        await ctx.send('**Cleared**')

    async def clear_slash(self, ctx):
        """Empties the queue."""
        await ctx.respond(f'Attempting to clear the queue...', ephemeral=True)
        await self.clear_(ctx)

    @commands.command(name='queue', aliases=['q', 'playlist', 'que'], description="shows the queue")
    async def queue_info(self, ctx):
        """Retrieve a basic queue of upcoming songs."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = await self.get_player(ctx)
        if player.queue.empty():
            embed = discord.Embed(title="", description="queue is empty", color=discord.Color.green())
            return await ctx.send(embed=embed)

        seconds = vc.source.duration % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        if hour > 0:
            duration = "%dh %02dm %02ds" % (hour, minutes, seconds)
        else:
            duration = "%02dm %02ds" % (minutes, seconds)

        # Grabs the songs in the queue...
        upcoming = list(itertools.islice(player.queue._queue, 0, int(len(player.queue._queue))))
        fmt = '\n'.join(
            f"`{(upcoming.index(_)) + 1}.` [{_['title']}]({_['webpage_url']}) | ` {duration} Requested by: {_['requester']}`\n"
            for _ in upcoming)
        fmt = f"\n__Now Playing__:\n[{vc.source.title}]({vc.source.web_url}) | ` {duration} Requested by: {vc.source.requester}`\n\n__Up Next:__\n" + fmt + f"\n**{len(upcoming)} songs in queue**"
        embed = discord.Embed(title=f'Queue for {ctx.guild.name}', description=fmt, color=discord.Color.green())
        embed.set_footer(text=f"{ctx.author.display_name}", icon_url=ctx.author.avatar.url)

        await ctx.send(embed=embed)

    async def queue_slash(self, ctx):
        """Describes the queue of upcoming songs."""
        await ctx.respond(f'Attempting to describe the queue...', ephemeral=True)
        await self.queue_info(ctx)

    @commands.command(name='np', aliases=['song', 'current', 'currentsong', 'playing'],
                      description="shows the current playing song")
    async def now_playing_(self, ctx):
        """Display information about the currently playing song."""
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = await self.get_player(ctx)
        if not player.current:
            embed = discord.Embed(title="", description="I am currently not playing anything",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        seconds = vc.source.duration % (24 * 3600)
        hour = seconds // 3600
        seconds %= 3600
        minutes = seconds // 60
        seconds %= 60
        if hour > 0:
            duration = "%dh %02dm %02ds" % (hour, minutes, seconds)
        else:
            duration = "%02dm %02ds" % (minutes, seconds)

        embed = discord.Embed(title="",
                              description=f"[{vc.source.title}]({vc.source.web_url}) [{vc.source.requester.mention}] | `{duration}`",
                              color=discord.Color.green())
        embed.set_author(icon_url=self.bot.user.display_avatar.url, name=f"Now Playing 🎶")
        await ctx.send(embed=embed)

    async def now_playing_slash(self, ctx):
        """Display information about the currently playing song."""
        await ctx.respond(f'Attempting to report now playing...', ephemeral=True)
        await self.now_playing_(ctx)

    @commands.command(name='volume', aliases=['vol', 'v'], description="changes Kermit's volume")
    async def change_volume(self, ctx, *, vol: float = None):
        """Change the player volume.
        Parameters
        ------------
        volume: float or int [Required]
            The volume to set the player to in percentage. This must be between 1 and 100.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I am not currently connected to voice",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        if not vol:
            embed = discord.Embed(title="", description=f"🔊 **{(vc.source.volume) * 100}%**",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        if not 0 < vol < 101:
            embed = discord.Embed(title="", description="Please enter a value between 1 and 100",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        player = await self.get_player(ctx)

        if vc.source:
            vc.source.volume = vol / 100

        player.volume = vol / 100
        embed = discord.Embed(title="", description=f'**`{ctx.author}`** set the volume to **{vol}%**',
                              color=discord.Color.green())

        #store new volume on guild in mongo
        query = {"guild_id": ctx.guild.id}
        new_value = {"$set": {"volume": vol}}
        guilds = self.bot.mongo_db.guilds
        guilds.update_one(query, new_value)

        await ctx.send(embed=embed)

    async def volume_slash(self, ctx, *, vol: Option(float,
                                        description="Volume level. Default is 5.",
                                        required=True,
                                        min_value=0,
                                        max_value=100)):
        """Change the player volume. A little goes a long way."""
        await ctx.respond(f'Attempting to change volume to {vol}...', ephemeral=True)
        await self.change_volume(ctx, vol)

    @commands.command(name='leave', aliases=["stop", "dc", "disconnect", "bye"],
                      description="stops music and disconnects from voice")
    async def leave_(self, ctx):
        """Stop the currently playing song and destroy the player.
        !Warning!
            This will destroy the player assigned to your guild, also deleting any queued songs and settings.
        """
        vc = ctx.voice_client

        if not vc or not vc.is_connected():
            embed = discord.Embed(title="", description="I'm not connected to a voice channel",
                                  color=discord.Color.green())
            return await ctx.send(embed=embed)

        await ctx.send('**Successfully disconnected**')

        await self.cleanup(ctx.guild)

    async def leave_slash(self, ctx):
        """Stop the currently playing song and destroy the player."""
        await ctx.respond(f'Attempting to destroy the player...', ephemeral=True)
        await self.leave_(ctx)

