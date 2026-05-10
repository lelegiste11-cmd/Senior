import re
import asyncio
import logging
import sys
import traceback
from typing import List, Optional, Dict
from datetime import datetime, timezone, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import ChatWriteForbiddenError, UserBannedInChannelError
from aiohttp import web

from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    PREDICTION_CHANNEL_ID, PORT, API_POLL_INTERVAL,
    ALL_SUITS, SUIT_DISPLAY, SUIT_INVERSE,
    COMPTEUR2_ACTIVE, COMPTEUR2_B, TELEGRAM_SESSION,
    COMPTEUR3_ACTIVE, COMPTEUR3_SEUIL,
    COMPTEUR4_ACTIVE, COMPTEUR4_JJ
)
from utils import get_latest_results

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

# ============================================================================
# VARIABLES GLOBALES
# ============================================================================

client = None
prediction_channel_ok = False
current_game_number = 0
last_prediction_time: Optional[datetime] = None

# Prédictions en attente de vérification {game_number: {...}}
pending_predictions: Dict[int, dict] = {}

# Compteur2 - absences consécutives par couleur (costumes du joueur)
compteur2_active = COMPTEUR2_ACTIVE
compteur2_b = COMPTEUR2_B
compteur2_absences: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur2_last_game = 0
compteur2_last_seen: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur2_processed_games: set = set()

# Compteur3 - apparences consécutives par couleur (costumes du joueur)
compteur3_active: bool = COMPTEUR3_ACTIVE
compteur3_seuil: int = COMPTEUR3_SEUIL
compteur3_appearances: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}
compteur3_last_appeared: Dict[str, int] = {suit: 0 for suit in ALL_SUITS}

# Compteur4 - absences consécutives des paires inverses ensemble
# Paire A : ♠ et ♦ absents ensemble | Paire B : ♥ et ♣ absents ensemble
compteur4_active: bool = COMPTEUR4_ACTIVE
compteur4_jj: int = COMPTEUR4_JJ
compteur4_pair_a: int = 0   # compteur paire ♠+♦ absents ensemble
compteur4_pair_b: int = 0   # compteur paire ♥+♣ absents ensemble
compteur4_last_game_pair_a: int = 0
compteur4_last_game_pair_b: int = 0

# Mode Attente - attend PERDU avant de prédire à nouveau
attente_mode = False
attente_locked = False

# Historique des prédictions
prediction_history: List[Dict] = []
MAX_HISTORY_SIZE = 100

# Jeux pour lesquels la main du joueur a déjà été traitée (compteur2)
player_processed_games: set = set()

# Cache des derniers résultats API {game_number: result_dict}
api_results_cache: Dict[int, dict] = {}

# Dernier numéro de jeu pour lequel une prédiction a été envoyée
last_prediction_game: int = 0

# Pour éviter de déclencher le reset plusieurs fois pour la partie 1440
reset_done_for_cycle: bool = False

# ============================================================================
# INTERVALLES HORAIRES - Prédictions autorisées (heure du Bénin = UTC+1)
# ============================================================================

BENIN_TZ = timezone(timedelta(hours=1))

# Liste des intervalles autorisés: [{"start": HH, "end": HH}, ...]
# Si la liste est vide, les prédictions sont toujours autorisées.
prediction_intervals: List[Dict[str, int]] = []
intervals_enabled: bool = False  # Désactivé par défaut (toujours autorisé)

def is_prediction_allowed_now() -> bool:
    """Vérifie si les prédictions sont autorisées à l'heure actuelle (heure Bénin)."""
    if not intervals_enabled or not prediction_intervals:
        return True
    now_benin = datetime.now(BENIN_TZ)
    current_hour = now_benin.hour
    current_minute = now_benin.minute
    current_total = current_hour * 60 + current_minute
    for interval in prediction_intervals:
        start_total = interval["start"] * 60
        end_total = interval["end"] * 60
        if start_total <= end_total:
            if start_total <= current_total < end_total:
                return True
        else:
            # Intervalle qui passe minuit (ex: 23h → 2h)
            if current_total >= start_total or current_total < end_total:
                return True
    return False

def get_intervals_status_text() -> str:
    now_benin = datetime.now(BENIN_TZ)
    status = "✅ ON" if intervals_enabled else "❌ OFF"
    allowed = "✅ OUI" if is_prediction_allowed_now() else "🚫 NON"
    lines = [
        f"⏰ **Intervalles de prédiction**",
        f"Mode restriction: {status}",
        f"Heure Bénin actuelle: {now_benin.strftime('%H:%M')}",
        f"Prédiction autorisée: {allowed}",
        "",
    ]
    if prediction_intervals:
        lines.append("Intervalles configurés:")
        for i, iv in enumerate(prediction_intervals, 1):
            lines.append(f"  {i}. {iv['start']:02d}h00 → {iv['end']:02d}h00")
    else:
        lines.append("Aucun intervalle défini (prédictions toujours autorisées si mode OFF)")
    return "\n".join(lines)

# ============================================================================
# UTILITAIRES - Costumes
# ============================================================================

def normalize_suit(suit_emoji: str) -> str:
    """Convertit un costume emoji (♠️) en costume simple (♠)."""
    return suit_emoji.replace('\ufe0f', '').replace('❤', '♥')

def player_suits_from_cards(player_cards: list) -> List[str]:
    """Extrait la liste des costumes uniques des cartes du joueur."""
    suits = set()
    for card in player_cards:
        raw = card.get('S', '')
        normalized = normalize_suit(raw)
        if normalized in ALL_SUITS:
            suits.add(normalized)
    return list(suits)

def has_player_cards(result: dict) -> bool:
    """Retourne True si le joueur a au moins 2 cartes (main prête)."""
    return len(result.get('player_cards', [])) >= 2

# ============================================================================
# UTILITAIRES - Canaux
# ============================================================================

def normalize_channel_id(channel_id) -> Optional[int]:
    if not channel_id:
        return None
    s = str(channel_id)
    if s.startswith('-100'):
        return int(s)
    if s.startswith('-'):
        return int(s)
    return int(f"-100{s}")

async def resolve_channel(entity_id):
    try:
        if not entity_id:
            return None
        normalized = normalize_channel_id(entity_id)
        entity = await client.get_entity(normalized)
        return entity
    except Exception as e:
        logger.error(f"❌ Impossible de résoudre le canal {entity_id}: {e}")
        return None

# ============================================================================
# HISTORIQUE DES PRÉDICTIONS
# ============================================================================

def add_prediction_to_history(game_number: int, suit: str, triggered_by_suit: str):
    global prediction_history
    prediction_history.insert(0, {
        'predicted_game': game_number,
        'suit': suit,
        'triggered_by': triggered_by_suit,
        'predicted_at': datetime.now(),
        'status': 'en_cours',
        'result_game': None,
        'silent': attente_mode,
    })
    if len(prediction_history) > MAX_HISTORY_SIZE:
        prediction_history = prediction_history[:MAX_HISTORY_SIZE]

def update_prediction_history_status(game_number: int, suit: str, status: str, result_game: int):
    for pred in prediction_history:
        if pred['predicted_game'] == game_number and pred['suit'] == suit:
            pred['status'] = status
            pred['result_game'] = result_game
            break

