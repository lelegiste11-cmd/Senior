# config.py
"""
Configuration BACCARAT AI 🤖
Définissez toutes les variables d environnement ci-dessous sur votre plateforme
avant de lancer le bot.

Variables obligatoires :
  - ADMIN_ID
  - PREDICTION_CHANNEL_ID
  - API_ID
  - API_HASH
  - BOT_TOKEN

Variables optionnelles :
  - TELEGRAM_SESSION   (laisser vide pour session automatique)
  - PORT               (defaut : 10000)
  - API_POLL_INTERVAL  (defaut : 5)
  - COMPTEUR2_ACTIVE   (defaut : true)
  - COMPTEUR2_B        (defaut : 4)
  - COMPTEUR3_ACTIVE   (defaut : true)
  - COMPTEUR3_SEUIL    (defaut : 3)
  - COMPTEUR4_ACTIVE   (defaut : true)
  - COMPTEUR4_JJ       (defaut : 2)
"""

import os

def parse_channel_id(value: str) -> int:
    try:
        channel_id = int(value)
        if channel_id > 0 and len(str(channel_id)) >= 10:
            channel_id = -channel_id
        return channel_id
    except:
        raise ValueError(f"ID de canal invalide : {value}")

# ============================================================================
# VARIABLES D ENVIRONNEMENT - OBLIGATOIRES
# ============================================================================

ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
PREDICTION_CHANNEL_ID = parse_channel_id(os.getenv("PREDICTION_CHANNEL_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "")

# ============================================================================
# PARAMETRES DU BOT
# ============================================================================

# Port du serveur health check — prend le port de la plateforme, sinon 10000
PORT = int(os.getenv("PORT", "10000"))
API_POLL_INTERVAL = int(os.getenv("API_POLL_INTERVAL", "5"))

# Compteur2 — absences consecutives
COMPTEUR2_ACTIVE = os.getenv("COMPTEUR2_ACTIVE", "true").lower() == "true"
COMPTEUR2_B = int(os.getenv("COMPTEUR2_B", "4"))

# Compteur3 — apparences consecutives de l inverse (active par defaut)
COMPTEUR3_ACTIVE = os.getenv("COMPTEUR3_ACTIVE", "true").lower() == "true"
COMPTEUR3_SEUIL = int(os.getenv("COMPTEUR3_SEUIL", "3"))

# Compteur4 — paires inverses absentes ensemble (active par defaut)
COMPTEUR4_ACTIVE = os.getenv("COMPTEUR4_ACTIVE", "true").lower() == "true"
COMPTEUR4_JJ = int(os.getenv("COMPTEUR4_JJ", "2"))

# ============================================================================
# CONSTANTES — NE PAS MODIFIER
# ============================================================================

ALL_SUITS = ["♠", "♥", "♦", "♣"]

SUIT_DISPLAY = {
    "♠": "♠️",
    "♥": "❤️",
    "♦": "♦️",
    "♣": "♣️"
}

SUIT_INVERSE = {
    "♠": "♦",
    "♦": "♠",
    "♥": "♣",
    "♣": "♥",
}
