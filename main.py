import asyncio
import json
import logging
import os
import re
import sys
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

import discord
from discord import app_commands
from dotenv import load_dotenv

from translate import TranslationError, Translator

WEBHOOK_NAME = "JA-EN Translator"
MAX_LENGTH = 1900
MAX_TRACKED = 5000
MAX_CHANNEL_BASE = 97
LANGS = ("ja", "en", "ko")
JSON_KEYS = {"ja": "JP", "en": "EN", "ko": "KO"}
SUFFIXES = {"ja": "-jp", "en": "-en", "ko": "-ko"}
REPLY_LABEL = {"ja": "返信", "en": "Reply", "ko": "답장"}
TRANSLATED_TYPES = (
    discord.MessageType.default,
    discord.MessageType.reply,
    discord.MessageType.chat_input_command,
    discord.MessageType.context_menu_command,
)
NO_MENTIONS = discord.AllowedMentions.none()

log = logging.getLogger("translate-bot")

Cluster = dict[str, int]


@dataclass
class Mirror:
    source_channel_id: int
    source_content: str
    targets: dict[int, list[int]]


class ClusterStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.clusters: list[Cluster] = [
            {lang: int(entry[JSON_KEYS[lang]]) for lang in LANGS if JSON_KEYS[lang] in entry}
            for entry in raw
        ]

    def contains(self, channel_id: int) -> bool:
        return any(channel_id in cluster.values() for cluster in self.clusters)

    def add(self, cluster: Cluster) -> None:
        self.clusters.append(cluster)
        self.save()

    def remove_channel(self, channel_id: int) -> None:
        for cluster in self.clusters:
            for lang, cid in list(cluster.items()):
                if cid == channel_id:
                    del cluster[lang]
        self.clusters = [cluster for cluster in self.clusters if len(cluster) >= 2]
        self.save()

    def save(self) -> None:
        data = [
            {JSON_KEYS[lang]: str(cluster[lang]) for lang in LANGS if lang in cluster}
            for cluster in self.clusters
        ]
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class BotStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        self.ids: set[int] = {int(bot_id) for bot_id in raw}

    def add(self, bot_id: int) -> None:
        self.ids.add(bot_id)
        self.save()

    def remove(self, bot_id: int) -> None:
        self.ids.discard(bot_id)
        self.save()

    def save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps([str(bot_id) for bot_id in sorted(self.ids)], indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def split_message(text: str, limit: int = MAX_LENGTH) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n ")
    if text:
        chunks.append(text)
    return chunks


def slugify(name: str, suffix: str) -> str:
    slug = re.sub(r"[^\w]+", "-", name.lower()).strip("-")
    slug = slug.removesuffix(suffix)
    return slug[:MAX_CHANNEL_BASE].strip("-") or "translated"


def webhook_username(member: discord.abc.User) -> str:
    name = member.display_name.strip()[:80]
    lowered = name.lower()
    if not name or "discord" in lowered or "clyde" in lowered:
        return member.name[:80] if member.name else "User"
    return name


class TranslateBot(discord.Client):
    def __init__(self, store: ClusterStore, bots: BotStore, translator: Translator) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.tree = app_commands.CommandTree(self)
        self.store = store
        self.bots = bots
        self.translator = translator
        self.cluster_of: dict[int, Cluster] = {}
        self.lang_of: dict[int, str] = {}
        self.webhooks: dict[int, discord.Webhook] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.mirrors: OrderedDict[int, Mirror] = OrderedDict()
        self.origins: dict[int, int] = {}
        self.initialized = False
        self.reindex()

    def reindex(self) -> None:
        self.cluster_of.clear()
        self.lang_of.clear()
        for cluster in self.store.clusters:
            for lang, channel_id in cluster.items():
                self.cluster_of[channel_id] = cluster
                self.lang_of[channel_id] = lang
                self.locks.setdefault(channel_id, asyncio.Lock())

    async def setup_hook(self) -> None:
        @self.tree.command(
            name="create-tr-channel",
            description="このチャンネルの英語・韓国語の翻訳チャンネルを作って紐付けます",
        )
        @app_commands.guild_only()
        @app_commands.default_permissions(manage_channels=True)
        async def create_tr_channel(interaction: discord.Interaction) -> None:
            await self.create_tr_channel(interaction)

        bot_group = app_commands.Group(
            name="tr-bot",
            description="翻訳する Bot の設定",
            guild_only=True,
            default_permissions=discord.Permissions(manage_channels=True),
        )

        @bot_group.command(name="add", description="指定した Bot の発言も翻訳するようにします")
        @app_commands.describe(bot="翻訳する Bot")
        async def tr_bot_add(interaction: discord.Interaction, bot: discord.User) -> None:
            await self.add_translated_bot(interaction, bot)

        @bot_group.command(name="remove", description="指定した Bot の発言を翻訳しないようにします")
        @app_commands.describe(bot="翻訳をやめる Bot")
        async def tr_bot_remove(interaction: discord.Interaction, bot: discord.User) -> None:
            await self.remove_translated_bot(interaction, bot)

        @bot_group.command(name="list", description="発言を翻訳する Bot の一覧を表示します")
        async def tr_bot_list(interaction: discord.Interaction) -> None:
            await self.list_translated_bots(interaction)

        self.tree.add_command(bot_group)

    async def on_ready(self) -> None:
        if self.initialized:
            return
        self.initialized = True
        for guild in self.guilds:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        for channel_id in list(self.lang_of):
            channel = await self.resolve_channel(channel_id)
            if channel:
                await self.setup_webhook(channel)
        await self.add_missing_korean_channels()
        log.info("ready as %s with %d cluster(s)", self.user, len(self.store.clusters))

    async def resolve_channel(self, channel_id: int) -> discord.TextChannel | None:
        try:
            channel = self.get_channel(channel_id) or await self.fetch_channel(channel_id)
        except discord.HTTPException:
            log.warning("channel %s is not accessible; skipping", channel_id)
            return None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def setup_webhook(self, channel: discord.TextChannel) -> None:
        hooks = await channel.webhooks()
        hook = next((h for h in hooks if h.name == WEBHOOK_NAME and h.user == self.user), None)
        self.webhooks[channel.id] = hook or await channel.create_webhook(name=WEBHOOK_NAME)

    async def create_language_channel(
        self, template: discord.TextChannel, base: str, lang: str
    ) -> discord.TextChannel:
        translated = await self.translator.channel_name(base, lang)
        channel = await template.clone(name=slugify(translated, SUFFIXES[lang]) + SUFFIXES[lang])
        try:
            await self.setup_webhook(channel)
        except discord.HTTPException:
            await channel.delete()
            raise
        return channel

    async def arrange(self, channels: list[discord.TextChannel]) -> None:
        """Place channels[1:] directly below channels[0], in the given order."""
        first = channels[0]
        moved = {c.id for c in channels[1:]}
        siblings = sorted(
            (
                c
                for c in first.guild.channels
                if c.category_id == first.category_id
                and c._sorting_bucket == first._sorting_bucket
                and c.id not in moved
            ),
            key=lambda c: (c.position, c.id),
        )
        index = next(i for i, c in enumerate(siblings) if c.id == first.id) + 1
        ordered = siblings[:index] + channels[1:] + siblings[index:]
        payload = [{"id": c.id, "position": i} for i, c in enumerate(ordered)]
        await self.http.bulk_channel_update(first.guild.id, payload)

    async def add_missing_korean_channels(self) -> None:
        for cluster in self.store.clusters:
            if "ko" in cluster or "ja" not in cluster or "en" not in cluster:
                continue
            ja = await self.resolve_channel(cluster["ja"])
            en = await self.resolve_channel(cluster["en"])
            if ja is None or en is None:
                continue
            base = ja.name.removesuffix(SUFFIXES["ja"])[:MAX_CHANNEL_BASE]
            try:
                ko = await self.create_language_channel(en, base, "ko")
            except (TranslationError, discord.HTTPException):
                log.exception("failed to add a Korean channel for %s", ja.id)
                continue
            cluster["ko"] = ko.id
            self.store.save()
            self.reindex()
            try:
                if ja.category_id == en.category_id:
                    await self.arrange([ja, en, ko])
                else:
                    await self.arrange([en, ko])
            except discord.HTTPException:
                log.exception("failed to reorder channels for %s", ja.id)
            log.info("added Korean channel %s to cluster of %s", ko.id, ja.id)

    async def create_tr_channel(self, interaction: discord.Interaction) -> None:
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
            return
        if channel.id in self.lang_of:
            await interaction.response.send_message("このチャンネルはすでに紐付いています。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)

        base = channel.name.removesuffix(SUFFIXES["ja"])[:MAX_CHANNEL_BASE]
        created: list[discord.TextChannel] = []
        try:
            for lang in ("en", "ko"):
                created.append(await self.create_language_channel(channel, base, lang))
            await self.setup_webhook(channel)
            await self.arrange([channel, *created])
            if channel.name != base + SUFFIXES["ja"]:
                await channel.edit(name=base + SUFFIXES["ja"], reason="/create-tr-channel")
        except (TranslationError, discord.HTTPException) as e:
            log.exception("failed to create translation channels for %s", channel.id)
            for new_channel in created:
                try:
                    await new_channel.delete(reason="/create-tr-channel failed")
                except discord.HTTPException:
                    pass
            reason = (
                "チャンネル名の翻訳に失敗しました"
                if isinstance(e, TranslationError)
                else f"Discord がエラーを返しました（HTTP {e.status}）。Bot に「チャンネルの管理」と「Webhook の管理」の権限があるか確認してください"
            )
            await interaction.followup.send(f"翻訳チャンネルを作成できませんでした。{reason}。")
            return

        en, ko = created
        self.store.add({"ja": channel.id, "en": en.id, "ko": ko.id})
        self.reindex()
        await interaction.followup.send(
            f"{en.mention} と {ko.mention} を作成し、{channel.mention} と紐付けました。"
        )

    async def add_translated_bot(self, interaction: discord.Interaction, bot: discord.User) -> None:
        if bot.id == self.user.id:
            message = "この Bot 自身は指定できません（翻訳結果を翻訳し続けてしまうため）。"
        elif not bot.bot:
            message = f"{bot.mention} は Bot ではありません。ユーザーの発言は常に翻訳されます。"
        elif bot.id in self.bots.ids:
            message = f"{bot.mention} はすでに翻訳対象です。"
        else:
            self.bots.add(bot.id)
            message = f"{bot.mention} の発言を翻訳するようにしました。"
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def remove_translated_bot(self, interaction: discord.Interaction, bot: discord.User) -> None:
        if bot.id in self.bots.ids:
            self.bots.remove(bot.id)
            message = f"{bot.mention} の発言を翻訳しないようにしました。"
        else:
            message = f"{bot.mention} は翻訳対象ではありません。"
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def list_translated_bots(self, interaction: discord.Interaction) -> None:
        if self.bots.ids:
            message = "発言を翻訳する Bot:\n" + "\n".join(f"- <@{bot_id}>" for bot_id in sorted(self.bots.ids))
        else:
            message = "発言を翻訳する Bot はありません。"
        await interaction.response.send_message(message, ephemeral=True, allowed_mentions=NO_MENTIONS)

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        if channel.id not in self.lang_of:
            return
        self.webhooks.pop(channel.id, None)
        self.store.remove_channel(channel.id)
        self.reindex()
        log.info("removed deleted channel %s from its cluster", channel.id)

    async def close(self) -> None:
        await self.translator.aclose()
        await super().close()

    def targets_for(self, channel_id: int) -> list[tuple[int, str]]:
        return [
            (cid, lang)
            for lang, cid in self.cluster_of[channel_id].items()
            if cid != channel_id and cid in self.webhooks
        ]

    def is_target(self, message: discord.Message) -> bool:
        return (
            message.channel.id in self.lang_of
            and message.channel.id in self.webhooks
            and message.type in TRANSLATED_TYPES
            and (self.is_human(message) or self.is_translated_bot(message))
        )

    def is_human(self, message: discord.Message) -> bool:
        return not message.author.bot and message.webhook_id is None

    def is_translated_bot(self, message: discord.Message) -> bool:
        # Webhook posts use the webhook's ID as the author, so the bot's own translations never match.
        # Slash command responses carry a webhook_id but are authored by the bot user itself.
        return message.author.id in self.bots.ids and message.author.id != self.user.id

    def counterpart(self, message_id: int, channel_id: int) -> int | None:
        source_id = message_id if message_id in self.mirrors else self.origins.get(message_id)
        mirror = self.mirrors.get(source_id) if source_id is not None else None
        if mirror is None:
            return None
        if mirror.source_channel_id == channel_id:
            return source_id
        ids = mirror.targets.get(channel_id)
        return ids[0] if ids else None

    def reply_link(self, message: discord.Message, target_channel_id: int, lang: str) -> str:
        if message.reference is None or message.reference.message_id is None:
            return ""
        counterpart_id = self.counterpart(message.reference.message_id, target_channel_id)
        if counterpart_id is None:
            return ""
        url = f"https://discord.com/channels/{message.guild.id}/{target_channel_id}/{counterpart_id}"
        return f"-# ↪ [{REPLY_LABEL[lang]}]({url})\n"

    async def build_chunks(self, message: discord.Message, target_channel_id: int, lang: str) -> list[str]:
        parts = []
        if message.content.strip():
            parts.append(await self.translator.translate(message.content, lang))
        parts.extend(attachment.url for attachment in message.attachments)
        if not parts:
            return []
        return split_message(self.reply_link(message, target_channel_id, lang) + "\n".join(parts))

    async def send_chunks(self, message: discord.Message, target_channel_id: int, chunks: list[str]) -> list[int]:
        webhook = self.webhooks[target_channel_id]
        ids = []
        for chunk in chunks:
            sent = await webhook.send(
                chunk,
                username=webhook_username(message.author),
                avatar_url=message.author.display_avatar.url,
                allowed_mentions=NO_MENTIONS,
                wait=True,
            )
            ids.append(sent.id)
        return ids

    async def deliver(self, message: discord.Message, target_channel_id: int, lang: str) -> list[int] | None:
        try:
            chunks = await self.build_chunks(message, target_channel_id, lang)
            return await self.send_chunks(message, target_channel_id, chunks) if chunks else []
        except (TranslationError, discord.HTTPException):
            log.exception("failed to translate message %s into %s", message.id, lang)
            return None

    async def resync(
        self, message: discord.Message, target_channel_id: int, lang: str, old_ids: list[int]
    ) -> list[int]:
        webhook = self.webhooks.get(target_channel_id)
        if webhook is None:
            return old_ids
        try:
            chunks = await self.build_chunks(message, target_channel_id, lang)
            if len(chunks) == len(old_ids):
                for target_id, chunk in zip(old_ids, chunks):
                    await webhook.edit_message(target_id, content=chunk, allowed_mentions=NO_MENTIONS)
                return old_ids
            for target_id in old_ids:
                try:
                    await webhook.delete_message(target_id)
                except discord.NotFound:
                    pass
            return await self.send_chunks(message, target_channel_id, chunks) if chunks else []
        except (TranslationError, discord.HTTPException):
            log.exception("failed to update translation of message %s in %s", message.id, lang)
            return old_ids

    def remember(self, source_id: int, mirror: Mirror) -> None:
        self.mirrors[source_id] = mirror
        self.mirrors.move_to_end(source_id)
        for ids in mirror.targets.values():
            for target_id in ids:
                self.origins[target_id] = source_id
        while len(self.mirrors) > MAX_TRACKED:
            _, old = self.mirrors.popitem(last=False)
            for ids in old.targets.values():
                for target_id in ids:
                    self.origins.pop(target_id, None)

    def forget(self, source_id: int) -> Mirror | None:
        mirror = self.mirrors.pop(source_id, None)
        if mirror:
            for ids in mirror.targets.values():
                for target_id in ids:
                    self.origins.pop(target_id, None)
        return mirror

    async def mark_failed(self, message: discord.Message) -> None:
        try:
            await message.add_reaction("⚠️")
        except discord.HTTPException:
            pass

    async def on_message(self, message: discord.Message) -> None:
        if not self.is_target(message):
            return
        targets = self.targets_for(message.channel.id)
        async with self.locks[message.channel.id]:
            results = await asyncio.gather(
                *(self.deliver(message, cid, lang) for cid, lang in targets)
            )
            sent = {cid: ids for (cid, _), ids in zip(targets, results) if ids}
            if sent:
                self.remember(message.id, Mirror(message.channel.id, message.content, sent))
            if any(ids is None for ids in results):
                await self.mark_failed(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if payload.message_id not in self.mirrors or payload.channel_id not in self.lang_of:
            return
        async with self.locks[payload.channel_id]:
            mirror = self.mirrors.get(payload.message_id)
            if mirror is None:
                return
            try:
                channel = self.get_channel(payload.channel_id)
                message = await channel.fetch_message(payload.message_id)
            except discord.HTTPException:
                log.exception("failed to fetch edited message %s", payload.message_id)
                return
            if message.content == mirror.source_content:
                return
            targets = [(cid, ids) for cid, ids in mirror.targets.items() if cid in self.lang_of]
            results = await asyncio.gather(
                *(self.resync(message, cid, self.lang_of[cid], ids) for cid, ids in targets)
            )
            updated = {cid: ids for (cid, _), ids in zip(targets, results) if ids}
            self.forget(payload.message_id)
            if updated:
                self.remember(payload.message_id, Mirror(payload.channel_id, message.content, updated))

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        await self.delete_mirror(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        for message_id in payload.message_ids:
            await self.delete_mirror(message_id)

    async def delete_mirror(self, source_id: int) -> None:
        mirror = self.forget(source_id)
        if mirror is None:
            return
        for channel_id, ids in mirror.targets.items():
            webhook = self.webhooks.get(channel_id)
            if webhook is None:
                continue
            for target_id in ids:
                try:
                    await webhook.delete_message(target_id)
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    log.exception("failed to delete translated message %s", target_id)


def main() -> None:
    load_dotenv()
    required = ["DISCORD_BOT_TOKEN", "LLM_API_KEY", "LLM_MODEL"]
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        sys.exit(f"missing settings in .env: {', '.join(missing)}")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    pairs_path = Path(os.getenv("PAIRS_PATH", "pairs.json"))
    store = ClusterStore(pairs_path)
    bots = BotStore(Path(os.getenv("BOTS_PATH", pairs_path.with_name("bots.json"))))
    ja_id, en_id = os.getenv("JA_CHANNEL_ID"), os.getenv("EN_CHANNEL_ID")
    if ja_id and en_id and not store.contains(int(ja_id)):
        store.add({"ja": int(ja_id), "en": int(en_id)})

    translator = Translator(
        base_url=os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"),
        api_key=os.environ["LLM_API_KEY"],
        model=os.environ["LLM_MODEL"],
        timeout=float(os.getenv("LLM_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
    )
    bot = TranslateBot(store, bots, translator)
    bot.run(os.environ["DISCORD_BOT_TOKEN"], log_handler=None)


if __name__ == "__main__":
    main()