# ============================================================================
# ENVOI ET MISE À JOUR DES PRÉDICTIONS
# ============================================================================

async def send_prediction(game_number: int, suit: str, triggered_by_suit: str) -> Optional[int]:
    """Envoie une prédiction au canal."""
    global last_prediction_time, attente_locked, last_prediction_game

    # ── Vérification de l'intervalle horaire ──────────────────────────────────
    if not is_prediction_allowed_now():
        now_benin = datetime.now(BENIN_TZ)
        logger.info(
            f"⏰ Prédiction #{game_number} {suit} bloquée: hors intervalle autorisé "
            f"(heure Bénin: {now_benin.strftime('%H:%M')})"
        )
        return None

    if not PREDICTION_CHANNEL_ID:
        logger.error("❌ PREDICTION_CHANNEL_ID non configuré")
        return None

    prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
    if not prediction_entity:
        logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
        return None

    suit_display = SUIT_DISPLAY.get(suit, suit)
    msg = (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_number}  :{suit_display}\n\n"
        f"En cours de vérification"
    )

    try:
        sent = await client.send_message(prediction_entity, msg)
        last_prediction_time = datetime.now()
        last_prediction_game = game_number

        pending_predictions[game_number] = {
            'suit': suit,
            'triggered_by': triggered_by_suit,
            'message_id': sent.id,
            'status': 'en_cours',
            'awaiting_rattrapage': 0,
            'sent_time': datetime.now(),
        }

        add_prediction_to_history(game_number, suit, triggered_by_suit)

        if attente_mode:
            attente_locked = True

        logger.info(f"✅ Prédiction envoyée: #{game_number} {suit} (déclenché par absence {triggered_by_suit})")
        return sent.id

    except ChatWriteForbiddenError:
        logger.error(f"❌ Pas la permission d'écrire dans le canal {PREDICTION_CHANNEL_ID}")
        return None
    except UserBannedInChannelError:
        logger.error(f"❌ Bot banni du canal {PREDICTION_CHANNEL_ID}")
        return None
    except Exception as e:
        logger.error(f"❌ Erreur envoi prédiction: {e}")
        return None

async def update_prediction_message(game_number: int, status: str, trouve: bool, rattrapage: int = 0):
    """Met à jour le message de prédiction avec le résultat."""
    global attente_locked

    if game_number not in pending_predictions:
        return

    pred = pending_predictions[game_number]
    suit = pred['suit']
    msg_id = pred['message_id']
    suit_display = SUIT_DISPLAY.get(suit, suit)

    if status == '✅0️⃣':
        result_line = "Rattrapage :✅0️⃣"
        game_display = str(game_number)
    elif status == '✅1️⃣':
        result_line = "Rattrapage :✅1️⃣"
        game_display = str(game_number)
    elif status == '✅2️⃣':
        result_line = "Rattrapage :✅2️⃣"
        game_display = str(game_number)
    elif status == '✅3️⃣':
        result_line = "Rattrapage :✅3️⃣"
        game_display = str(game_number)
    else:
        result_line = "Rattrapage : ❌PERDU"
        game_display = f"#N{game_number}"

    new_msg = (
        f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲\n"
        f"Game {game_display}  :{suit_display}\n\n"
        f"{result_line}"
    )

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            logger.error("❌ Canal prédiction inaccessible pour mise à jour")
            return

        await client.edit_message(prediction_entity, msg_id, new_msg)
        pred['status'] = status

        status_key = 'gagne' if trouve else 'perdu'
        update_prediction_history_status(game_number, suit, status_key, game_number)

        if trouve:
            logger.info(f"✅ Gagné: #{game_number} {suit} ({status})")
        else:
            logger.info(f"❌ Perdu: #{game_number} {suit}")
            if attente_mode:
                attente_locked = False
                logger.info("🔓 Mode Attente: PERDU détecté → prêt pour prochaine prédiction")

        del pending_predictions[game_number]

    except Exception as e:
        logger.error(f"❌ Erreur update message: {e}")

# ============================================================================
# VÉRIFICATION DYNAMIQUE (dès que les cartes du joueur apparaissent)
# ============================================================================

async def check_prediction_result_dynamic(game_number: int, player_suits: List[str], is_finished: bool):
    """Vérification dynamique des prédictions.

    Règles :
    - Si le costume prédit apparaît dans les cartes du joueur → gagné immédiatement
      (même si la partie n'est pas encore totalement terminée côté banquier).
    - Si pas trouvé ET partie joueur terminée (is_finished) → avancer rattrapage.
    - Si pas trouvé ET partie encore en cours → ne rien faire, attendre le prochain poll.
    """

    # --- Vérification directe (jeu prédit = jeu en cours) ---
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        if pred.get('awaiting_rattrapage', 0) == 0:
            target_suit = pred['suit']

            if target_suit in player_suits:
                logger.info(f"🔍 [DYN] #{game_number}: {target_suit} ✅ trouvé chez joueur (en_cours={not is_finished})")
                await update_prediction_message(game_number, '✅0️⃣', True)
            elif is_finished:
                pred['awaiting_rattrapage'] = 1
                logger.info(f"🔍 [DYN] #{game_number}: {target_suit} ❌ absent → rattrapage #{game_number + 1}")
            else:
                logger.debug(f"🔍 [DYN] #{game_number}: partie en cours, costume pas encore visible - attente")
            return

    # --- Vérification rattrapages ---
    for original_game, pred in list(pending_predictions.items()):
        awaiting = pred.get('awaiting_rattrapage', 0)
        if awaiting <= 0:
            continue
        if game_number != original_game + awaiting:
            continue

        target_suit = pred['suit']

        if target_suit in player_suits:
            status = f'✅{awaiting}️⃣'
            logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target_suit} ✅ trouvé chez joueur")
            await update_prediction_message(original_game, status, True, awaiting)
        elif is_finished:
            if awaiting < 3:
                pred['awaiting_rattrapage'] = awaiting + 1
                logger.info(f"🔍 [DYN] R{awaiting} #{game_number}: {target_suit} ❌ absent → R{awaiting+1} #{original_game + awaiting + 1}")
            else:
                logger.info(f"🔍 [DYN] R3 #{game_number}: {target_suit} ❌ → prédiction perdue")
                await update_prediction_message(original_game, '❌', False)
        else:
            logger.debug(f"🔍 [DYN] R{awaiting} #{game_number}: partie en cours - attente")
        return

# ============================================================================
# COMPTEUR2 - Logique principale (costumes du joueur)
# ============================================================================

def get_compteur2_status_text() -> str:
    status = "✅ ON" if compteur2_active else "❌ OFF"
    last_game_str = f"#{compteur2_last_game}" if compteur2_last_game else "Aucun"

    lines = [
        f"📊 Compteur2: {status} | B={compteur2_b}",
        f"🎮 Dernier jeu reçu: {last_game_str}",
        f"🎯 Dernière prédiction: #{last_prediction_game}" if last_prediction_game else "🎯 Dernière prédiction: Aucune",
        "",
        "Progression des absences (cartes joueur):",
    ]

    for suit in ALL_SUITS:
        count = compteur2_absences.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur2_b - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur2_b}")

    if attente_mode:
        attente_status = "🔒 Verrouillé (attend PERDU)" if attente_locked else "🔓 Prêt"
        lines.append(f"\n🕐 Mode Attente: ✅ ON | {attente_status}")
    else:
        lines.append(f"\n🕐 Mode Attente: ❌ OFF")

    return "\n".join(lines)

