import os
import discord
from discord.ext import commands, tasks
import requests

TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
API_URL = os.environ.get("API_URL", "http://localhost:5000/api/text_standings")
TARGET_CHANNEL_ID = 1556251556465086506 
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Zalogowano bota: {bot.user.name}")
    # Uruchomienie automatycznej petli synchronizacji tabeli tekstowej
    update_text_leaderboard.start()

@tasks.loop(minutes=5)
async def update_text_leaderboard():
    """
    Automatycznie pobiera dane z Flask i aktualizuje
    kompaktowa tabele tekstowa na wybranym kanale Discorda.
    """
    channel = bot.get_channel(TARGET_CHANNEL_ID)
    if not channel:
        print("Nie znaleziono kanalu do tabeli. Sprawdz TARGET_CHANNEL_ID.")
        return
    
    try:
        response = requests.get(API_URL)
        if response.status_code == 200:
            data = response.json()
            table_text = f"```\n{data['text_view']}\n```\n*Ostatnia aktualizacja: Automatyczna*"
            
            # Czyszczenie poprzednich wiadomosci bota, aby uniknac spamu
            await channel.purge(limit=5, check=lambda m: m.author == bot.user)
            # Wyslanie lekkiej, tekstowej tabeli
            await channel.send(table_text)
    except Exception as e:
        print(f"Blad synchronizacji tabeli tekstowej: {e}")
@bot.event
async def on_message(message):
    # Ignoruj wiadomosci wysylane przez bota samego do siebie
    if message.author == bot.user:
        return

    # SYSTEM ZGLOSZEN (Oszustwa, toksycznosc, glitche gry)
    # Sprawdzamy, czy nazwa kanalu to 'zgloszenia-oszustw'
    if message.channel.name == "zgloszenia-oszustw":
        # Sprawdzamy, czy gracz dolaczyl dowod (link lub wideo/obraz)
        has_link = "http://" in message.content or "https://" in message.content
        has_attachment = len(message.attachments) > 0

        if not (has_link or has_attachment):
            # Usuniecie wiadomosci bez dowodu i pouczenie uzytkownika na PW
            await message.delete()
            try:
                await message.author.send(
                    "X Twoje zgloszenie na kanale #zgloszenia-oszustw zostalo usuniete.\n"
                    "Kazde zgloszenie musi zawierac dowod w postaci pliku wideo, zdjecia lub odnosnika (np. YouTube/Twitch)."
                )
            except discord.Forbidden:
                pass  # Ignoruj, jesli uzytkownik ma zablokowane PW
            return

        # Jesli zgloszenie ma dowody, bot doda reakcje dla ulatwienia pracy administracji
        await message.add_reaction("⏳")  # Oczekiwanie na weryfikacje
        await message.add_reaction("✅")  # Zgloszenie potwierdzone / Gracz ukarany
        await message.add_reaction("❌")  # Zgloszenie odrzucone / Brak wystarczajacych dowodow

    # Przekazanie wiadomosci do przetwarzania ewentualnych komend tekstowych
    await bot.process_commands(message)

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Blad: Brak zmiennej srodowiskowej DISCORD_BOT_TOKEN.")
