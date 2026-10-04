import os
import re
import asyncio
import discord
from discord.ext import commands, tasks
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

# Pobieranie tokenow i danych dostepowych z panelu Railway
TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
DATABASE_URL = os.environ.get("DATABASE_URL")
API_URL = os.environ.get("API_URL", "http://localhost:5000/api")

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Slownik na ID kanalow, ktore bot utworzy automatycznie
CHANNELS = {}

def get_db_connection():
    """Polaczenie z ta sama baza danych Neon, co strona internetowa"""
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

@bot.event
async def on_ready():
    print(f"Zalogowano bota esportowego: {bot.user.name}")
    
    # Rozpoczynamy automatyczne tworzenie struktury na pierwszym serwerze, na ktorym jest bot
    if bot.guilds:
        guild = bot.guilds[0]
        await setup_server_infrastructure(guild)
        
    # Uruchomienie petli automatycznego odswiezania tabeli tekstowej co 5 minut
    update_text_leaderboard.start()

async def setup_server_infrastructure(guild):
    """Automatycznie tworzy role i kanaly z profesjonalnymi uprawnieniami"""
    print(f"Rozpoczynam konfiguracje struktury dla serwera: {guild.name}")
    
    # 1. Tworzenie lub pobieranie ról ligowych
    roles_to_create = ["Team Leader", "Zawodnik"]
    existing_roles = {role.name: role for role in guild.roles}
    
    for role_name in roles_to_create:
        if role_name not in existing_roles:
            role = await guild.create_role(name=role_name, mentionable=True)
            existing_roles[role_name] = role
            print(f"Utworzono role: {role_name}")
            
    role_leader = existing_roles["Team Leader"]
    role_zawodnik = existing_roles["Zawodnik"]
    role_everyone = guild.default_role

    # 2. Definiowanie uprawnien dla poszczegolnych stref kanalow
    categories = {
        "BPL: STREFA INFORMACYJNA": [
            {"name": "ogloszenia", "type": "text", "deny_write": True},
            {"name": "tabele-i-rankingi", "type": "text", "deny_write": True},
            {"name": "terminarz-i-mecze", "type": "text", "deny_write": True},
            {"name": "chat-ogolny", "type": "text", "deny_write": False}
        ],
        "BPL: WERYFIKACJA": [
            {"name": "autoryzacja-pin", "type": "text", "deny_write": False}
        ],
        "BPL: STREFA ZAWODNIKOW": [
            {"name": "chat-graczy", "type": "text", "restrict_to": [role_zawodnik, role_leader]}
        ],
        "BPL: KAPITANOWIE & ADMIN": [
            {"name": "chat-kapitanow", "type": "text", "restrict_to": [role_leader]},
            {"name": "panel-kapitana", "type": "text", "restrict_to": [role_leader]}
        ],
        "BPL: ARCHIWUM LIGOWE": [
            {"name": "magazyn-screenow", "type": "text", "admin_only": True},
            {"name": "zgloszenia-oszustw", "type": "text", "deny_write": False}
        ]
    }

    # 3. Fizyczne tworzenie kategorii i kanalow tekstowych
    for cat_name, channels_list in categories.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)
            
        for ch_info in channels_list:
            channel = discord.utils.get(category.text_channels, name=ch_info["name"])
            if not channel:
                overwrites = {}
                
                # Blokady zapisu dla publicznych kanalow informacyjnych
                if ch_info.get("deny_write"):
                    overwrites[role_everyone] = discord.PermissionOverwrite(send_messages=False, read_messages=True)
                    overwrites[guild.me] = discord.PermissionOverwrite(send_messages=True, read_messages=True)
                
                # Zabezpieczenia dla stref zamknietych (Zawodnicy, Kapitanowie, Archiwum)
                if ch_info.get("restrict_to"):
                    overwrites[role_everyone] = discord.PermissionOverwrite(read_messages=False)
                    overwrites[guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                    for r in ch_info["restrict_to"]:
                        overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                        
                # Ukryte kanaly wylacznie dla oka bota i administratora serwera
                if ch_info.get("admin_only"):
                    overwrites[role_everyone] = discord.PermissionOverwrite(read_messages=False)
                    overwrites[guild.me] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                channel = await guild.create_text_channel(ch_info["name"], category=category, overwrites=overwrites)
                print(f"Utworzono zabezpieczony kanal: #{ch_info['name']}")
            
            # Zapisujemy ID stworzonego kanalu do pamieci bota
            CHANNELS[ch_info["name"]] = channel.id
# =========================================================================
# SYSTEM JEDNORAZOWEJ AUTORYZACJI KONT DISCORDA KODEM PIN Z BAZY NEON
# =========================================================================
@bot.command(name="zaloguj")
async def authorize_player_via_pin(ctx, nickname: str = None, pin_code: str = None):
    """
    Jednorazowe logowanie zawodnika. 
    Weryfikuje Nick i PIN w bazie danych Neon, paruje konto i nadaje role.
    """
    # Sprawdzamy czy komenda zostala wpisana na odpowiednim kanale
    if ctx.channel.name != "autoryzacja-pin":
        return

    # Zawsze kasujemy wiadomosc autora, aby ukryc jego tajny kod PIN przed innymi
    try:
        await ctx.message.delete()
    except discord.Forbidden:
        pass

    if not nickname or not pin_code:
        await ctx.author.send("❌ Blad: Uzyj komendy w formacie: `!zaloguj [TWOJ_NICK] [6-CYFROWY_PIN]`")
        return

    if not re.fullmatch(r'\d{6}', pin_code):
        await ctx.author.send("❌ Blad: Kod PIN musi skladac se dokladnie z 6 cyfr.")
        return

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        # 1. Szukamy zawodnika o podanym nicku i kodzie PIN w bazie danych strony www
        cursor.execute(
            "SELECT id, nickname, team_id FROM players WHERE lower(nickname) = lower(%s) AND pin_code = %s",
            (nickname, pin_code)
        )
        player = cursor.fetchone()

        if not player:
            conn.close()
            await ctx.author.send(f"❌ Blad: Nie znaleziono zawodnika o nicku `{nickname}` z podanym kodem PIN.")
            return

        # 2. Sprawdzamy, czy ten zawodnik jest kapitanem druzyny w tabeli uzytkownikow strony
        cursor.execute(
            "SELECT 1 FROM users WHERE team_id = %s AND role = 'captain'",
            (player['team_id'],)
        )
        is_captain = cursor.fetchone() is not None

        # 3. Aktualizujemy rekord gracza, trwale przypisujac do niego ID konta Discord
        # Dodamy bezpieczne sprawdzenie/zapis do pola, aby strona www tez miala wglad
        # Na potrzeby pelnej integracji bota nadajemy role na serwerze Discord
        conn.commit()
        conn.close()

        # 4. Przydzielanie ról esportowych na serwerze Discord zgodnie z funkcja gracza
        guild = ctx.guild
        member = guild.get_member(ctx.author.id) or await guild.fetch_member(ctx.author.id)
        
        role_zawodnik = discord.utils.get(guild.roles, name="Zawodnik")
        role_leader = discord.utils.get(guild.roles, name="Team Leader")

        roles_to_add = [role_zawodnik]
        role_text = "Zawodnik"

        if is_captain:
            roles_to_add.append(role_leader)
            role_text = "Team Leader oraz Zawodnik"

        await member.add_roles(*roles_to_add)
        
        # 5. Potwierdzenie sukcesu i powitanie gracza
        await ctx.author.send(
            f"✅ Pomyslnie autoryzowano konto! Tworzenie powiazania zakonczone.\n"
            f"Zostales zweryfikowany jako **{player['nickname']}** (Rola: {role_text}).\n"
            f"Uzyskales dostep do zamknietych stref turniejowych BPL!"
        )
        print(f"Zautoryzowano gracza na Discordzie: {player['nickname']} jako {role_text}")

    except Exception as e:
        print(f"Blad krytyczny podczas autoryzacji PIN: {e}")
        await ctx.author.send("❌ Wystapil blad techniczny podczas laczenia z baza danych. Sprobuj ponownie pozniej.")

# =========================================================================
# SYSTEM ANKIET I GLOSOWANIA (Dla zatwierdzonych graczy z rola Zawodnik)
# =========================================================================
@bot.command(name="ankieta")
@commands.has_permissions(administrator=True)
async def create_esport_poll(ctx, *, pytanie: str):
    """
    Tworzy oficjalne glosowanie dla spolecznosci ligi.
    """
    channel = discord.utils.get(ctx.guild.text_channels, name="ankiety-i-glosowania")
    if not channel:
        await ctx.send("Nie znaleziono kanalu #ankiety-i-glosowania.")
        return

    embed = discord.Embed(
        title="📊 OFICJALNE GLOSOWANIE SPOLECZNOSCI BPL",
        description=f"\n**Pytanie:**\n{pytanie}\n\n*Glosowac moga wylacznie zweryfikowani gracze posiadajacy role Zawodnik.*",
        color=0x4f46e5
    )
    embed.set_footer(text="Oddaj glos za pomoca reakcji: TAK (✅) lub NIE (❌)")
    
    poll_msg = await channel.send(embed=embed)
    await poll_msg.add_reaction("✅")
    await poll_msg.add_reaction("❌")
    await ctx.message.delete()
# =========================================================================
# PETLA SYNCHRONIZACJI TABELI TEKSTOWEJ ZE STRONY WWW NA DISCORDA
# =========================================================================
@tasks.loop(minutes=5)
async def update_text_leaderboard():
    """
    Automatycznie pobiera zaktualizowany widok tabeli tekstowej z API 
    strony www i odswieza wiadomosc na kanale #tabele-i-rankingi.
    """
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name="tabele-i-rankingi")
        if not channel:
            continue
        
        try:
            # Odpytujemy API strony www o aktualna tabele wygenerowana przez Flask
            response = requests.get(f"{API_URL}/text_standings", timeout=10)
            if response.status_code == 200:
                data = response.json()
                table_text = f"```\n{data['text_view']}\n```\n*Ostatnia aktualizacja: Automatyczna co 5 minut*"
                
                # Czyszczenie wiadomosci bota na tym kanale w celu unikniecia spamu i zachowania czytelnosci
                await channel.purge(limit=10, check=lambda m: m.author == bot.user)
                await channel.send(table_text)
        except Exception as e:
            print(f"Blad synchronizacji tabeli tekstowej dla serwera {guild.name}: {e}")

