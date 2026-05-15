import discord
from discord.ext import commands
from discord import app_commands
import json
import os
import time
from collections import defaultdict

# ──────────────────────────────────────────────
#  CONFIG — all IDs
# ──────────────────────────────────────────────

QUARANTINE_ROLE_ID  = 1503924411797999659   # role applied when someone is quarantined
TRUSTED_ROLE_ID     = 1503796620788109324   # can use /whitelist & /removequar; immune to everything
TRANSCRIPT_CHAN_ID  = 1503924739176140912   # channel where all antinuke logs are sent
QUARANTINE_CAT_ID   = 1503924954788397228   # category where quarantine ticket channels are created

# Ping flood thresholds
PING_LIMIT  = 10   # max pings allowed…
PING_WINDOW = 25   # …within this many seconds

WHITELIST_FILE = "whitelist.json"

# ──────────────────────────────────────────────
#  PERSISTENCE
# ──────────────────────────────────────────────

def load_whitelist() -> set:
    if os.path.exists(WHITELIST_FILE):
        with open(WHITELIST_FILE, "r") as f:
            return set(int(x) for x in json.load(f))
    return set()


def save_whitelist(wl: set):
    with open(WHITELIST_FILE, "w") as f:
        json.dump(list(wl), f)


# ──────────────────────────────────────────────
#  BOT SETUP
# ──────────────────────────────────────────────

intents = discord.Intents.default()
intents.members         = True
intents.message_content = True
intents.guilds          = True

bot = commands.Bot(command_prefix="!", intents=intents)

whitelist:    set[int]        = load_whitelist()
ping_tracker: dict[int, list] = defaultdict(list)


# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────

def is_whitelisted(user_id: int) -> bool:
    return user_id in whitelist


def has_trusted_role(member: discord.Member) -> bool:
    if is_whitelisted(member.id):
        return True
    return any(r.id == TRUSTED_ROLE_ID for r in member.roles)


async def log_action(guild: discord.Guild, embed: discord.Embed):
    """Send an embed to the transcript/log channel."""
    ch = guild.get_channel(TRANSCRIPT_CHAN_ID)
    if ch:
        try:
            await ch.send(embed=embed)
        except discord.Forbidden:
            pass


async def create_quarantine_ticket(member: discord.Member, reason: str):
    """
    Open a private channel inside the quarantine category so staff
    can see exactly why someone was quarantined.
    """
    guild    = member.guild
    category = guild.get_channel(QUARANTINE_CAT_ID)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        member:             discord.PermissionOverwrite(view_channel=True, send_messages=False),
        guild.me:           discord.PermissionOverwrite(view_channel=True, send_messages=True),
    }

    trusted_role = guild.get_role(TRUSTED_ROLE_ID)
    if trusted_role:
        overwrites[trusted_role] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True
        )

    chan_name = f"quar-{member.name[:20].lower().replace(' ', '-')}"

    try:
        ticket = await guild.create_text_channel(
            name=chan_name,
            category=category,
            overwrites=overwrites,
            reason=f"Quarantine ticket for {member} — {reason}"
        )
        await ticket.send(
            f"🔒 **{member.mention}** has been quarantined.\n"
            f"**Reason:** {reason}\n\n"
            f"Staff: use `/removequar` to lift the quarantine when resolved."
        )
    except discord.Forbidden:
        print(f"[antinuke] Could not create ticket channel — missing permissions.")


async def quarantine_member(member: discord.Member, reason: str):
    """Strip all roles, apply quarantine role, log to transcript, open ticket."""
    guild = member.guild

    qrole = guild.get_role(QUARANTINE_ROLE_ID)
    if qrole is None:
        print(f"[antinuke] ERROR: Quarantine role {QUARANTINE_ROLE_ID} not found!")
        return

    if qrole in member.roles:
        return  # already quarantined

    roles_to_remove = [r for r in member.roles if r != guild.default_role]
    try:
        await member.remove_roles(*roles_to_remove, reason=reason)
        await member.add_roles(qrole, reason=reason)
    except discord.Forbidden:
        print(f"[antinuke] Missing permissions to manage roles for {member}")
        return

    # Log embed
    embed = discord.Embed(
        title="🔒 Member Quarantined",
        colour=discord.Colour.red(),
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="User",   value=f"{member.mention} (`{member.id}`)", inline=False)
    embed.add_field(name="Reason", value=reason,                               inline=False)
    embed.set_thumbnail(url=member.display_avatar.url)
    await log_action(guild, embed)

    # Open ticket
    await create_quarantine_ticket(member, reason)


# ──────────────────────────────────────────────
#  EVENT: BOT INVITE DETECTION
# ──────────────────────────────────────────────