def get_compteur3_status_text() -> str:
    status = "✅ ON" if compteur3_active else "❌ OFF"
    lines = [
        f"🔢 Compteur3: {status} | Seuil={compteur3_seuil}",
        "",
        "Progression des apparences consécutives (cartes joueur):",
    ]
    for suit in ALL_SUITS:
        count = compteur3_appearances.get(suit, 0)
        filled = '█' * count
        empty = '░' * max(0, compteur3_seuil - count)
        bar = f"[{filled}{empty}]"
        display = SUIT_DISPLAY.get(suit, suit)
        lines.append(f"{display} : {bar} {count}/{compteur3_seuil}")
    return "\n".join(lines)

def get_compteur4_status_text() -> str:
    status = "✅ ON" if compteur4_active else "❌ OFF"
    filled_a = '█' * compteur4_pair_a
    empty_a  = '░' * max(0, compteur4_jj - compteur4_pair_a)
    filled_b = '█' * compteur4_pair_b
    empty_b  = '░' * max(0, compteur4_jj - compteur4_pair_b)
    lines = [
        f"🔲 Compteur4: {status} | JJ={compteur4_jj}",
        "",
        "Absences consécutives des paires inverses :",
        f"♠️+♦️ : [{filled_a}{empty_a}] {compteur4_pair_a}/{compteur4_jj}",
        f"❤️+♣️ : [{filled_b}{empty_b}] {compteur4_pair_b}/{compteur4_jj}",
    ]
    return "\n".join(lines)

async def process_compteur2(game_number: int, player_suits: List[str]):
    """Traite le Compteur2 dès que le joueur a ses cartes (>= 2 cartes).

    Règles de prédiction :
    - Déclenché dès que le joueur a pris ses cartes (sans attendre la fin du banquier).
    - Attend qu'il n'y ait aucune prédiction en cours (pending_predictions vide).
    - L'écart entre la dernière prédiction envoyée et la nouvelle doit être ≥ 2.
    - Pas de prédiction pour deux numéros consécutifs.
    - Pas de prédiction pour le même numéro deux fois (même si costumes différents).
    - Si bloquée par intervalle horaire, le compteur n'est PAS réinitialisé.
    - Si compteur3 activé : vérifie les apparences consécutives de l'inverse pour
      décider si on prédit le manquant ou l'inverse.
    """
    global compteur2_absences, compteur2_last_game, compteur2_last_seen, compteur2_processed_games
    global compteur3_appearances, compteur3_last_appeared
    global compteur4_pair_a, compteur4_pair_b, compteur4_last_game_pair_a, compteur4_last_game_pair_b

    if not compteur2_active:
        return

    if game_number in compteur2_processed_games:
        return

    compteur2_processed_games.add(game_number)
    if len(compteur2_processed_games) > 200:
        oldest = min(compteur2_processed_games)
        compteur2_processed_games.discard(oldest)

    compteur2_last_game = game_number

    for suit in ALL_SUITS:
        last_seen = compteur2_last_seen.get(suit, 0)

        if suit in player_suits:
            # ── Compteur2 : reset absence ──────────────────────────────────────
            if compteur2_absences[suit] > 0:
                logger.info(f"📊 Compteur2 {suit}: trouvé au jeu #{game_number} (joueur) → reset (était {compteur2_absences[suit]})")
            compteur2_absences[suit] = 0
            compteur2_last_seen[suit] = game_number

            # ── Compteur3 : incrémenter apparences consécutives ────────────────
            last_app = compteur3_last_appeared.get(suit, 0)
            if last_app == 0 or game_number == last_app + 1:
                compteur3_appearances[suit] += 1
            else:
                compteur3_appearances[suit] = 1
            compteur3_last_appeared[suit] = game_number
            logger.debug(f"🔢 Compteur3 {suit}: apparence consécutive {compteur3_appearances[suit]}/{compteur3_seuil} (jeu #{game_number})")
        else:
            # ── Compteur3 : reset apparences ──────────────────────────────────
            if compteur3_appearances[suit] > 0:
                logger.debug(f"🔢 Compteur3 {suit}: absent au jeu #{game_number} → reset apparences (était {compteur3_appearances[suit]})")
            compteur3_appearances[suit] = 0

            # ── Compteur2 : incrémenter absences consécutives ─────────────────
            if last_seen == 0 or game_number == last_seen + 1:
                compteur2_absences[suit] += 1
            else:
                logger.info(
                    f"📊 Compteur2 {suit}: jeu #{game_number} non-consécutif "
                    f"(précédent #{last_seen}) → compteur remis à 1"
                )
                compteur2_absences[suit] = 1

            compteur2_last_seen[suit] = game_number
            count = compteur2_absences[suit]
            logger.info(f"📊 Compteur2 {suit}: absence joueur consécutive {count}/{compteur2_b} (jeu #{game_number})")

            if count >= compteur2_b:
                inverse_suit = SUIT_INVERSE.get(suit, suit)
                pred_game = game_number + 1

                # ── Décision compteur3 : manquant ou inverse ? ─────────────────
                if compteur3_active:
                    inverse_appearances = compteur3_appearances.get(inverse_suit, 0)
                    if inverse_appearances >= compteur3_seuil:
                        pred_suit = suit
                        logger.info(
                            f"🔢 Compteur3 actif: {inverse_suit} apparu {inverse_appearances}x CONSÉCUTIFS "
                            f"≥ seuil3={compteur3_seuil} + {suit} absent {count}x "
                            f"→ prédiction MANQUANT {pred_suit} pour #{pred_game}"
                        )
                    else:
                        pred_suit = inverse_suit
                        logger.info(
                            f"🔢 Compteur3 actif: {inverse_suit} apparu {inverse_appearances}x "
                            f"< seuil3={compteur3_seuil} + {suit} absent {count}x "
                            f"→ prédiction INVERSE {pred_suit} pour #{pred_game}"
                        )
                else:
                    pred_suit = inverse_suit
                    logger.info(
                        f"🔮 Compteur2: {suit} absent {compteur2_b}x CONSÉCUTIFS "
                        f"→ prédiction inverse {pred_suit} pour #{pred_game}"
                    )

                # ── Règle 1 : Mode Attente verrouillé ──────────────────────────
                if attente_mode and attente_locked:
                    logger.info(
                        f"🔒 Mode Attente verrouillé: B={compteur2_b} atteint pour {suit} "
                        f"→ prédiction {pred_suit} ignorée (attend PERDU)"
                    )
                    compteur2_absences[suit] = 0
                    continue

                # ── Règle 2 : Attendre que toutes les vérifications soient faites ──
                if pending_predictions:
                    logger.info(
                        f"⏸ Prédiction #{pred_game} {pred_suit} ignorée: "
                        f"vérification en cours pour {list(pending_predictions.keys())}"
                    )
                    compteur2_absences[suit] = 0
                    continue

                # ── Règle 3 : Écart minimum de 2 entre prédictions ──────────────
                if last_prediction_game > 0 and pred_game < last_prediction_game + 2:
                    logger.info(
                        f"⏸ Prédiction #{pred_game} {pred_suit} ignorée: "
                        f"écart insuffisant (dernier prédit: #{last_prediction_game}, "
                        f"écart requis: 2, écart actuel: {pred_game - last_prediction_game})"
                    )
                    compteur2_absences[suit] = 0
                    continue

                # ── Règle 4 : Pas de prédiction pour le même numéro deux fois ──
                if pred_game == last_prediction_game:
                    logger.info(
                        f"⏸ Prédiction #{pred_game} {pred_suit} ignorée: "
                        f"game #{pred_game} déjà prédit"
                    )
                    compteur2_absences[suit] = 0
                    continue

                # ── Règle 5 : Compteur4 - paire d'inverses absente JJ fois ────
                if compteur4_active:
                    if suit in ('♠', '♦'):
                        pair_count = compteur4_pair_a
                        pair_label = "♠+♦"
                    else:
                        pair_count = compteur4_pair_b
                        pair_label = "♥+♣"

                    if pair_count >= compteur4_jj:
                        logger.info(
                            f"🔲 Compteur4: Paire {pair_label} absente ensemble "
                            f"{pair_count}x ≥ JJ={compteur4_jj} "
                            f"→ prédiction #{pred_game} {pred_suit} bloquée, attendre la prochaine"
                        )
                        # Réinitialiser la paire et le compteur2 pour ce costume
                        if suit in ('♠', '♦'):
                            compteur4_pair_a = 0
                        else:
                            compteur4_pair_b = 0
                        compteur2_absences[suit] = 0
                        continue

                sent = await send_prediction(pred_game, pred_suit, suit)
                if sent is not None:
                    # Prédiction envoyée avec succès → reset du compteur
                    compteur2_absences[suit] = 0
                else:
                    # Bloquée (hors intervalle horaire) → on garde le compteur
                    # pour qu'il puisse retenter au prochain jeu si besoin
                    logger.info(
                        f"⏰ Compteur2 {suit}: prédiction non envoyée (hors intervalle) "
                        f"→ compteur conservé à {compteur2_absences[suit]}"
                    )

    # ── Compteur4 : suivi des absences de paires (après la boucle par costume) ──
    if compteur4_active:
        pair_a_absent = '♠' not in player_suits and '♦' not in player_suits
        if pair_a_absent:
            if compteur4_last_game_pair_a == 0 or game_number == compteur4_last_game_pair_a + 1:
                compteur4_pair_a += 1
            else:
                compteur4_pair_a = 1
            compteur4_last_game_pair_a = game_number
            logger.info(f"🔲 Compteur4 ♠+♦ absents ensemble: {compteur4_pair_a}/{compteur4_jj} (jeu #{game_number})")
        else:
            if compteur4_pair_a > 0:
                logger.debug(f"🔲 Compteur4 ♠+♦: présent au jeu #{game_number} → reset")
            compteur4_pair_a = 0
            compteur4_last_game_pair_a = game_number

        pair_b_absent = '♥' not in player_suits and '♣' not in player_suits
        if pair_b_absent:
            if compteur4_last_game_pair_b == 0 or game_number == compteur4_last_game_pair_b + 1:
                compteur4_pair_b += 1
            else:
                compteur4_pair_b = 1
            compteur4_last_game_pair_b = game_number
            logger.info(f"🔲 Compteur4 ♥+♣ absents ensemble: {compteur4_pair_b}/{compteur4_jj} (jeu #{game_number})")
        else:
            if compteur4_pair_b > 0:
                logger.debug(f"🔲 Compteur4 ♥+♣: présent au jeu #{game_number} → reset")
            compteur4_pair_b = 0
            compteur4_last_game_pair_b = game_number