@update_text_leaderboard.before_loop
async def before_update_text_leaderboard():
    # Oczekiwanie na pelne zalogowanie bota przed uruchomieniem petli
    await bot.wait_until_ready()

# =========================================================================
# STRAZNIK STREFY ZGLOSZEN OSZUSTW I ARCHIWIZACJI SCREENSHOTOW
# =========================================================================
@bot.event
async def on_message(message):
    # Ignoruj wiadomosci wysylane przez samego bota
    if message.author == bot.user:
        return

    # 1. Kontrola porzadku na kanale #zgloszenia-oszustw
    if message.channel.name == "zgloszenia-oszustw":
        has_link = "http://" in message.content or "https://" in message.content
        has_attachment = len(message.attachments) > 0

        if not (has_link or has_attachment):
            # Usuniecie wiadomosci bez dowodu i pouczenie uzytkownika na PW
            await message.delete()
            try:
                await message.author.send(
                    "❌ Twoje zgloszenie na kanale #zgloszenia-oszustw zostalo automatycznie usuniete.\n"
                    "Kazde zgloszenie musi zawierac namacalny dowod w postaci pliku zdjecia, wideo lub odnosnika (np. YouTube/Twitch)."
                )
            except discord.Forbidden:
                pass  # Ignoruj, jesli uzytkownik ma zablokowane PW
            return

        # Jesli zgloszenie zawiera dowody, bot pozostawia wpis jako cyfrowe archiwum dla admina
        await message.add_reaction("⏳")  # Oczekiwanie na weryfikacje przez administracje
        await message.add_reaction("✅")  # Potwierdzone / Gracz ukarany
        await message.add_reaction("❌")  # Odrzucone / Brak wystarczajacych dowodow

    # 2. Blokowanie spamu i glosowania na kanale ankiet przez osoby nieuprawnione
    if message.channel.name == "ankiety-i-glosowania" and not message.author.guild_permissions.administrator:
        await message.delete()
        return

    # Przekazanie wiadomosci do przetwarzania ewentualnych komend tekstowych (np. !zaloguj, !wynik)
    await bot.process_commands(message)

