import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from main import TranslateBot, emoji_only_part, sticker_image_url


class VisualMessageTests(unittest.IsolatedAsyncioTestCase):
    def message(self, content="", stickers=(), attachments=()):
        return SimpleNamespace(
            content=content, stickers=stickers, attachments=attachments,
            guild=SimpleNamespace(emojis=[SimpleNamespace(id=11111111111111111)]),
            reference=None,
        )

    def bot(self):
        return TranslateBot(SimpleNamespace(clusters=[]), SimpleNamespace(ids=set()),
                            SimpleNamespace(translate=AsyncMock(return_value="translated")))

    async def test_emoji_only_skips_translation(self):
        for content in ("😀", "🇯🇵 🇰🇷", "👍🏽", "👨‍👩‍👧‍👦", "1️⃣", "😀\n❤️", "<:local:11111111111111111>"):
            with self.subTest(content=content):
                bot = self.bot()
                self.assertEqual(await bot.build_chunks(self.message(content), 2, "en"), [content])
                bot.translator.translate.assert_not_called()

    async def test_mixed_external_emoji_preserves_order_and_animation(self):
        content = "<:local:11111111111111111> 😀 <a:external:22222222222222222> 🇯🇵"
        expected = "\n".join((
            "https://cdn.discordapp.com/emojis/11111111111111111.png", "😀",
            "https://cdn.discordapp.com/emojis/22222222222222222.gif", "🇯🇵",
        ))
        bot = self.bot()
        self.assertEqual(await bot.build_chunks(self.message(content), 2, "en"), [expected])
        bot.translator.translate.assert_not_called()

    async def test_text_with_emoji_still_translates(self):
        for content in ("草", "Hello 😀", "hello <:external:22222222222222222>", "1 + 1 = 2"):
            with self.subTest(content=content):
                bot = self.bot()
                self.assertEqual(await bot.build_chunks(self.message(content), 2, "en"), ["translated"])
                bot.translator.translate.assert_awaited_once_with(content, "en")
                self.assertIsNone(emoji_only_part(self.message(content)))

    async def test_sticker_only_message(self):
        sticker = discord.StickerItem(data={"id": "22222222222222222", "name": "test", "format_type": 1}, state=None)
        bot = self.bot()
        self.assertEqual(await bot.build_chunks(self.message(stickers=[sticker]), 2, "ko"),
                         ["https://cdn.discordapp.com/stickers/22222222222222222.png"])
        bot.translator.translate.assert_not_called()

    async def test_sticker_formats_use_library_urls(self):
        for format_type, expected in (
            (1, "https://cdn.discordapp.com/stickers/22222222222222222.png"),
            (2, "https://cdn.discordapp.com/stickers/22222222222222222.png"),
            (3, "https://media.discordapp.net/stickers/22222222222222222.png"),
            (4, "https://media.discordapp.net/stickers/22222222222222222.gif"),
        ):
            with self.subTest(format_type=format_type):
                sticker = discord.StickerItem(data={"id": "22222222222222222", "name": "test", "format_type": format_type}, state=None)
                self.assertEqual(sticker_image_url(sticker), expected)

    async def test_text_attachments_and_stickers_are_all_retained(self):
        bot = self.bot()
        message = self.message("hello", stickers=[SimpleNamespace(id=42, format=discord.StickerFormatType.png, url="https://cdn.discordapp.com/stickers/42.png")],
                               attachments=[SimpleNamespace(url="https://example.test/file.png")])
        self.assertEqual(await bot.build_chunks(message, 2, "en"),
                         ["translated\nhttps://example.test/file.png\nhttps://cdn.discordapp.com/stickers/42.png"])

    async def test_empty_message_stays_empty(self):
        bot = self.bot()
        self.assertEqual(await bot.build_chunks(self.message(), 2, "en"), [])
        bot.translator.translate.assert_not_called()

    async def test_many_custom_emoji_respect_message_length(self):
        bot = self.bot()
        chunks = await bot.build_chunks(self.message("<:external:22222222222222222> " * 50), 2, "en")
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 1900 for chunk in chunks))
        self.assertEqual("\n".join(chunks).count("https://cdn.discordapp.com/emojis/22222222222222222.png"), 50)
        bot.translator.translate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