# ============================================================================
# BOUCLE DE POLLING API - DYNAMIQUE
# ============================================================================

async def api_polling_loop():
    """Interroge l'API 1xBet en continu.

    Comportement :
    - Vérification dynamique : dès que les cartes du joueur sont disponibles.
    - Compteur2 : déclenché dès que le joueur a ses cartes (>= 2), sans attendre le banquier.
    - Reset automatique : déclenché quand la partie #1440 est terminée.
    """
    global current_game_number, api_results_cache, player_processed_games
    global reset_done_for_cycle

    loop = asyncio.get_event_loop()
    logger.info(f"🔄 Polling API dynamique démarré (intervalle: {API_POLL_INTERVAL}s)")

    while True:
        try:
            results = await loop.run_in_executor(None, get_latest_results)

            if results:
                for result in results:
                    game_number = result["game_number"]
                    is_finished = result["is_finished"]
                    player_cards = result.get("player_cards", [])

                    # Mettre à jour le cache
                    api_results_cache[game_number] = result

                    # Extraire costumes joueur
                    player_suits = player_suits_from_cards(player_cards)
                    ready = len(player_cards) >= 2

                    if not ready:
                        continue

                    current_game_number = game_number

                    p_display = " ".join(SUIT_DISPLAY.get(s, s) for s in player_suits) or "—"

                    # ── 1. VÉRIFICATION DYNAMIQUE ──────────────────────────────
                    # Dès que les cartes du joueur sont disponibles
                    await check_prediction_result_dynamic(game_number, player_suits, is_finished)

                    # ── 2. COMPTEUR2 ───────────────────────────────────────────
                    # Déclenché dès que le JOUEUR a terminé de prendre ses cartes
                    # (ready=True = joueur a >= 2 cartes), sans attendre la fin du banquier.
                    if game_number not in player_processed_games and ready:
                        player_processed_games.add(game_number)
                        if len(player_processed_games) > 500:
                            oldest = min(player_processed_games)
                            player_processed_games.discard(oldest)

                        logger.info(
                            f"🃏 Jeu #{game_number} | Joueur a ses cartes: {p_display} "
                            f"| Terminé: {is_finished}"
                        )
                        await process_compteur2(game_number, player_suits)

                    # ── 3. RESET AUTOMATIQUE sur la partie #1440 ────────────────
                    if game_number == 1440 and is_finished and not reset_done_for_cycle:
                        reset_done_for_cycle = True
                        logger.info("🔄 Reset automatique: partie #1440 terminée")
                        await perform_full_reset("Reset automatique (partie #1440 terminée)")

                    # Remettre à zéro le flag si on repart au début du cycle
                    if game_number < 100 and reset_done_for_cycle:
                        reset_done_for_cycle = False
                        logger.info("🔄 Nouveau cycle détecté (game < 100) → flag reset remis à zéro")

                # Nettoyage du cache (garder 300 derniers)
                if len(api_results_cache) > 300:
                    oldest = min(api_results_cache.keys())
                    del api_results_cache[oldest]

        except Exception as e:
            logger.error(f"❌ Erreur polling API: {e}")
            logger.error(traceback.format_exc())

        await asyncio.sleep(API_POLL_INTERVAL)

# ============================================================================
# RESET COMPLET
# ============================================================================