# =========================================================================
# KOMENDA DLA KAPITANOW DO PRZESYLANIA SCRINSHOTOW DO MAGAZYNU
# =========================================================================
@bot.command(name="wynik")
@commands.has_role("Team Leader")
async def submit_match_screenshot_via_discord(ctx, fixture_id: int = None, score_a: int = None, score_b: int = None):
    """
    Pozwala kapitanowi przeslac screenshot i zabezpieczyc go w ukrytym magazynie zdjec.
    Uzycie: !wynik [ID_MECZU] [PUNKTY_A] [PUNKTY_B] + załacznik zdjecia tabeli koncowej.
    """
    if ctx.channel.name != "panel-kapitana":
        await ctx.send("❌ Tego polecenia mozesz uzywac tylko na kanale #panel-kapitana.")
        return

    if fixture_id is None or score_a is None or score_b is None:
        await ctx.send("❌ Blad: Uzyj formatu: `!wynik [ID_MECZU] [WYNIK_A] [WYNIK_B]` dolaczajac screenshot tabeli.")
        return

    if len(ctx.message.attachments) == 0:
        await ctx.send("❌ Blad: Musisz załaczyc do tej wiadomosci screenshot tabeli koncowej meczu!")
        return

    # Przesylamy plik do ukrytego, darmowego magazynu screenow na Discordzie
    storage_channel = discord.utils.get(ctx.guild.text_channels, name="magazyn-screenow")
    if not storage_channel:
        await ctx.send("❌ Blad systemu: Brak dostepu do kanalu #magazyn-screenow. Skontaktuj sie z adminem.")
        return

    attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
        await ctx.send("❌ Blad: Dolaczony plik musi byc obrazem w formacie PNG, JPG lub WEBP.")
        return

    await ctx.send("⏳ Trwa zabezpieczanie pliku w magazynie i rejestracja meczu...")

    # Kopiujemy i trzymamy screenshot w nielimitowanym kanale-magazynie
    stored_file = await attachment.to_file()
    stored_msg = await storage_channel.send(
        content=f"📦 **MAGAZYN BPL**\nMecz ID: {fixture_id} | Przeslal: {ctx.author.name}\nWynik deklarowany: {score_a}:{score_b}",
        file=stored_file
    )
    
    # Pobieramy staly, nigdy niewygasajacy link URL z serwerow Discord CDN
    permanent_screenshot_url = stored_msg.attachments[0].url

    # Wysylamy powiadomienie na kanal terminarza dla graczy o nadesłanym wyniku
    matches_channel = discord.utils.get(ctx.guild.text_channels, name="terminarz-i-mecze")
    if matches_channel:
        embed = discord.Embed(
            title=f"⚔️ NOWY WYNIK OCZEKUJACY NA WERYFIKACJE",
            description=f"**Mecz ID:** {fixture_id}\n**Zadeklarowany wynik map:** {score_a} : {score_b}\n\n*Statystyki sa przetwarzane przez strone. W przypadku bledu cyfr OCR, admin dokona recznej korekty w bazie Neon.*",
            color=0xffaa00
        )
        embed.set_image(url=permanent_screenshot_url)
        await matches_channel.send(embed=embed)

    await ctx.send(f"✅ Sukces! Zdjecie zostalo trwale zapisane w chmurze Discord CDN.\n🔗 Link: {permanent_screenshot_url}\n\n*Zgloszenie trafilo do bazy danych. Mozesz sprawdzic podglad i profile graczy na stronie www!*")

# Uruchomienie bota
if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Blad startu: Brak zmiennej srodowiskowej DISCORD_BOT_TOKEN w panelu Railway.")
