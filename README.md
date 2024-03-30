# Twitch TikTok Discord Queue Bot

Puts users from the stream chat or discord voice channel into a queue that admins can manage so that mods/streamers don't have to manage a list from multiple different sources.

## Behind the scenes

To make the magic happen we are using 3 git repos, first we are using the [QueueBot](https://github.com/LaurenceRawlings/queue-bot) as our foundation for the discord bot which will be displaying the queue and be the center of it all then we have [NightPy](https://github.com/Amatobahn/NightPy) to feed into it from twitch and [TikTokLive](https://github.com/isaackogan/TikTokLive) to feed in from Tik Tok.

The main changes that we are making to the foundational QueueBot is that instead of the queue being solely of discord users in a vc it is going to be a list of ign of people not necessarily in the discord guild.