async def perform_full_reset(reason: str):
    global pending_predictions, last_prediction_time
    global compteur2_absences, compteur2_last_game, attente_locked
    global compteur2_last_seen, compteur2_processed_games
    global player_processed_games, api_results_cache
    global last_prediction_game, reset_done_for_cycle
    global compteur3_appearances, compteur3_last_appeared
    global compteur4_pair_a, compteur4_pair_b, compteur4_last_game_pair_a, compteur4_last_game_pair_b

    stats = len(pending_predictions)
    pending_predictions.clear()
    last_prediction_time = None
    last_prediction_game = 0
    compteur2_absences = {suit: 0 for suit in ALL_SUITS}
    compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
    compteur2_processed_games = set()
    compteur2_last_game = 0
    attente_locked = False
    player_processed_games = set()
    api_results_cache = {}
    compteur3_appearances = {suit: 0 for suit in ALL_SUITS}
    compteur3_last_appeared = {suit: 0 for suit in ALL_SUITS}
    compteur4_pair_a = 0
    compteur4_pair_b = 0
    compteur4_last_game_pair_a = 0
    compteur4_last_game_pair_b = 0

    logger.info(f"🔄 {reason} - {stats} prédictions cleared")

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if prediction_entity and client and client.is_connected():
            await client.send_message(
                prediction_entity,
                f"🔄 **RESET SYSTÈME**\n\n{reason}\n\n"
                f"✅ Compteurs remis à zéro\n"
                f"✅ {stats} prédictions cleared\n\n"
                f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
            )
    except Exception as e:
        logger.error(f"❌ Notif reset failed: {e}")

# ============================================================================
# COMMANDES ADMIN
# ============================================================================

async def cmd_compteur2(event):
    global compteur2_active, compteur2_b, compteur2_absences, compteur2_last_game
    global compteur2_last_seen, compteur2_processed_games, player_processed_games

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(get_compteur2_status_text())
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur2_active = True
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur2_processed_games = set()
        player_processed_games = set()
        await event.respond(
            f"✅ Compteur2 ACTIVÉ | B={compteur2_b}\n\n" + get_compteur2_status_text()
        )

    elif arg == 'off':
        compteur2_active = False
        await event.respond("❌ Compteur2 DÉSACTIVÉ")

    elif arg == 'reset':
        compteur2_absences = {suit: 0 for suit in ALL_SUITS}
        compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
        compteur2_processed_games = set()
        player_processed_games = set()
        compteur2_last_game = 0
        await event.respond("🔄 Compteur2 remis à zéro\n\n" + get_compteur2_status_text())

    elif arg == 'b':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur2 b <valeur>` (ex: `/compteur2 b 4`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ B doit être entre 1 et 20")
                return
            old_b = compteur2_b
            compteur2_b = val
            compteur2_absences = {suit: 0 for suit in ALL_SUITS}
            compteur2_last_seen = {suit: 0 for suit in ALL_SUITS}
            compteur2_processed_games = set()
            player_processed_games = set()
            await event.respond(
                f"✅ Compteur2 B: {old_b} → {compteur2_b} | Compteurs remis à zéro\n\n"
                + get_compteur2_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur2 b 4`")
    else:
        await event.respond(
            "📊 **COMPTEUR2 - Aide**\n\n"
            "`/compteur2` — Afficher l'état\n"
            "`/compteur2 on` — Activer\n"
            "`/compteur2 off` — Désactiver\n"
            "`/compteur2 b <val>` — Changer le seuil B\n"
            "`/compteur2 reset` — Remettre les compteurs à zéro"
        )

