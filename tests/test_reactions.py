import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from main import Mirror, TranslateBot


class Message:
    def __init__(self, message_id):
        self.id = message_id
        self.entries = {}

    def entry(self, emoji):
        return self.entries.setdefault(str(emoji), [emoji, 0, False])

    @property
    def reactions(self):
        return [
            SimpleNamespace(emoji=emoji, count=people + int(me), me=me)
            for emoji, people, me in self.entries.values()
            if people or me
        ]

    async def add_reaction(self, emoji):
        self.entry(emoji)[2] = True

    async def remove_reaction(self, emoji, user):
        self.entry(emoji)[2] = False


class ReactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = TranslateBot(
            SimpleNamespace(clusters=[{"ja": 1, "en": 2, "ko": 3}]),
            SimpleNamespace(ids=set()),
            SimpleNamespace(),
        )
        self.bot._connection.user = SimpleNamespace(id=999)
        self.messages = {mid: Message(mid) for mid in (10, 20, 21, 30)}
        self.bot.fetch_mirrored_message = AsyncMock(
            side_effect=lambda channel, mid: self.messages[mid]
        )
        self.bot.get_guild = lambda _: SimpleNamespace(emojis=[SimpleNamespace(id=123)])
        self.bot.remember(10, Mirror(1, "hello", {2: [20, 21], 3: [30]}))
        self.emoji = discord.PartialEmoji(name="👍")

    def payload(self, mid, emoji=None, user=100):
        return SimpleNamespace(
            channel_id={10: 1, 20: 2, 21: 2, 30: 3}[mid],
            message_id=mid,
            guild_id=42,
            user_id=user,
            emoji=emoji or self.emoji,
        )

    async def react(self, mid, count, emoji=None):
        emoji = emoji or self.emoji
        entry = self.messages[mid].entry(emoji)
        before = entry[1]
        entry[1] = count
        handler = self.bot.on_raw_reaction_add if count > before else self.bot.on_raw_reaction_remove
        await handler(self.payload(mid, emoji))

    def assert_forwarded(self, expected, emoji=None):
        emoji = emoji or self.emoji
        actual = {mid for mid, message in self.messages.items() if message.entry(emoji)[2]}
        self.assertEqual(actual, set(expected))

    async def test_bidirectional_add_remove(self):
        await self.react(10, 1)
        self.assert_forwarded({20, 30})
        await self.react(10, 0)
        self.assert_forwarded(set())
        await self.react(20, 1)
        self.assert_forwarded({10, 30})

    async def test_other_channel_reaction_survives_removal(self):
        await self.react(10, 1)
        await self.react(20, 1)
        await self.react(10, 0)
        self.assert_forwarded({10, 30})
        await self.react(20, 0)
        self.assert_forwarded(set())

    async def test_multiple_people_on_same_message(self):
        await self.react(10, 2)
        await self.react(10, 1)
        self.assert_forwarded({20, 30})

    async def test_clear_emoji_preserves_other_channel(self):
        await self.react(10, 1)
        await self.react(20, 1)
        self.messages[10].entries.clear()
        await self.bot.on_raw_reaction_clear_emoji(self.payload(10))
        self.assert_forwarded({10, 30})

    async def test_clear_all_preserves_unrelated_reactions(self):
        heart = discord.PartialEmoji(name="❤️")
        await self.react(10, 1)
        await self.react(20, 1, heart)
        self.messages[10].entries.clear()
        await self.bot.on_raw_reaction_clear(self.payload(10))
        self.assert_forwarded(set())
        self.assert_forwarded({10, 30}, heart)

    async def test_failed_fetch_does_not_remove_existing_reactions(self):
        await self.react(10, 1)
        self.messages[10].entry(self.emoji)[1] = 0
        self.bot.fetch_mirrored_message = AsyncMock(
            side_effect=lambda channel, mid: None if mid == 20 else self.messages[mid]
        )
        await self.bot.on_raw_reaction_remove(self.payload(10))
        self.assert_forwarded({20, 30})

    async def test_does_not_remove_untracked_bot_warning(self):
        warning = discord.PartialEmoji(name="⚠️")
        self.messages[10].entry(warning)[2] = True
        await self.react(20, 1, warning)
        await self.react(20, 0, warning)
        self.assert_forwarded({10}, warning)

    async def test_ignored_events(self):
        await self.bot.on_raw_reaction_add(self.payload(10, user=999))
        await self.bot.on_raw_reaction_add(self.payload(10, discord.PartialEmoji(name="outside", id=456)))
        await self.bot.on_raw_reaction_add(self.payload(21))
        self.assertEqual(self.bot.fetch_mirrored_message.await_count, 0)

    async def test_local_custom_emoji(self):
        custom = discord.PartialEmoji(name="inside", id=123)
        await self.react(10, 1, custom)
        self.assert_forwarded({20, 30}, custom)

    async def test_edit_keeps_tracking_for_retained_message_ids(self):
        await self.react(10, 1)
        edited = SimpleNamespace(content="edited")
        self.bot.get_channel = lambda _: SimpleNamespace(fetch_message=AsyncMock(return_value=edited))
        self.bot.resync = AsyncMock(side_effect=lambda message, cid, lang, ids: ids)
        await self.bot.on_raw_message_edit(self.payload(10))
        await self.react(10, 0)
        self.assert_forwarded(set())

    async def test_interleaved_add_and_remove_finish_consistently(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first = True

        async def fetch(channel, mid):
            nonlocal first
            if first:
                first = False
                entered.set()
                await release.wait()
            return self.messages[mid]

        self.bot.fetch_mirrored_message = AsyncMock(side_effect=fetch)
        self.messages[10].entry(self.emoji)[1] = 1
        adding = asyncio.create_task(self.bot.on_raw_reaction_add(self.payload(10)))
        await entered.wait()
        self.messages[10].entry(self.emoji)[1] = 0
        removing = asyncio.create_task(self.bot.on_raw_reaction_remove(self.payload(10)))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(adding, removing)
        self.assert_forwarded(set())

    async def test_forget_cleans_tracking(self):
        await self.react(10, 1)
        self.bot.forget(10)
        self.assertEqual(self.bot.reaction_mirrors, {})
        self.assertEqual(self.bot.origins, {})


if __name__ == "__main__":
    unittest.main()