@bot.event
async def on_member_join(member: discord.Member):
    if not member.bot:
        return

    guild = member.guild

    try:
        async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.bot_add):
            if entry.target.id != member.id:
                continue

            inviter = entry.user
            if inviter is None:
                break

            if is_whitelisted(inviter.id):
                return

            inviter_member = guild.get_member(inviter.id)
            if inviter_member and has_trusted_role(inviter_member):
                return

            if inviter_member:
                await quarantine_member(
                    inviter_member,
                    reason=f"Invited an unauthorized bot ({member.name} `{member.id}`) without permission."
                )
            break

    except discord.Forbidden:
        print("[antinuke] Missing 'View Audit Log' permission.")


# ──────────────────────────────────────────────
#  EVENT: MESSAGE (ping spam + @everyone abuse)
# ──────────────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return

    author = message.author

    if is_whitelisted(author.id):
        await bot.process_commands(message)
        return

    now = time.time()

    # ── @everyone / @here without trusted role ───
    if message.mention_everyone and not has_trusted_role(author):
        try:
            await message.delete()
        except discord.Forbidden:
            pass
        await quarantine_member(
            author,
            reason="Used @everyone / @here without the required role."
        )
        await bot.process_commands(message)
        return

    # ── Ping flood ───────────────────────────────
    ping_count = len(message.mentions) + len(message.role_mentions)
    if ping_count > 0:
        ping_tracker[author.id].append((now, ping_count))
        ping_tracker[author.id] = [
            (t, c) for t, c in ping_tracker[author.id] if now - t <= PING_WINDOW
        ]
        total = sum(c for _, c in ping_tracker[author.id])

        if total >= PING_LIMIT:
            ping_tracker[author.id] = []
            try:
                await message.delete()
            except discord.Forbidden:
                pass
            await quarantine_member(
                author,
                reason=f"Ping spam: {total} pings sent in under {PING_WINDOW} seconds."
            )
            await bot.process_commands(message)
            return

    await bot.process_commands(message)


# ──────────────────────────────────────────────
#  SLASH COMMANDS
# ──────────────────────────────────────────────

@bot.tree.command(name="whitelist", description="Make a user immune to all antinuke actions.")
@app_commands.describe(user="The user to whitelist")
async def cmd_whitelist(interaction: discord.Interaction, user: discord.Member):
    if not has_trusted_role(interaction.user):
        await interaction.response.send_message("❌ You don't have permission to use this.", ephemeral=True)
        return

    whitelist.add(user.id)
    save_whitelist(whitelist)

    embed = discord.Embed(
        title="✅ User Whitelisted",
        description=f"{user.mention} (`{user.id}`) is now immune to all antinuke actions.",
        colour=discord.Colour.green(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {interaction.user}")
    await interaction.response.send_message(embed=embed)
    await log_action(interaction.guild, embed)


@bot.tree.command(name="removequar", description="Remove the quarantine role from a user.")
@app_commands.describe(user="The user to unquarantine")
async def cmd_removequar(interaction: discord.Interaction, user: discord.Member):
    if not has_trusted_role(interaction.user):
        await interaction.response.send_message("❌ You don't have permission to use this.", ephemeral=True)
        return

    guild = interaction.guild
    qrole = guild.get_role(QUARANTINE_ROLE_ID)

    if qrole is None or qrole not in user.roles:
        await interaction.response.send_message(
            f"ℹ️ **{user.display_name}** doesn't have the quarantine role.", ephemeral=True
        )
        return

    await user.remove_roles(qrole, reason=f"Quarantine lifted by {interaction.user}")

    embed = discord.Embed(
        title="🔓 Quarantine Lifted",
        description=f"{user.mention} (`{user.id}`) has been unquarantined.",
        colour=discord.Colour.blurple(),
        timestamp=discord.utils.utcnow()
    )
    embed.set_footer(text=f"By {interaction.user}")
    await interaction.response.send_message(embed=embed)
    await log_action(guild, embed)


# ──────────────────────────────────────────────
#  READY
# ──────────────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Logged in as {bot.user} ({bot.user.id})")
    print(f"   Quarantine role : {QUARANTINE_ROLE_ID}")
    print(f"   Trusted role    : {TRUSTED_ROLE_ID}")
    print(f"   Transcript chan : {TRANSCRIPT_CHAN_ID}")
    print(f"   Quarantine cat  : {QUARANTINE_CAT_ID}")
    print(f"   Whitelist loaded: {whitelist}")


# ──────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise ValueError("Set the DISCORD_TOKEN environment variable before running!")

bot.run(TOKEN)