async def cmd_compteur3(event):
    global compteur3_active, compteur3_seuil, compteur3_appearances, compteur3_last_appeared

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(
            get_compteur3_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/compteur3 on` — Activer\n"
            "`/compteur3 off` — Désactiver\n"
            "`/compteur3 s <val>` — Changer le seuil (ex: `/compteur3 s 3`)\n"
            "`/compteur3 reset` — Remettre les compteurs à zéro"
        )
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur3_active = True
        compteur3_appearances = {suit: 0 for suit in ALL_SUITS}
        compteur3_last_appeared = {suit: 0 for suit in ALL_SUITS}
        await event.respond(
            f"✅ Compteur3 ACTIVÉ | Seuil={compteur3_seuil}\n\n"
            + get_compteur3_status_text()
        )

    elif arg == 'off':
        compteur3_active = False
        await event.respond("❌ Compteur3 DÉSACTIVÉ\n\nLe bot prédit l'inverse dès que le manquant atteint B.")

    elif arg == 'reset':
        compteur3_appearances = {suit: 0 for suit in ALL_SUITS}
        compteur3_last_appeared = {suit: 0 for suit in ALL_SUITS}
        await event.respond("🔄 Compteur3 remis à zéro\n\n" + get_compteur3_status_text())

    elif arg == 's':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur3 s <valeur>` (ex: `/compteur3 s 3`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ Le seuil doit être entre 1 et 20")
                return
            old_s = compteur3_seuil
            compteur3_seuil = val
            compteur3_appearances = {suit: 0 for suit in ALL_SUITS}
            compteur3_last_appeared = {suit: 0 for suit in ALL_SUITS}
            await event.respond(
                f"✅ Compteur3 Seuil: {old_s} → {compteur3_seuil} | Compteurs remis à zéro\n\n"
                + get_compteur3_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur3 s 3`")

    else:
        await event.respond(
            "🔢 **COMPTEUR3 - Aide**\n\n"
            "`/compteur3` — Afficher l'état\n"
            "`/compteur3 on` — Activer\n"
            "`/compteur3 off` — Désactiver\n"
            "`/compteur3 s <val>` — Changer le seuil\n"
            "`/compteur3 reset` — Remettre les compteurs à zéro"
        )

async def cmd_compteur4(event):
    global compteur4_active, compteur4_jj
    global compteur4_pair_a, compteur4_pair_b, compteur4_last_game_pair_a, compteur4_last_game_pair_b

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        await event.respond(
            get_compteur4_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/compteur4 on` — Activer\n"
            "`/compteur4 off` — Désactiver\n"
            "`/compteur4 jj <val>` — Changer le seuil JJ (ex: `/compteur4 jj 2`)\n"
            "`/compteur4 reset` — Remettre les compteurs à zéro"
        )
        return

    arg = parts[1].lower()

    if arg == 'on':
        compteur4_active = True
        compteur4_pair_a = 0
        compteur4_pair_b = 0
        compteur4_last_game_pair_a = 0
        compteur4_last_game_pair_b = 0
        await event.respond(
            f"✅ Compteur4 ACTIVÉ | JJ={compteur4_jj}\n\n"
            + get_compteur4_status_text()
        )

    elif arg == 'off':
        compteur4_active = False
        await event.respond(
            "❌ Compteur4 DÉSACTIVÉ\n\n"
            "Les prédictions ne sont plus bloquées par les paires inverses absentes."
        )

    elif arg == 'reset':
        compteur4_pair_a = 0
        compteur4_pair_b = 0
        compteur4_last_game_pair_a = 0
        compteur4_last_game_pair_b = 0
        await event.respond("🔄 Compteur4 remis à zéro\n\n" + get_compteur4_status_text())

    elif arg == 'jj':
        if len(parts) < 3:
            await event.respond("Usage: `/compteur4 jj <valeur>` (ex: `/compteur4 jj 2`)")
            return
        try:
            val = int(parts[2])
            if not 1 <= val <= 20:
                await event.respond("❌ JJ doit être entre 1 et 20")
                return
            old_jj = compteur4_jj
            compteur4_jj = val
            compteur4_pair_a = 0
            compteur4_pair_b = 0
            compteur4_last_game_pair_a = 0
            compteur4_last_game_pair_b = 0
            await event.respond(
                f"✅ Compteur4 JJ: {old_jj} → {compteur4_jj} | Compteurs remis à zéro\n\n"
                + get_compteur4_status_text()
            )
        except ValueError:
            await event.respond("❌ Valeur invalide. Usage: `/compteur4 jj 2`")

    else:
        await event.respond(
            "🔲 **COMPTEUR4 - Aide**\n\n"
            "`/compteur4` — Afficher l'état\n"
            "`/compteur4 on` — Activer\n"
            "`/compteur4 off` — Désactiver\n"
            "`/compteur4 jj <val>` — Changer le seuil JJ\n"
            "`/compteur4 reset` — Remettre les compteurs à zéro\n\n"
            "**Logique:**\n"
            "• Compte les jeux consécutifs où ♠️+♦️ sont absents **ensemble**\n"
            "• Compte les jeux consécutifs où ❤️+♣️ sont absents **ensemble**\n"
            "• Si une paire atteint JJ → bloque la prédiction, attend la prochaine"
        )

async def cmd_attente(event):
    global attente_mode, attente_locked

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.strip().split()

    if len(parts) == 1 or (len(parts) == 2 and parts[1].lower() == 'status'):
        mode_str = "✅ ON" if attente_mode else "❌ OFF"
        lock_str = "🔒 Verrouillé (attend PERDU)" if (attente_mode and attente_locked) else "🔓 Prêt"
        await event.respond(
            f"🕐 **MODE ATTENTE**\n\n"
            f"Statut: {mode_str}\n"
            f"État: {lock_str}\n\n"
            f"`/attente on` — Activer\n"
            f"`/attente off` — Désactiver\n"
            f"`/attente reset` — Déverrouiller manuellement"
        )
        return

    arg = parts[1].lower()

    if arg == 'on':
        attente_mode = True
        attente_locked = False
        await event.respond("✅ **Mode Attente ACTIVÉ**\n\nÉtat actuel: 🔓 Prêt.")

    elif arg == 'off':
        attente_mode = False
        attente_locked = False
        await event.respond("❌ **Mode Attente DÉSACTIVÉ**")

    elif arg == 'reset':
        attente_locked = False
        status = "✅ ON" if attente_mode else "❌ OFF"
        await event.respond(
            f"🔓 **Mode Attente déverrouillé manuellement**\n\nMode Attente: {status}"
        )
    else:
        await event.respond(
            "🕐 **MODE ATTENTE - Aide**\n\n"
            "`/attente on/off` — Activer/désactiver\n"
            "`/attente reset` — Déverrouiller manuellement"
        )

async def cmd_history(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    if not prediction_history:
        await event.respond("📜 Aucune prédiction dans l'historique.")
        return

    lines = [
        "📜 **HISTORIQUE DES PRÉDICTIONS**",
        "═══════════════════════════════════════",
        ""
    ]

    for i, pred in enumerate(prediction_history[:20], 1):
        pred_game = pred['predicted_game']
        suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
        trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
        time_str = pred['predicted_at'].strftime('%H:%M:%S')
        silent_tag = " [Attente]" if pred.get('silent') else ""

        status = pred['status']
        if status == 'en_cours':
            status_str = "⏳ En cours..."
        elif status == 'gagne':
            status_str = "✅ GAGNÉ"
        elif status == 'perdu':
            status_str = "❌ PERDU"
        else:
            status_str = f"❓ {status}"

        lines.append(
            f"{i}. 🕐 `{time_str}` | **Game #{pred_game}** {suit}{silent_tag}\n"
            f"   📉 Déclenché par: {trig} absent {compteur2_b}x\n"
            f"   📊 Résultat: {status_str}"
        )
        lines.append("")

    if pending_predictions:
        lines.append("**🔮 PRÉDICTIONS ACTIVES:**")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"Attente R{ar} (#{num + ar})" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #{num} {suit}: {st}")
        lines.append("")

    lines.append("═══════════════════════════════════════")
    await event.respond("\n".join(lines))

async def cmd_channels(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    pred_status = "❌"
    pred_name = "Inaccessible"

    try:
        if PREDICTION_CHANNEL_ID:
            pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
            if pred_entity:
                pred_status = "✅"
                pred_name = getattr(pred_entity, 'title', 'Sans titre')
    except Exception as e:
        pred_status = f"❌ ({str(e)[:30]})"

    await event.respond(
        f"📡 **CONFIGURATION**\n\n"
        f"**Source des données:** API 1xBet (polling {API_POLL_INTERVAL}s)\n"
        f"**Jeux en cache:** {len(api_results_cache)}\n"
        f"**Jeux traités (joueur):** {len(player_processed_games)}\n\n"
        f"**Canal Prédiction:**\n"
        f"ID: `{PREDICTION_CHANNEL_ID}`\n"
        f"Status: {pred_status}\n"
        f"Nom: {pred_name}\n\n"
        f"**Paramètres:**\n"
        f"Compteur2 B={compteur2_b} | Actif: {'✅' if compteur2_active else '❌'}\n"
        f"Mode Attente: {'✅ ON' if attente_mode else '❌ OFF'}\n"
        f"Dernière prédiction: #{last_prediction_game if last_prediction_game else 'Aucune'}\n"
        f"Reset au jeu: #1440 (fin de partie)\n"
        f"Admin ID: `{ADMIN_ID}`"
    )

async def cmd_test(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🧪 Test de connexion au canal de prédiction...")

    try:
        if not PREDICTION_CHANNEL_ID:
            await event.respond("❌ PREDICTION_CHANNEL_ID non configuré")
            return

        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond(
                f"❌ **Canal inaccessible** `{PREDICTION_CHANNEL_ID}`\n\n"
                f"Vérifiez:\n"
                f"1. L'ID est correct\n"
                f"2. Le bot est administrateur du canal\n"
                f"3. Le bot a les permissions d'envoi"
            )
            return

        test_msg = (
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲 [TEST]\n"
            f"Game 9999  :♠️\n\n"
            f"En cours de vérification\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        sent = await client.send_message(prediction_entity, test_msg)
        await asyncio.sleep(2)

        await client.edit_message(
            prediction_entity, sent.id,
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲 [TEST]\n"
            f"Game 9999  :♠️\n\n"
            f"Rattrapage :✅0️⃣\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}"
        )
        await asyncio.sleep(2)
        await client.delete_messages(prediction_entity, [sent.id])

        pred_name_display = getattr(prediction_entity, 'title', str(prediction_entity.id))
        await event.respond(
            f"✅ **TEST RÉUSSI!**\n\n"
            f"Canal: `{pred_name_display}`\n"
            f"Envoi, modification et suppression: OK"
        )

    except ChatWriteForbiddenError:
        await event.respond(
            "❌ **Permission refusée** — Ajoutez le bot comme administrateur."
        )
    except Exception as e:
        await event.respond(f"❌ Échec du test: {e}")

async def cmd_reset(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    await event.respond("🔄 Reset en cours...")
    await perform_full_reset("Reset manuel admin")
    await event.respond("✅ Reset effectué! Compteurs remis à zéro.")

async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    lines = [
        "📈 **ÉTAT DU BOT**",
        "",
        get_compteur2_status_text(),
        "",
        get_compteur3_status_text(),
        "",
        get_compteur4_status_text(),
        "",
        f"🔮 Prédictions actives: {len(pending_predictions)}",
        f"📡 Source: API 1xBet (polling {API_POLL_INTERVAL}s)",
        f"📦 Jeux en cache: {len(api_results_cache)}",
        f"🔄 Reset automatique: partie #1440 terminée",
    ]

    if pending_predictions:
        lines.append("")
        for num, pred in sorted(pending_predictions.items()):
            suit = SUIT_DISPLAY.get(pred['suit'], pred['suit'])
            trig = SUIT_DISPLAY.get(pred['triggered_by'], pred['triggered_by'])
            ar = pred.get('awaiting_rattrapage', 0)
            st = f"R{ar} en attente (#{num+ar})" if ar > 0 else "Vérification directe"
            lines.append(f"• Game #{num} {suit} (inverse de {trig}): {st}")

    await event.respond("\n".join(lines))

async def cmd_announce(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    parts = event.message.message.split(' ', 1)
    if len(parts) < 2:
        await event.respond("Usage: `/announce Message`")
        return

    text = parts[1].strip()
    if len(text) > 500:
        await event.respond("❌ Trop long (max 500 caractères)")
        return

    try:
        prediction_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
        if not prediction_entity:
            await event.respond("❌ Canal de prédiction non accessible")
            return

        now = datetime.now()
        msg = (
            f"╔══════════════════════════════════════╗\n"
            f"║     📢 ANNONCE OFFICIELLE 📢          ║\n"
            f"╠══════════════════════════════════════╣\n\n"
            f"{text}\n\n"
            f"╠══════════════════════════════════════╣\n"
            f"║  📅 {now.strftime('%d/%m/%Y')}  🕐 {now.strftime('%H:%M')}\n"
            f"╚══════════════════════════════════════╝\n\n"
            f"🎲𝐁𝐀𝐂𝐂𝐀𝐑𝐀 𝐏𝐑𝐄𝐌𝐈𝐔𝐌+2 ✨🎲"
        )
        sent = await client.send_message(prediction_entity, msg)
        await event.respond(f"✅ Annonce envoyée (ID: {sent.id})")
    except Exception as e:
        await event.respond(f"❌ Erreur: {e}")

async def cmd_predi(event):
    """Gestion des intervalles horaires de prédiction (heure du Bénin = UTC+1).

    Commandes:
      /predi                 — Afficher l'état et les intervalles
      /predi+HH-HH           — Ajouter un intervalle (ex: /predi+12-15)
      /predi del <N>         — Supprimer l'intervalle N
      /predi clear           — Supprimer tous les intervalles
      /predi on              — Activer la restriction par intervalles
      /predi off             — Désactiver la restriction (toujours autorisé)
    """
    global prediction_intervals, intervals_enabled

    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("🔒 Admin uniquement")
        return

    raw = event.message.message.strip()

    # Commande: /predi+HH-HH  ex: /predi+12-15  ou  /predi+1-4
    add_match = re.match(r'^/predi\+(\d{1,2})-(\d{1,2})$', raw)
    if add_match:
        start_h = int(add_match.group(1))
        end_h = int(add_match.group(2))
        if not (0 <= start_h <= 23 and 0 <= end_h <= 23):
            await event.respond("❌ Heures invalides. Utilisez des valeurs entre 0 et 23.")
            return
        if start_h == end_h:
            await event.respond("❌ L'heure de début et de fin ne peuvent pas être identiques.")
            return
        # Vérifier si l'intervalle existe déjà
        for iv in prediction_intervals:
            if iv["start"] == start_h and iv["end"] == end_h:
                await event.respond(f"⚠️ L'intervalle {start_h:02d}h00→{end_h:02d}h00 existe déjà.")
                return
        prediction_intervals.append({"start": start_h, "end": end_h})
        await event.respond(
            f"✅ Intervalle ajouté: {start_h:02d}h00 → {end_h:02d}h00 (heure Bénin)\n\n"
            + get_intervals_status_text()
        )
        return

    parts = raw.split()

    if len(parts) == 1:
        await event.respond(
            get_intervals_status_text() + "\n\n"
            "**Commandes:**\n"
            "`/predi+HH-HH` — Ajouter un intervalle (ex: `/predi+12-15`)\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction\n"
            "`/predi off` — Désactiver la restriction"
        )
        return

    arg = parts[1].lower()

    if arg == "on":
        intervals_enabled = True
        await event.respond(
            "✅ **Restriction horaire ACTIVÉE**\n\n"
            + get_intervals_status_text()
        )

    elif arg == "off":
        intervals_enabled = False
        await event.respond(
            "❌ **Restriction horaire DÉSACTIVÉE** — prédictions toujours autorisées\n\n"
            + get_intervals_status_text()
        )

    elif arg == "clear":
        prediction_intervals = []
        await event.respond("🗑️ Tous les intervalles supprimés.\n\n" + get_intervals_status_text())

    elif arg == "del":
        if len(parts) < 3:
            await event.respond("Usage: `/predi del <N>` (N = numéro de l'intervalle dans la liste)")
            return
        try:
            idx = int(parts[2]) - 1
            if not (0 <= idx < len(prediction_intervals)):
                await event.respond(f"❌ Index invalide. Il y a {len(prediction_intervals)} intervalle(s).")
                return
            removed = prediction_intervals.pop(idx)
            await event.respond(
                f"🗑️ Intervalle {removed['start']:02d}h00→{removed['end']:02d}h00 supprimé.\n\n"
                + get_intervals_status_text()
            )
        except ValueError:
            await event.respond("❌ Numéro invalide.")

    else:
        await event.respond(
            "⏰ **INTERVALLES - Aide**\n\n"
            "`/predi` — Afficher l'état\n"
            "`/predi+HH-HH` — Ajouter un intervalle (ex: `/predi+12-15`)\n"
            "`/predi del <N>` — Supprimer l'intervalle N\n"
            "`/predi clear` — Supprimer tous les intervalles\n"
            "`/predi on` — Activer la restriction horaire\n"
            "`/predi off` — Désactiver la restriction horaire\n\n"
            "**Exemples:**\n"
            "`/predi+12-15` → prédit de 12h à 15h\n"
            "`/predi+20-21` → prédit de 20h à 21h\n"
            "`/predi+1-4` → prédit de 1h à 4h\n"
            "`/predi+23-3` → prédit de 23h à 3h (passe minuit)\n"
            "Toutes les heures sont en heure du Bénin (UTC+1)"
        )


async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "🎰 **Bienvenue sur le Bot de Manou !**\n\n"
        "✨ **Bot de Prédiction Baccarat — Fiable & Précis**\n\n"
        "Ce bot est l'outil ultime pour vos prédictions Baccarat.\n"
        "Grâce à un algorithme intelligent basé sur l'analyse des absences\n"
        "et apparences de costumes, il prédit avec une fiabilité remarquable\n"
        "le prochain résultat — et met à jour automatiquement chaque prédiction\n"
        "en temps réel dès que le résultat tombe. 🔥\n\n"
        "📊 Stratégies avancées (Compteur2, Compteur3, Compteur4)\n"
        "⚡ Vérification dynamique des résultats\n"
        "🕐 Gestion des intervalles horaires (heure Bénin)\n"
        "🔄 Reset automatique intelligent\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 Tapez /help pour voir toutes les commandes disponibles.\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"
        "👨‍💻 **Bot de Manou : SOSSOU Kouamé**\n"
        "📞 +22995501564\n"
        "📩 Telegram : @Kouamappoloak\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "_Pour tout problème, contactez le développeur directement sur Telegram._"
    )


async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    await event.respond(
        "📖 **BACCARAT PREMIUM+2 - AIDE**\n\n"
        "**🎮 Système de prédiction (Compteur2):**\n"
        "• Lit les cartes du joueur depuis l'API 1xBet\n"
        "• Compteur déclenché dès que le joueur a pris ses cartes\n"
        "• Quand une couleur atteint B absences → prédit l'**inverse** pour le jeu SUIVANT\n"
        "• ♠️↔♦️ | ❤️↔♣️\n\n"
        "**🔢 Compteur3 (optionnel) — Apparences consécutives:**\n"
        "• Compte combien de fois de suite un costume apparaît\n"
        "• Quand compteur3 est **OFF** : prédit toujours l'inverse du manquant\n"
        "• Quand compteur3 est **ON** :\n"
        "  – Si l'inverse du manquant a atteint le seuil3 → prédit le **manquant**\n"
        "  – Sinon → prédit l'**inverse** (comportement normal)\n\n"
        "**🔲 Compteur4 (optionnel) — Paires inverses absentes :**\n"
        "• Surveille si ♠️+♦️ sont absents **ensemble** JJ fois de suite\n"
        "• Surveille si ❤️+♣️ sont absents **ensemble** JJ fois de suite\n"
        "• Si le seuil JJ est atteint → bloque la prédiction, attend la suivante\n\n"
        "**🛡️ Règles anti-spam prédictions:**\n"
        "• Écart minimum de 2 entre les numéros de jeu prédits\n"
        "• Pas de prédictions consécutives (ex: #20 puis #21)\n"
        "• Un seul prédit à la fois (attend la vérification avant d'envoyer)\n"
        "• Pas de doublon sur le même numéro de jeu\n\n"
        "**🔍 Vérification dynamique:**\n"
        "• Dès que les cartes du joueur apparaissent → vérifie la prédiction\n"
        "• Costume trouvé → résultat immédiat\n"
        "• Pas trouvé et partie terminée → passe au rattrapage (max 3)\n\n"
        "**⏰ Intervalles horaires (heure Bénin):**\n"
        "• Définir des créneaux où les prédictions sont autorisées\n"
        "• Hors créneau → prédiction silencieusement ignorée\n\n"
        "**🔄 Reset automatique:**\n"
        "• Se déclenche quand la partie #1440 est terminée\n\n"
        "**🕐 Mode Attente:**\n"
        "• Prédit une fois, puis attend de voir ❌PERDU\n\n"
        "**🔧 Commandes Admin:**\n"
        "`/compteur2` — État et gestion du Compteur2\n"
        "`/compteur2 on/off` — Activer/désactiver\n"
        "`/compteur2 b <val>` — Changer le seuil B\n"
        "`/compteur3` — État et gestion du Compteur3\n"
        "`/compteur3 on/off` — Activer/désactiver\n"
        "`/compteur3 s <val>` — Changer le seuil (défaut: 3)\n"
        "`/compteur4` — État et gestion du Compteur4\n"
        "`/compteur4 on/off` — Activer/désactiver\n"
        "`/compteur4 jj <val>` — Changer le seuil JJ (défaut: 2)\n"
        "`/attente on/off/reset` — Mode Attente\n"
        "`/predi` — Gérer les intervalles horaires\n"
        "`/predi+HH-HH` — Ajouter un intervalle (ex: /predi+12-15)\n"
        "`/status` — État complet\n"
        "`/history` — Historique des prédictions\n"
        "`/channels` — Configuration\n"
        "`/test` — Tester le canal\n"
        "`/reset` — Reset complet\n"
        "`/announce <msg>` — Annonce\n"
        "`/help` — Cette aide"
    )

# ============================================================================
# CONFIGURATION DES HANDLERS
# ============================================================================

def setup_handlers():
    client.add_event_handler(cmd_compteur2, events.NewMessage(pattern=r'^/compteur2'))
    client.add_event_handler(cmd_compteur3, events.NewMessage(pattern=r'^/compteur3'))
    client.add_event_handler(cmd_compteur4, events.NewMessage(pattern=r'^/compteur4'))
    client.add_event_handler(cmd_attente, events.NewMessage(pattern=r'^/attente'))
    client.add_event_handler(cmd_predi, events.NewMessage(pattern=r'^/predi'))
    client.add_event_handler(cmd_status, events.NewMessage(pattern=r'^/status$'))
    client.add_event_handler(cmd_history, events.NewMessage(pattern=r'^/history$'))
    client.add_event_handler(cmd_start, events.NewMessage(pattern=r'^/start$'))
    client.add_event_handler(cmd_help, events.NewMessage(pattern=r'^/help$'))
    client.add_event_handler(cmd_reset, events.NewMessage(pattern=r'^/reset$'))
    client.add_event_handler(cmd_channels, events.NewMessage(pattern=r'^/channels$'))
    client.add_event_handler(cmd_test, events.NewMessage(pattern=r'^/test$'))
    client.add_event_handler(cmd_announce, events.NewMessage(pattern=r'^/announce'))

# ============================================================================
# DÉMARRAGE
# ============================================================================

async def start_bot():
    global client, prediction_channel_ok

    client = TelegramClient(StringSession(TELEGRAM_SESSION), API_ID, API_HASH)

    try:
        await client.start(bot_token=BOT_TOKEN)
        setup_handlers()

        if PREDICTION_CHANNEL_ID:
            try:
                pred_entity = await resolve_channel(PREDICTION_CHANNEL_ID)
                if pred_entity:
                    prediction_channel_ok = True
                    logger.info(f"✅ Canal prédiction OK: {getattr(pred_entity, 'title', 'Unknown')}")
                else:
                    logger.error(f"❌ Canal prédiction inaccessible: {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur vérification canal: {e}")

        logger.info(f"🤖 Bot démarré | Compteur2 B={compteur2_b} | Attente={'ON' if attente_mode else 'OFF'}")
        logger.info(f"🔄 Reset automatique configuré: fin de la partie #1440")
        return True

    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False

async def main():
    try:
        if not await start_bot():
            return

        asyncio.create_task(api_polling_loop())
        logger.info("🔄 Polling API dynamique démarré")

        app = web.Application()
        app.router.add_get('/health', lambda r: web.Response(text="OK"))
        app.router.add_get('/', lambda r: web.Response(text="BACCARAT PREMIUM+2 🎲 Running"))

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()

        logger.info(f"🌐 Serveur web démarré sur port {PORT}")

        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"❌ Erreur main: {e}")
    finally:
        if client and client.is_connected():
            await client.disconnect()
            logger.info("🔌 Déconnecté")

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
