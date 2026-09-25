#!/usr/bin/env python3
"""Single-file GameVH Phỏm WebSocket bot with public-information AI.

The bot logs in with credentials supplied through the environment, enters a
Phỏm lobby, requires the exact requested virtual-coin bet, creates a public
table, plays the complete server state machine, and records sanitized state
and explainable AI decisions. It never stores credentials/session tokens or
uses hidden opponent cards.
"""
from __future__ import annotations

import atexit
import http.cookiejar
import json
import os
import re
import signal
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import websocket

# Rule engine and public-information AI are intentionally embedded in this
# file so the Termux deployment has one Python runtime file.
RANK_NAMES = ("A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K")
SUIT_NAMES = ("S0", "S1", "S2", "S3")


def valid_card(card: int) -> bool:
    return isinstance(card, int) and 0 <= card < 52


def rank(card: int) -> int:
    return card % 13


def suit(card: int) -> int:
    return card // 13


def point(card: int) -> int:
    return rank(card) + 1


def card_name(card: int) -> str:
    if not valid_card(card):
        return "??"
    return f"{RANK_NAMES[rank(card)]}-{SUIT_NAMES[suit(card)]}({card})"


def meld_masks(cards: list[int]) -> list[int]:
    """Return every set/run mask available in this concrete card list."""
    masks: set[int] = set()
    by_rank: dict[int, list[int]] = {}
    by_suit: dict[int, list[tuple[int, int]]] = {}
    for index, card in enumerate(cards):
        if not valid_card(card):
            continue
        by_rank.setdefault(rank(card), []).append(index)
        by_suit.setdefault(suit(card), []).append((rank(card), index))

    for indices in by_rank.values():
        if len(indices) >= 3:
            # A four-of-a-kind may be kept whole or as any three-card set.
            full = sum(1 << index for index in indices)
            masks.add(full)
            if len(indices) == 4:
                for omitted in indices:
                    masks.add(full & ~(1 << omitted))

    for values in by_suit.values():
        values.sort()
        for start in range(len(values)):
            for end in range(start + 3, len(values) + 1):
                segment = values[start:end]
                ranks = [item[0] for item in segment]
                if all(ranks[i] + 1 == ranks[i + 1] for i in range(len(ranks) - 1)):
                    masks.add(sum(1 << item[1] for item in segment))
                elif ranks[-1] - ranks[0] >= len(ranks):
                    break
    return sorted(masks)


def best_meld_partition(cards: list[int]) -> tuple[int, list[list[int]]]:
    """Return minimum deadwood points and one optimal disjoint meld partition."""
    concrete = [card for card in cards if valid_card(card)]
    cards = concrete
    masks = meld_masks(cards)
    mask_points = {
        mask: sum(point(cards[i]) for i in range(len(cards)) if mask & (1 << i))
        for mask in masks
    }

    @lru_cache(maxsize=None)
    def saved(used: int) -> tuple[int, tuple[int, ...]]:
        best_value: int = 0
        best_partition: tuple[int, ...] = ()
        for mask in masks:
            if used & mask:
                continue
            child_value, child_partition = saved(used | mask)
            candidate_value = mask_points[mask] + child_value
            candidate_partition = (mask, *child_partition)
            candidate_covered = sum(item.bit_count() for item in candidate_partition)
            best_covered = sum(item.bit_count() for item in best_partition)
            if ((candidate_value, candidate_covered, -len(candidate_partition))
                    > (best_value, best_covered, -len(best_partition))):
                best_value = candidate_value
                best_partition = candidate_partition
        return best_value, best_partition

    saved_points, partition_masks = saved(0)
    groups = [
        [cards[i] for i in range(len(cards)) if mask & (1 << i)]
        for mask in partition_masks
    ]
    return sum(point(card) for card in cards) - saved_points, groups


def best_deadwood(cards: list[int]) -> tuple[int, set[int]]:
    """Return minimum deadwood points and indices covered by disjoint melds."""
    concrete = [card for card in cards if valid_card(card)]
    deadwood, groups = best_meld_partition(concrete)
    remaining = list(enumerate(concrete))
    covered: set[int] = set()
    for group in groups:
        for card in group:
            for index, candidate in remaining:
                if index not in covered and candidate == card:
                    covered.add(index)
                    break
    return deadwood, covered


def can_cover_required_cards(cards: list[int], required_cards: list[int]) -> bool:
    """Whether disjoint melds can cover every mandatory (eaten) card."""
    concrete = [card for card in cards if valid_card(card)]
    required = [card for card in required_cards if valid_card(card)]
    if not required:
        return True
    combined = list(dict.fromkeys([*concrete, *required]))
    required_mask = 0
    for card in required:
        if card not in combined:
            return False
        required_mask |= 1 << combined.index(card)
    masks = meld_masks(combined)

    @lru_cache(maxsize=None)
    def search(used: int) -> bool:
        if used & required_mask == required_mask:
            return True
        return any(not (used & mask) and search(used | mask) for mask in masks)

    return search(0)


def can_form_meld_with(cards: list[int], offered: int | None) -> bool:
    if offered is None or not valid_card(offered):
        return False
    combined = [card for card in cards if valid_card(card)] + [offered]
    offered_index = len(combined) - 1
    return any(mask & (1 << offered_index) for mask in meld_masks(combined))


def is_meld(cards: list[int]) -> bool:
    cards = [card for card in cards if valid_card(card)]
    if len(cards) < 3 or len(set(cards)) != len(cards):
        return False
    ranks = [rank(card) for card in cards]
    suits = [suit(card) for card in cards]
    is_set = len(cards) <= 4 and len(set(ranks)) == 1
    ordered = sorted(ranks)
    is_run = (len(set(suits)) == 1
              and all(ordered[i] + 1 == ordered[i + 1]
                      for i in range(len(ordered) - 1)))
    return is_set or is_run


def can_extend_public_meld(public_cards: list[int], card: int) -> bool:
    """Whether card can legally extend at least one exposed meld.

    A player's exposed line can contain several melds without boundary markers,
    so inspect all concrete subsets. Phỏm hands are tiny, keeping this cheap.
    """
    concrete = list(dict.fromkeys(
        value for value in public_cards if valid_card(value) and value != card
    ))
    for size in range(3, len(concrete) + 1):
        for subset in combinations(concrete, size):
            if is_meld(list(subset)) and is_meld([*subset, card]):
                return True
    return False


def choose_send_card(hand: list[int], exposed_lines: list[list[int]]) -> int | None:
    candidates = [
        card for card in hand if valid_card(card)
        and any(can_extend_public_meld(line, card) for line in exposed_lines)
    ]
    # Prefer sending the highest deadwood value.
    return max(candidates, key=lambda card: (point(card), card), default=None)


AI_POLICY_VERSION = "public-v1"


def _state_items(public_state: dict | None, key: str) -> list[dict]:
    if not isinstance(public_state, dict):
        return []
    value = public_state.get(key, [])
    return value if isinstance(value, list) else []


def meld_completion_pairs(card: int) -> list[tuple[int, int]]:
    """Every two-card holding that could legally eat ``card``.

    These are structural possibilities, not claims about hidden hands.
    """
    if not valid_card(card):
        return []
    pairs: set[tuple[int, int]] = set()
    same_rank = [other_suit * 13 + rank(card) for other_suit in range(4)
                 if other_suit != suit(card)]
    for pair in combinations(same_rank, 2):
        pairs.add(tuple(sorted(pair)))
    for left, right in ((-2, -1), (-1, 1), (1, 2)):
        first_rank = rank(card) + left
        second_rank = rank(card) + right
        if 0 <= first_rank < 13 and 0 <= second_rank < 13:
            pairs.add(tuple(sorted((suit(card) * 13 + first_rank,
                                    suit(card) * 13 + second_rank))))
    return sorted(pairs)


def public_unavailable_cards(hand: list[int], public_state: dict | None,
                             offered_card: int | None = None) -> set[int]:
    """Cards known not to be in an opponent's hidden hand.

    ``offered_card`` is excluded because it is the card being risk-assessed.
    """
    unavailable = {card for card in hand if valid_card(card) and card != offered_card}
    for item in _state_items(public_state, "discarded_cards"):
        card = item.get("card")
        if valid_card(card) and card != offered_card:
            unavailable.add(card)
    for item in _state_items(public_state, "eaten_cards"):
        card = item.get("card")
        if valid_card(card) and card != offered_card:
            unavailable.add(card)
    for item in _state_items(public_state, "exposed_melds"):
        for card in item.get("cards", []):
            if valid_card(card) and card != offered_card:
                unavailable.add(card)
    return unavailable


def inferred_pass_signals(public_state: dict | None) -> list[dict]:
    """Infer only observable non-eats from successive public discards.

    If discard D is not eaten and another player later discards before the next
    eat, that player necessarily continued the turn without eating D. This is
    weak negative evidence, never a hidden-card assertion.
    """
    discards = sorted(
        (dict(item) for item in _state_items(public_state, "discarded_cards")
         if valid_card(item.get("card")) and isinstance(item.get("sequence"), int)),
        key=lambda item: item["sequence"],
    )
    eats = [dict(item) for item in _state_items(public_state, "eaten_cards")
            if valid_card(item.get("card")) and isinstance(item.get("sequence"), int)]
    signals = []
    for index, discard in enumerate(discards[:-1]):
        following = discards[index + 1]
        eaten = any(
            item["card"] == discard["card"]
            and discard["sequence"] < item["sequence"] < following["sequence"]
            for item in eats
        )
        if not eaten and following.get("by_slot") != discard.get("by_slot"):
            signals.append({
                "card": discard["card"],
                "discarded_by": discard.get("by_slot"),
                "passed_by": following.get("by_slot"),
            })
    return signals


def opponent_interest_score(card: int, public_state: dict | None,
                            my_slot: int) -> tuple[float, list[str]]:
    """Explainable public-signal estimate of opponent interest in a card."""
    score = 0.0
    signals: list[str] = []
    for item in _state_items(public_state, "eaten_cards"):
        eaten = item.get("card")
        if not valid_card(eaten) or item.get("to_slot") == my_slot:
            continue
        if rank(eaten) == rank(card):
            score += 1.5
            signals.append(f"opponent-slot-{item.get('to_slot')}-ate-same-rank")
        if suit(eaten) == suit(card) and abs(rank(eaten) - rank(card)) <= 2:
            score += 1.0
            signals.append(f"opponent-slot-{item.get('to_slot')}-ate-nearby-suit")
    for item in _state_items(public_state, "exposed_melds"):
        if item.get("slot") == my_slot:
            continue
        meld = [value for value in item.get("cards", []) if valid_card(value)]
        if can_extend_public_meld(meld, card):
            score += 4.0
            signals.append(f"extends-slot-{item.get('slot')}-exposed-meld")
        elif any(rank(value) == rank(card) for value in meld):
            score += 0.5
            signals.append(f"matches-slot-{item.get('slot')}-exposed-rank")
        elif any(suit(value) == suit(card)
                 and abs(rank(value) - rank(card)) <= 2 for value in meld):
            score += 0.35
            signals.append(f"near-slot-{item.get('slot')}-exposed-run")
    recent_discards = _state_items(public_state, "discarded_cards")[-8:]
    for item in recent_discards:
        discarded = item.get("card")
        if (item.get("by_slot") == my_slot or not valid_card(discarded)):
            continue
        if rank(discarded) == rank(card):
            score -= 0.20
            signals.append(f"slot-{item.get('by_slot')}-discarded-same-rank")
        elif suit(discarded) == suit(card) and abs(rank(discarded) - rank(card)) <= 2:
            score -= 0.10
            signals.append(f"slot-{item.get('by_slot')}-discarded-nearby-suit")
    for item in inferred_pass_signals(public_state)[-8:]:
        passed = item["card"]
        if item.get("passed_by") == my_slot:
            continue
        if rank(passed) == rank(card):
            score -= 0.20
            signals.append(f"slot-{item.get('passed_by')}-passed-same-rank")
        elif suit(passed) == suit(card) and abs(rank(passed) - rank(card)) <= 2:
            score -= 0.10
            signals.append(f"slot-{item.get('passed_by')}-passed-nearby-suit")
    return round(max(-2.0, min(8.0, score)), 3), signals


def assess_card_safety(card: int, hand: list[int], public_state: dict | None,
                       my_slot: int = -1) -> dict:
    """Assess whether opponents can still hold a two-card completion.

    ``safety_index`` is a public-information feasibility score in [0, 1], not
    a calibrated probability. It is 1 only when every legal completion pair
    contains at least one publicly unavailable card.
    """
    pairs = meld_completion_pairs(card)
    unavailable = public_unavailable_cards(hand, public_state, card)
    live_pairs = [list(pair) for pair in pairs
                  if pair[0] not in unavailable and pair[1] not in unavailable]
    blocked_pairs = [list(pair) for pair in pairs
                     if pair[0] in unavailable or pair[1] in unavailable]
    safety = 1.0 if not pairs else 1.0 - len(live_pairs) / len(pairs)
    interest, signals = opponent_interest_score(card, public_state, my_slot)
    return {
        "card": card,
        "card_name": card_name(card),
        "safety_index": round(safety, 4),
        "is_structurally_safe": not live_pairs,
        "live_completion_pairs": live_pairs,
        "blocked_completion_pairs": blocked_pairs,
        "opponent_interest": interest,
        "signals": signals,
    }


def hand_draw_potential(cards: list[int], public_state: dict | None) -> float:
    """Count live public-information draws that improve incomplete meld pairs."""
    concrete = list(dict.fromkeys(card for card in cards if valid_card(card)))
    if len(concrete) < 2:
        return 0.0
    _deadwood, covered = best_deadwood(concrete)
    unavailable = public_unavailable_cards(concrete, public_state)
    potential = 0.0
    for left_index, right_index in combinations(range(len(concrete)), 2):
        if left_index in covered and right_index in covered:
            continue
        left, right = concrete[left_index], concrete[right_index]
        draws = [candidate for candidate in range(52)
                 if candidate not in unavailable
                 and candidate not in (left, right)
                 and is_meld([left, right, candidate])]
        if draws:
            potential += 1.0 + 0.5 * len(draws)
    return round(potential, 3)


def _ai_phase(public_state: dict | None, my_slot: int,
              final_round: bool) -> tuple[str, int]:
    own_discards = sum(
        item.get("by_slot") == my_slot
        for item in _state_items(public_state, "discarded_cards")
    )
    if final_round or own_discards >= 3:
        return "final", own_discards
    if own_discards <= 1:
        return "early", own_discards
    return "middle", own_discards


def choose_discard_ai(cards: list[int], public_state: dict | None = None,
                      my_slot: int = -1,
                      required_cards: list[int] | None = None,
                      forbidden_cards: set[int] | None = None,
                      final_round: bool = False) -> tuple[int | None, dict]:
    """Choose a legal discard with explainable public-information scoring."""
    concrete = [card for card in cards if valid_card(card)]
    required = [card for card in (required_cards or []) if valid_card(card)]
    forbidden = forbidden_cards or set()
    phase, own_discards = _ai_phase(public_state, my_slot, final_round)
    weights = {
        "early": {"deadwood": 6.0, "potential": 2.5, "safety": 15.0,
                  "live_pair": 0.8, "interest": 2.0},
        "middle": {"deadwood": 5.0, "potential": 2.0, "safety": 42.0,
                   "live_pair": 1.8, "interest": 4.5},
        "final": {"deadwood": 3.5, "potential": 1.0, "safety": 90.0,
                  "live_pair": 3.5, "interest": 9.0},
    }[phase]
    candidates = []
    for index, card in enumerate(concrete):
        item = {"card": card, "card_name": card_name(card), "legal": False}
        if card in forbidden:
            item["rejection_reason"] = "server-rejected-in-current-state"
            candidates.append(item)
            continue
        remaining = concrete[:index] + concrete[index + 1:]
        if not can_cover_required_cards(remaining, required):
            item["rejection_reason"] = "breaks-mandatory-eaten-card-meld"
            candidates.append(item)
            continue
        combined = list(dict.fromkeys([*remaining, *required]))
        deadwood, groups = best_meld_partition(combined)
        potential = hand_draw_potential(combined, public_state)
        safety = assess_card_safety(card, concrete, public_state, my_slot)
        live_count = len(safety["live_completion_pairs"])
        score = (
            -weights["deadwood"] * deadwood
            + weights["potential"] * potential
            + 0.35 * point(card)
            + weights["safety"] * safety["safety_index"]
            - weights["live_pair"] * live_count
            - weights["interest"] * safety["opponent_interest"]
        )
        item.update({
            "legal": True,
            "score": round(score, 4),
            "deadwood_after": deadwood,
            "draw_potential_after": potential,
            "melds_after": [sorted(group) for group in groups],
            "point_value": point(card),
            "safety": safety,
        })
        candidates.append(item)
    legal = [item for item in candidates if item["legal"]]
    chosen = max(
        legal,
        key=lambda item: (
            item["score"], item["safety"]["safety_index"],
            -item["deadwood_after"], item["point_value"], -item["card"],
        ),
        default=None,
    )
    decision = {
        "policy_version": AI_POLICY_VERSION,
        "decision_type": "discard",
        "phase": phase,
        "own_discard_count": own_discards,
        "chosen_card": chosen["card"] if chosen else None,
        "chosen_card_name": chosen["card_name"] if chosen else None,
        "weights": weights,
        "candidates": sorted(
            candidates,
            key=lambda item: (not item["legal"], -item.get("score", -10**9), item["card"]),
        ),
    }
    return decision["chosen_card"], decision


def choose_discard(cards: list[int], required_cards: list[int] | None = None,
                   forbidden_cards: set[int] | None = None) -> int | None:
    """Compatibility wrapper using AI v1 without table-history signals."""
    return choose_discard_ai(
        cards,
        required_cards=required_cards,
        forbidden_cards=forbidden_cards,
    )[0]


def assess_eat_decision(hand: list[int], offered: int | None,
                        public_state: dict | None, my_slot: int,
                        required_cards: list[int] | None = None) -> dict:
    """Choose EAT versus TAKE using projected legal post-eat deadwood."""
    decision = {
        "policy_version": AI_POLICY_VERSION,
        "decision_type": "eat",
        "offered_card": offered,
        "offered_card_name": card_name(offered) if valid_card(offered) else "??",
        "eat": False,
    }
    if offered is None or not can_form_meld_with(hand, offered):
        decision["reason"] = "offered-card-does-not-form-meld"
        return decision
    required_before = [card for card in (required_cards or []) if valid_card(card)]
    required_after = list(dict.fromkeys([*required_before, offered]))
    before_cards = list(dict.fromkeys([*hand, *required_before]))
    deadwood_before, melds_before = best_meld_partition(before_cards)
    projected_discard, projection = choose_discard_ai(
        hand,
        public_state=public_state,
        my_slot=my_slot,
        required_cards=required_after,
    )
    decision.update({
        "deadwood_before": deadwood_before,
        "melds_before": [sorted(group) for group in melds_before],
        "projected_discard": projected_discard,
        "projected_discard_name": (
            card_name(projected_discard) if projected_discard is not None else None
        ),
        "discard_projection": projection,
    })
    if projected_discard is None:
        decision["reason"] = "no-legal-post-eat-discard"
        return decision
    remaining = list(hand)
    remaining.remove(projected_discard)
    after_cards = list(dict.fromkeys([*remaining, *required_after]))
    deadwood_after, melds_after = best_meld_partition(after_cards)
    covered_before = sum(point(card) for card in before_cards) - deadwood_before
    covered_after = sum(point(card) for card in after_cards) - deadwood_after
    improvement = deadwood_before - deadwood_after
    decision.update({
        "deadwood_after_projected_discard": deadwood_after,
        "melds_after": [sorted(group) for group in melds_after],
        "covered_point_gain": covered_after - covered_before,
        "deadwood_improvement": improvement,
    })
    decision["eat"] = (
        can_cover_required_cards(remaining, required_after)
        and (improvement >= 0 or covered_after > covered_before)
    )
    decision["reason"] = (
        "legal-meld-and-nonworsening-projection" if decision["eat"]
        else "legal-but-projected-hand-worsens"
    )
    return decision


def choose_send_card_ai(hand: list[int], public_state: dict | None,
                        my_slot: int,
                        required_cards: list[int] | None = None
                        ) -> tuple[int | None, dict]:
    """Choose a legal consign that does not damage our remaining meld value."""
    concrete = [card for card in hand if valid_card(card)]
    required = [card for card in (required_cards or []) if valid_card(card)]
    before_cards = list(dict.fromkeys([*concrete, *required]))
    deadwood_before, _groups = best_meld_partition(before_cards)
    exposed = [item for item in _state_items(public_state, "exposed_melds")
               if item.get("slot") != my_slot]
    candidates = []
    for index, card in enumerate(concrete):
        targets = [
            {"slot": item.get("slot"), "cards": list(item.get("cards", []))}
            for item in exposed
            if can_extend_public_meld(item.get("cards", []), card)
        ]
        if not targets:
            continue
        remaining = concrete[:index] + concrete[index + 1:]
        legal = can_cover_required_cards(remaining, required)
        after_cards = list(dict.fromkeys([*remaining, *required]))
        deadwood_after, melds_after = best_meld_partition(after_cards)
        nonworsening = deadwood_after <= deadwood_before
        score = (deadwood_before - deadwood_after) * 20 + point(card)
        candidates.append({
            "card": card,
            "card_name": card_name(card),
            "legal": legal and nonworsening,
            "mandatory_coverage_ok": legal,
            "nonworsening": nonworsening,
            "deadwood_before": deadwood_before,
            "deadwood_after": deadwood_after,
            "score": score,
            "targets": targets,
            "melds_after": [sorted(group) for group in melds_after],
        })
    legal_candidates = [item for item in candidates if item["legal"]]
    chosen = max(
        legal_candidates,
        key=lambda item: (item["score"], point(item["card"]), -item["card"]),
        default=None,
    )
    decision = {
        "policy_version": AI_POLICY_VERSION,
        "decision_type": "send",
        "chosen_card": chosen["card"] if chosen else None,
        "chosen_card_name": chosen["card_name"] if chosen else None,
        "candidates": sorted(candidates, key=lambda item: (-item["score"], item["card"])),
        "reason": "best-nonworsening-consign" if chosen else "no-safe-consign",
    }
    return decision["chosen_card"], decision


LOGIN_URL = "https://gamevh.net/login.jsp"
GAME_URL = os.environ.get("GAMEVH_PHOM_URL", "https://gamevh.net/play/0/1")
WS_URL = "wss://gamevh.net/ws/gameServer"
TARGET_BET = int(os.environ.get("PHOM_BET_XU", "100"))
PLAY_ENABLED = os.environ.get("PHOM_PLAY", "0") == "1"
MAX_GAMES = max(1, int(os.environ.get("PHOM_MAX_GAMES", "1")))
HOLD_SECONDS = int(os.environ.get("PHOM_HOLD_SECONDS", "10"))
ACTION_DELAY = float(os.environ.get("PHOM_ACTION_DELAY", "0.8"))
CAPTURE_DIR = Path(os.environ.get("PHOM_CAPTURE_DIR", "ws_capture/phom"))

CMD_NAMES = {
    300: "PONG", 301: "PING", 302: "LOGIN", 303: "ALERT",
    311: "BROADCAST", 314: "SET_CLIENT_MODE", 315: "CONFIG",
    331: "CHAT.SEND", 335: "CHAT.MSG",
    401: "ENTER_PLACE", 405: "CREATE_RULE", 406: "PLAYER_ENTERED",
    407: "PLAYER_EXITED", 408: "QUICK_PLAY", 410: "KICK_PLAYER",
    412: "LIST_ZONE_ROOM", 413: "LIST_BET_AMT", 414: "GET_TABLE_DATA",
    416: "SLOT_IN_TABLE_CHANGED", 417: "START_MATCH", 418: "GAMEOVER",
    419: "ENTER_STATE", 420: "SET_TURN", 421: "SET_PLAYER_STATUS",
    422: "SET_PLAYER_POINT", 423: "SET_PLAYER_ATTR", 433: "GET_TABLE_DATA_EX",
    434: "SET_READY", 501: "BET", 502: "PLAY", 518: "HIGHLIGHT",
    521: "TAKE_CARD", 522: "SHOW_PLAYER_CARD", 523: "CLEAR_CARDS",
    524: "SET_CARDS", 525: "SELECT_CARDS", 527: "COMPARE_BAND",
    528: "SEND_CARD", 529: "MOVE", 530: "CHANGE_PIECE",
    531: "SET_REMAIN_TURN", 532: "ADD_LOG", 533: "ASK_DRAW",
    534: "SURRENDER", 535: "RETREAT", 536: "ACCEPT", 537: "HIT",
    538: "STAY", 539: "FIRE_CARD", 540: "PASS_TURN", 541: "SORT_CARD",
    542: "TAKE", 543: "EAT", 544: "REMOVE", 545: "DROP_BAND",
    546: "SELECT_BAND", 547: "DROP_AVAILABLE_BAND", 548: "HINT",
    549: "SUBMIT", 601: "LOGIN_EX",
}
CMD_IDS = {name: code for code, name in CMD_NAMES.items()}


def utc_record(**fields):
    return {"timestamp": time.time(), **fields}


def append_jsonl(name: str, record: dict) -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    path = CAPTURE_DIR / name
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


class Codec:
    @staticmethod
    def pack_command(command: str | int, payload: bytes = b"") -> bytes:
        out = bytearray()
        if isinstance(command, str) and command in CMD_IDS:
            out.extend(struct.pack(">H", CMD_IDS[command]))
        elif isinstance(command, str):
            raw = command.encode("ascii")
            out.append((-len(raw)) & 0xFF)
            out.extend(raw)
        else:
            out.extend(struct.pack(">H", command))
        out.extend(payload)
        return bytes(out)

    @staticmethod
    def byte(value: int) -> bytes:
        return struct.pack(">b", value)

    @staticmethod
    def integer(value: int) -> bytes:
        return struct.pack(">i", value)

    @staticmethod
    def ascii(value: str) -> bytes:
        raw = value.encode("ascii")[:255]
        return struct.pack(">B", len(raw)) + raw

    @staticmethod
    def string(value: str) -> bytes:
        raw = value.encode("utf-16-be")
        return struct.pack(">h", len(raw) // 2) + raw

    @staticmethod
    def byte_array(values: list[int]) -> bytes:
        return struct.pack(">h", len(values)) + bytes(value & 0xFF for value in values)


class Incoming:
    def __init__(self, data: bytes):
        self.data = bytes(data)
        self.offset = 0
        self.command = self._command()

    def _command(self) -> str:
        first = self.byte()
        if first < 0:
            size = -first
            value = self.data[self.offset:self.offset + size].decode("ascii", "replace")
            self.offset += size
            return value
        second = self.data[self.offset]
        self.offset += 1
        code = ((first & 0xFF) << 8) | second
        return CMD_NAMES.get(code, str(code))

    def byte(self) -> int:
        value = struct.unpack_from(">b", self.data, self.offset)[0]
        self.offset += 1
        return value

    def short(self) -> int:
        value = struct.unpack_from(">h", self.data, self.offset)[0]
        self.offset += 2
        return value

    def integer(self) -> int:
        value = struct.unpack_from(">i", self.data, self.offset)[0]
        self.offset += 4
        return value

    def long(self) -> int:
        value = struct.unpack_from(">q", self.data, self.offset)[0]
        self.offset += 8
        return value

    def ascii(self) -> str:
        size = self.byte()
        if size < 0:
            size += 256
        value = self.data[self.offset:self.offset + size].decode("ascii", "replace")
        self.offset += size
        return value

    def string(self) -> str:
        chars = self.short()
        value = self.data[self.offset:self.offset + chars * 2].decode("utf-16-be", "replace")
        self.offset += chars * 2
        return value

    def byte_array(self) -> list[int]:
        size = self.short()
        if size < 0:
            raise ValueError(f"negative byte-array size: {size}")
        end = self.offset + size
        if end > len(self.data):
            raise ValueError("truncated byte array")
        value = list(self.data[self.offset:end])
        self.offset = end
        return value

    def remaining(self) -> int:
        return len(self.data) - self.offset


class PhomTableBot:
    def __init__(self, username: str, password: str, target_bet: int,
                 *, capture_enabled: bool = True, quiet: bool = False):
        self.username = username
        self.password = password
        self.target_bet = target_bet
        self.capture_enabled = capture_enabled
        self.quiet = quiet
        self.nickname = ""
        self.player_id = 0
        self.token = 0
        self.game_id = "0"
        self.place_path = "Lobby.0.1"
        self.http_cookie = ""
        self.ws = None
        self.connected = False
        self.logged_in = False
        self.created = False
        self.table_path = None
        self.bet_amounts = []
        self.states = {}
        self.begin_state_id = None
        self.current_state_id = None
        self.my_slot_id = -1
        self.is_playing = False
        self.board_lines = {}
        self.current_turn_slot = -1
        self.last_discard = None
        # Public, fair-play state retained for exactly one match.
        self.discarded_cards = []
        self.eaten_cards = []
        self.exposed_melds = []
        self._public_event_sequence = 0
        self.games_started = 0
        self.games_completed = 0
        self.pending_action = None
        self.blocked_send_state_id = None
        self.rejected_discards = set()
        self._state_sequence = 0
        self._acted_sequence = -1
        self.stop_event = threading.Event()
        self.created_event = threading.Event()
        self.seated_event = threading.Event()
        self._send_lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._enter_target = "lobby"
        self._last_receive = 0.0

    def fetch_session(self) -> None:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar),
            urllib.request.HTTPRedirectHandler(),
        )
        opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/139 Safari/537.36"),
            ("Accept-Language", "vi-VN,vi;q=0.9,en;q=0.7"),
        ]
        opener.open(LOGIN_URL, timeout=20).read()
        body = urllib.parse.urlencode({
            "redirect": "/",
            "USER_NAME": self.username,
            "PASSWORD": self.password,
            "AUTO_LOGIN": "true",
            "LOGIN": "Đăng nhập",
        }).encode()
        req = urllib.request.Request(
            LOGIN_URL,
            data=body,
            headers={
                "Origin": "https://gamevh.net",
                "Referer": LOGIN_URL,
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        opener.open(req, timeout=20).read()
        response = opener.open(GAME_URL, timeout=20)
        html = response.read().decode("utf-8", "replace")

        def required(pattern: str, label: str) -> str:
            match = re.search(pattern, html, re.I)
            if not match:
                raise RuntimeError(f"missing {label}; login may have failed")
            return match.group(1)

        self.token = int(required(r"var\s+token\s*=\s*(-?\d+)", "token"))
        self.nickname = required(
            r'''var\s+currentPlayerNickName\s*=\s*["']([^"']+)["']''',
            "nickname",
        ).strip()
        player_match = re.search(r"var\s+currentPlayerId\s*=\s*(\d+)", html, re.I)
        if player_match:
            self.player_id = int(player_match.group(1))
        game_match = re.search(r'''var\s+gameId\s*=\s*["']([^"']+)["']''', html, re.I)
        if game_match:
            self.game_id = game_match.group(1)
        place_match = re.search(r'''var\s+placePath\s*=\s*["']([^"']+)["']''', html, re.I)
        if place_match:
            self.place_path = place_match.group(1)
        self.http_cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
        print(
            f"[SESSION] login OK nick={self.nickname!r} id={self.player_id} "
            f"game={self.game_id!r} place={self.place_path!r}",
            flush=True,
        )

    def send(self, command: str | int, payload: bytes = b"", *, detail: dict | None = None) -> None:
        if not self.ws or not self.connected:
            return
        with self._send_lock:
            self.ws.send(Codec.pack_command(command, payload), opcode=websocket.ABNF.OPCODE_BINARY)
        append_jsonl("events.jsonl", utc_record(
            direction="out", command=str(command), payload_length=len(payload),
            detail=detail or {},
        ))

    def send_login(self) -> None:
        payload = b"".join((
            Codec.ascii(self.nickname),
            Codec.integer(self.token),
            Codec.ascii("5.0.2"),
            Codec.ascii(""),
            Codec.ascii(self.game_id),
            Codec.byte(1),
        ))
        # Do not persist the LOGIN payload because it contains the session token.
        with self._send_lock:
            self.ws.send(Codec.pack_command("LOGIN", payload), opcode=websocket.ABNF.OPCODE_BINARY)
        append_jsonl("events.jsonl", utc_record(
            direction="out", command="LOGIN", payload_length=len(payload), redacted=True,
        ))

    def enter_place(self, path: str, target: str) -> None:
        self._enter_target = target
        payload = Codec.ascii(path) + Codec.string("") + Codec.byte(1)
        self.send("ENTER_PLACE", payload, detail={"path": path, "target": target})

    def request_bets(self) -> None:
        self.send("LIST_BET_AMT")

    def create_table(self, bet_id: int) -> None:
        # Phỏm's create_table.js only adds optional password/formattedName.
        # For a normal public test table both are absent, so arg count is zero.
        payload = Codec.byte(bet_id) + Codec.byte(0)
        print(f"[CREATE] requesting public Phỏm table bet={self.target_bet} id={bet_id}", flush=True)
        self.send("CREATE_RULE", payload, detail={"bet_id": bet_id, "bet": self.target_bet})

    def request_table_data(self) -> None:
        self.send("GET_TABLE_DATA_EX", Codec.ascii(""))

    @staticmethod
    def _unsigned_count(value: int) -> int:
        return value + 256 if value < 0 else value

    def parse_state_data(self, message: Incoming) -> None:
        self.states = {}
        state_count = self._unsigned_count(message.byte())
        for _ in range(state_count):
            state_id = message.byte()
            state_code = message.ascii()
            mode = message.byte()
            command_count = self._unsigned_count(message.byte())
            commands = []
            for _ in range(command_count):
                commands.append({
                    "position": message.byte(),
                    "code": message.ascii(),
                    "name": message.string(),
                    "fill_board_state": message.byte() == 1,
                    "take_confirmation": message.byte() == 1,
                })
            self.states[state_id] = {
                "id": state_id,
                "code": state_code,
                "mode": mode,
                "commands": commands,
            }
        self.begin_state_id = message.byte()

    def parse_table_data(self, message: Incoming) -> dict:
        self.my_slot_id = message.byte()
        self.is_playing = message.byte() == 1
        player_count = self._unsigned_count(message.byte())
        players = []
        for _ in range(player_count):
            slot_id = message.byte()
            player_id = message.long()
            _full_name = message.string()
            _avatar_id = message.short()
            _avatar = message.ascii()
            _tag_id = message.byte()
            _chip_balance = message.long()
            _star_balance = message.long()
            _score = message.long()
            _level = message.byte()
            owner = message.byte() == 1
            players.append({"slot": slot_id, "player_id": player_id, "owner": owner})
        current_turn_slot = message.byte()
        self.current_turn_slot = current_turn_slot
        current_turn_timeout = message.short()
        player_remain_duration = message.short()
        self.current_state_id = message.byte()
        return {
            "my_slot": self.my_slot_id,
            "is_playing": self.is_playing,
            "players": players,
            "turn_slot": current_turn_slot,
            "turn_timeout": current_turn_timeout,
            "player_remain_duration": player_remain_duration,
            "state_id": self.current_state_id,
        }

    def parse_match_points(self, message: Incoming) -> dict[int, int]:
        count = self._unsigned_count(message.byte())
        return {message.byte(): message.integer() for _ in range(count)}

    def parse_board_data(self, message: Incoming) -> dict:
        slot_count = self._unsigned_count(message.byte())
        lines = {}
        for _ in range(slot_count):
            slot_id = message.byte()
            line_count = self._unsigned_count(message.byte())
            for _ in range(line_count):
                line_id = message.byte() - 1
                card_count = message.byte()
                if card_count < 0:
                    cards = message.byte_array()
                else:
                    cards = [-1] * card_count
                lines[(slot_id, line_id)] = cards
        self.board_lines = lines
        hand = lines.get((self.my_slot_id, 0), [])
        return {
            "slot_count": slot_count,
            "line_counts": {
                f"{slot}:{line}": len(cards)
                for (slot, line), cards in sorted(lines.items())
            },
            "hand": hand,
        }

    def reset_public_state(self) -> None:
        self.discarded_cards = []
        self.eaten_cards = []
        self.exposed_melds = []
        self._public_event_sequence = 0

    def public_state_snapshot(self) -> dict:
        return {
            "discarded_cards": [dict(item) for item in self.discarded_cards],
            "eaten_cards": [dict(item) for item in self.eaten_cards],
            "exposed_melds": [
                {"slot": item["slot"], "cards": list(item["cards"])}
                for item in self.exposed_melds
            ],
        }

    def rebuild_exposed_melds(self, slot_id: int | None = None) -> None:
        slots = ({slot for slot, line in self.board_lines if line == 1}
                 if slot_id is None else {slot_id})
        self.exposed_melds = [
            item for item in self.exposed_melds if item["slot"] not in slots
        ]
        for slot in sorted(slots):
            cards = [card for card in self.board_lines.get((slot, 1), [])
                     if 0 <= card < 52]
            _deadwood, groups = best_meld_partition(cards)
            for group in groups:
                self.exposed_melds.append({
                    "slot": slot,
                    "cards": sorted(group),
                })
        self.exposed_melds.sort(
            key=lambda item: (item["slot"], item["cards"][0], len(item["cards"]))
        )

    def record_public_state(self, reason: str) -> None:
        if not self.capture_enabled:
            return
        append_jsonl("events.jsonl", utc_record(
            direction="state",
            command="STATE_MANAGER",
            reason=reason,
            state=self.public_state_snapshot(),
        ))

    def record_ai_decision(self, decision: dict, state_code: str) -> None:
        """Persist an explainable AI decision without opponent hidden cards."""
        record = {
            "direction": "ai",
            "command": "AI_DECISION",
            "game": self.games_started,
            "state_id": self.current_state_id,
            "state_code": state_code,
            "hand": list(self.current_hand()),
            "public_state": self.public_state_snapshot(),
            "decision": decision,
        }
        if self.capture_enabled:
            append_jsonl("events.jsonl", utc_record(**record))
        if not self.quiet:
            chosen = (decision.get("chosen_card_name")
                      or decision.get("projected_discard_name")
                      or decision.get("action"))
            if chosen is None and decision.get("decision_type") == "eat":
                chosen = "EAT" if decision.get("eat") else "TAKE"
            if chosen is None:
                chosen = "none"
            print(
                f"[AI] {decision.get('decision_type')} phase={decision.get('phase')} "
                f"chosen={chosen} reason={decision.get('reason', '')}",
                flush=True,
            )

    def current_hand(self) -> list[int]:
        return [card for card in self.board_lines.get((self.my_slot_id, 0), [])
                if 0 <= card < 52]

    @staticmethod
    def describe_cards(cards: list[int]) -> list[str]:
        return [card_name(card) for card in cards]

    def exposed_meld_lines(self) -> list[list[int]]:
        return [
            [card for card in cards if 0 <= card < 52]
            for (slot_id, line_id), cards in self.board_lines.items()
            if slot_id != self.my_slot_id and line_id == 1
            and any(0 <= card < 52 for card in cards)
        ]

    def has_own_exposed_meld_marker(self) -> bool:
        # An eaten card is moved to our line 1. Live validation showed that a
        # natural meld still in line 0 is not enough to consign before laying.
        return any(0 <= card < 52
                   for card in self.board_lines.get((self.my_slot_id, 1), []))

    def own_eaten_cards(self) -> list[int]:
        return [
            item["card"] for item in self.eaten_cards
            if item["to_slot"] == self.my_slot_id
        ]

    def enter_state(self, state_id: int, source: str) -> None:
        if state_id != self.current_state_id:
            self.blocked_send_state_id = None
            self.rejected_discards.clear()
        self.current_state_id = state_id
        self._state_sequence += 1
        state = self.states.get(state_id)
        print(
            f"[STATE-IN] source={source} id={state_id} "
            f"code={(state or {}).get('code')!r} turn={self.current_turn_slot} "
            f"hand={self.describe_cards(self.current_hand())}",
            flush=True,
        )
        if PLAY_ENABLED:
            sequence = self._state_sequence
            threading.Timer(ACTION_DELAY, self.act_for_state, args=(sequence,)).start()

    def _state_command(self, state: dict, code: str) -> dict | None:
        return next((item for item in state.get("commands", [])
                     if item.get("code") == code), None)

    def send_state_action(self, state: dict, code: str,
                          selected_cards: list[int] | None = None) -> bool:
        command = self._state_command(state, code)
        if command is None:
            return False
        selected_cards = selected_cards or []
        payload = b""
        if command["fill_board_state"]:
            if not selected_cards:
                print(f"[ACTION] {code} requires cards but none selected", flush=True)
                return False
            if state["mode"] == 1:
                payload = Codec.byte(selected_cards[0])
            else:
                payload = Codec.byte_array(selected_cards)
        self.pending_action = {
            "code": code,
            "state_id": state["id"],
            "cards": list(selected_cards),
            "sent_at": time.time(),
        }
        print(
            f"[ACTION] send {code} cards={self.describe_cards(selected_cards)}",
            flush=True,
        )
        self.send(code, payload, detail={
            "state_id": state["id"], "cards": selected_cards,
        })
        return True

    def act_for_state(self, sequence: int) -> None:
        with self._action_lock:
            if not PLAY_ENABLED or self.stop_event.is_set():
                return
            if sequence != self._state_sequence or sequence == self._acted_sequence:
                return
            if self.pending_action is not None:
                return
            state = self.states.get(self.current_state_id)
            if state is None:
                print(f"[ACTION] unknown state {self.current_state_id}", flush=True)
                self.request_table_data()
                return
            if self.current_turn_slot not in (-1, self.my_slot_id):
                return

            code = state["code"]
            sent = False
            public_state = self.public_state_snapshot()
            hand = self.current_hand()
            required = self.own_eaten_cards()
            if code == "take":
                sent = self.send_state_action(state, "TAKE")
            elif code == "eat":
                eat_decision = assess_eat_decision(
                    hand, self.last_discard, public_state,
                    self.my_slot_id, required,
                )
                if self._state_command(state, "EAT") is None:
                    eat_decision["eat"] = False
                    eat_decision["reason"] = "eat-command-not-offered-by-server"
                self.record_ai_decision(eat_decision, code)
                sent = self.send_state_action(
                    state, "EAT" if eat_decision["eat"] else "TAKE"
                )
            elif code in ("remove", "finalRemove"):
                send_card = None
                if (code == "finalRemove"
                        and self.blocked_send_state_id != state["id"]
                        and self._state_command(state, "SEND_CARD")):
                    send_card, send_decision = choose_send_card_ai(
                        hand, public_state, self.my_slot_id, required
                    )
                    self.record_ai_decision(send_decision, code)
                if send_card is not None:
                    sent = self.send_state_action(state, "SEND_CARD", [send_card])
                else:
                    discard, discard_decision = choose_discard_ai(
                        hand,
                        public_state=public_state,
                        my_slot=self.my_slot_id,
                        required_cards=required,
                        forbidden_cards=self.rejected_discards,
                        final_round=(code == "finalRemove"),
                    )
                    self.record_ai_decision(discard_decision, code)
                    if discard is not None:
                        sent = self.send_state_action(state, "REMOVE", [discard])
                    else:
                        print("[ACTION] no legal hand card available; refreshing", flush=True)
                        self.request_table_data()
            elif code == "drop":
                # The server controls when laying is mandatory. AI may first
                # consign a nonworsening card, but never breaks a required meld.
                send_card = None
                if (self.blocked_send_state_id != state["id"]
                        and self.has_own_exposed_meld_marker()
                        and self._state_command(state, "SEND_CARD")):
                    send_card, send_decision = choose_send_card_ai(
                        hand, public_state, self.my_slot_id, required
                    )
                    self.record_ai_decision(send_decision, code)
                if send_card is not None:
                    sent = self.send_state_action(state, "SEND_CARD", [send_card])
                else:
                    deadwood, melds = best_meld_partition(
                        list(dict.fromkeys([*hand, *required]))
                    )
                    lay_decision = {
                        "policy_version": AI_POLICY_VERSION,
                        "decision_type": "lay",
                        "action": "DROP_AVAILABLE_BAND",
                        "reason": "authoritative-server-drop-state",
                        "deadwood_before_lay": deadwood,
                        "melds": [sorted(group) for group in melds],
                    }
                    self.record_ai_decision(lay_decision, code)
                    sent = self.send_state_action(state, "DROP_AVAILABLE_BAND")
            elif code == "waitTurn":
                return
            else:
                print(f"[ACTION] no policy for state {code!r}", flush=True)

            if sent:
                self._acted_sequence = sequence

    def handle_action_response(self, message: Incoming) -> bool:
        pending = self.pending_action
        if pending is None or message.command != pending["code"]:
            return False
        status = message.byte()
        error = ""
        if status != 0 and message.remaining() >= 2:
            error = message.string()
        append_jsonl("events.jsonl", utc_record(
            direction="parsed", command=message.command,
            status=status, error=error, pending=pending,
        ))
        print(
            f"[ACTION] result {message.command} status={status} error={error!r}",
            flush=True,
        )
        self.pending_action = None
        if status != 0:
            if message.command == "SEND_CARD":
                self.blocked_send_state_id = pending["state_id"]
            elif message.command == "REMOVE":
                self.rejected_discards.update(pending["cards"])
            # Refresh authoritative state instead of guessing after a rejection.
            self.request_table_data()
        elif message.command == "SEND_CARD":
            self.blocked_send_state_id = None
            # Sending does not necessarily leave the current state. Continue
            # after its MOVE event has updated the hand.
            self._state_sequence += 1
            sequence = self._state_sequence
            threading.Timer(ACTION_DELAY, self.act_for_state, args=(sequence,)).start()
        return True

    def apply_move_event(self, message: Incoming) -> None:
        cards = message.byte_array()
        source_slot = message.byte()
        source_line = message.byte() - 1
        target_slot = message.byte()
        target_line = message.byte() - 1
        target_index = message.byte()
        source_key = (source_slot, source_line)
        target_key = (target_slot, target_line)
        source_cards = self.board_lines.setdefault(source_key, [])
        moved = []
        for card in cards:
            if card in source_cards:
                source_cards.remove(card)
            elif -1 in source_cards:
                source_cards.remove(-1)
            moved.append(card)
        target_cards = self.board_lines.setdefault(target_key, [])
        if target_index < 0 or target_index > len(target_cards):
            target_cards.extend(moved)
        else:
            for offset, card in enumerate(moved):
                target_cards.insert(target_index + offset, card)
        # Live Phỏm uses line 3 for the public discard pile and line 1 for
        # eaten/laid/sent cards (both are zero-based here).
        reasons = []
        concrete = [card for card in moved if 0 <= card < 52]
        if source_line == 0 and target_line == 3 and concrete:
            self._public_event_sequence += 1
            for card in concrete:
                self.discarded_cards.append({
                    "sequence": self._public_event_sequence,
                    "card": card,
                    "by_slot": source_slot,
                })
            self.last_discard = concrete[-1]
            reasons.append("discard")
        if (source_line == 3 and target_line == 1
                and source_slot != target_slot and concrete):
            self._public_event_sequence += 1
            for card in concrete:
                self.eaten_cards.append({
                    "sequence": self._public_event_sequence,
                    "card": card,
                    "from_slot": source_slot,
                    "to_slot": target_slot,
                })
            self.last_discard = None
            reasons.append("eat")
        if source_line == 1:
            self.rebuild_exposed_melds(source_slot)
            reasons.append("exposed-source-update")
        if target_line == 1:
            self.rebuild_exposed_melds(target_slot)
            reasons.append("exposed-target-update")
        if reasons:
            if not any(reason in ("discard", "eat") for reason in reasons):
                self._public_event_sequence += 1
            self.record_public_state("+".join(reasons))
        if not self.quiet:
            print(
                f"[MOVE] cards={self.describe_cards(moved)} "
                f"{source_slot}:{source_line}->{target_slot}:{target_line} "
                f"hand={self.describe_cards(self.current_hand())}",
                flush=True,
            )

    def _capture_incoming(self, raw: bytes, message: Incoming) -> None:
        # These generic frames can contain a session cookie, balances, profile
        # data, or private cards. Keep command/length metadata but not payload.
        redacted = message.command in {
            "LOGIN", "CONFIG", "SLOT_IN_TABLE_CHANGED", "PLAYER_ENTERED",
            "SHOW_PLAYER_CARD", "START_MATCH", "ENTER_STATE",
        }
        append_jsonl("frames.jsonl", utc_record(
            direction="in",
            command=message.command,
            length=len(raw),
            payload_hex=None if redacted else raw[message.offset:message.offset + 256].hex(),
            redacted=redacted,
        ))

    def reset_capture(self) -> None:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        for name in ("frames.jsonl", "events.jsonl"):
            (CAPTURE_DIR / name).write_text("", encoding="utf-8")

    def on_open(self, ws) -> None:
        self.connected = True
        self._last_receive = time.time()
        print("[WS] connected", flush=True)
        self.send_login()

    def on_message(self, ws, raw) -> None:
        if not isinstance(raw, (bytes, bytearray)):
            return
        self._last_receive = time.time()
        try:
            message = Incoming(raw)
            # Keep exactly one match per dump, as requested. Reset before the
            # START_MATCH frame itself is recorded.
            if PLAY_ENABLED and message.command == "START_MATCH":
                self.reset_capture()
            self._capture_incoming(bytes(raw), message)
            self.handle(message)
        except Exception as exc:
            print(f"[WS] decode error: {exc}", flush=True)

    def handle(self, message: Incoming) -> None:
        command = message.command
        if command == "PING":
            self.send("PONG")
            return
        if self.handle_action_response(message):
            return
        if command == "LOGIN":
            status = message.byte()
            if status != 0:
                error = message.string() if message.remaining() >= 2 else ""
                raise RuntimeError(f"WS login failed status={status} error={error!r}")
            self.logged_in = True
            path = message.string()
            if path == "REFRESH":
                raise RuntimeError("server requested HTTP session refresh")
            if message.remaining() > 0:
                _ws_cookie = message.ascii()  # deliberately not stored/logged
            print("[WS] protocol login OK", flush=True)
            self.enter_place(self.place_path, "lobby")
            return
        if command == "ENTER_PLACE":
            status = message.byte()
            if status != 0:
                error = message.string() if message.remaining() >= 2 else ""
                print(f"[ENTER] target={self._enter_target} status={status} {error!r}", flush=True)
                return
            detail = {"target": self._enter_target}
            if message.remaining() >= 3:
                detail["currency"] = message.byte()
                detail["entrance_rate_tenths"] = message.short()
            append_jsonl("events.jsonl", utc_record(direction="parsed", command=command, detail=detail))
            print(f"[ENTER] {self._enter_target} OK", flush=True)
            if self._enter_target == "lobby" and not self.created:
                self.request_bets()
            elif self._enter_target == "table":
                self.created_event.set()
            return
        if command == "LIST_BET_AMT":
            status = message.byte()
            if status != 0:
                error = message.string() if message.remaining() >= 2 else ""
                raise RuntimeError(f"LIST_BET_AMT failed status={status} error={error!r}")
            count = message.byte()
            if count < 0:
                count += 256
            self.bet_amounts = [{"id": i, "value": message.integer()} for i in range(count)]
            append_jsonl("events.jsonl", utc_record(
                direction="parsed", command=command, bets=self.bet_amounts,
            ))
            print(f"[BET] {self.bet_amounts}", flush=True)
            exact = next((item for item in self.bet_amounts if item["value"] == self.target_bet), None)
            if exact is None:
                raise RuntimeError(f"exact bet {self.target_bet} not offered; refusing fallback")
            self.create_table(exact["id"])
            return
        if command == "GET_TABLE_DATA_EX":
            status = message.byte()
            if status != 0:
                error = message.string() if message.remaining() >= 2 else ""
                raise RuntimeError(
                    f"GET_TABLE_DATA_EX failed status={status} error={error!r}"
                )
            self.parse_state_data(message)
            table = self.parse_table_data(message)
            points = self.parse_match_points(message)
            board = self.parse_board_data(message)
            if self.is_playing:
                self.rebuild_exposed_melds()
            auto_start = message.byte() == 1
            currency = message.byte()
            arg_count = self._unsigned_count(message.byte())
            args = {message.ascii(): message.string() for _ in range(arg_count)}
            state_dump = [self.states[key] for key in sorted(self.states)]
            append_jsonl("events.jsonl", utc_record(
                direction="parsed", command=command,
                states=state_dump, begin_state_id=self.begin_state_id,
                table=table, points=points, board=board,
                auto_start=auto_start, currency=currency, args=args,
                public_state=self.public_state_snapshot(),
            ))
            print(
                f"[TABLE-DATA] my_slot={self.my_slot_id} playing={self.is_playing} "
                f"state={self.current_state_id} begin={self.begin_state_id} "
                f"hand={board['hand']}",
                flush=True,
            )
            for state in state_dump:
                summary = [
                    f"{item['code']}:{item['name']}"
                    f"{'[cards]' if item['fill_board_state'] else ''}"
                    for item in state["commands"]
                ]
                print(
                    f"[STATE] id={state['id']} code={state['code']!r} "
                    f"mode={state['mode']} commands={summary}",
                    flush=True,
                )
            if PLAY_ENABLED and self.is_playing and self.current_state_id in self.states:
                self.enter_state(self.current_state_id, "GET_TABLE_DATA_EX")
            return
        if command == "CREATE_RULE":
            status = message.byte()
            if status != 0:
                error = message.string() if message.remaining() >= 2 else ""
                raise RuntimeError(f"CREATE_RULE failed status={status} error={error!r}")
            table_id = message.ascii()
            self.table_path = (table_id if "." in table_id
                               else f"{self.place_path}.{table_id}")
            entrance_value = message.integer() if message.remaining() >= 4 else None
            self.created = True
            append_jsonl("events.jsonl", utc_record(
                direction="parsed", command=command,
                table_id=table_id, table_path=self.table_path,
                entrance_value=entrance_value,
            ))
            print(
                f"[CREATE] success table={self.table_path!r} "
                f"entrance_value={entrance_value}",
                flush=True,
            )
            # CREATE_RULE already moves this WebSocket session into the new
            # table. Sending ENTER_PLACE again closes/leaves the fresh table.
            self._enter_target = "table"
            self.created_event.set()
            self.request_table_data()
            if HOLD_SECONDS > 0 and not PLAY_ENABLED:
                print(
                    f"[SAFETY] capture-only probe will disconnect in "
                    f"{HOLD_SECONDS}s",
                    flush=True,
                )
                threading.Timer(HOLD_SECONDS, self.close).start()
            return
        if command == "START_MATCH":
            if not PLAY_ENABLED:
                print(
                    "[SAFETY] START_MATCH received in capture-only mode; "
                    "disconnecting",
                    flush=True,
                )
                threading.Thread(target=self.close, daemon=True).start()
                return
            self.reset_public_state()
            points = self.parse_match_points(message)
            board = self.parse_board_data(message)
            self.rebuild_exposed_melds()
            self.games_started += 1
            self.is_playing = True
            self.last_discard = None
            self.pending_action = None
            self.rejected_discards.clear()
            append_jsonl("events.jsonl", utc_record(
                direction="parsed", command=command,
                game=self.games_started, points=points, board=board,
                states=[self.states[key] for key in sorted(self.states)],
                begin_state_id=self.begin_state_id,
                public_state=self.public_state_snapshot(),
            ))
            print(
                f"[GAME] START #{self.games_started} "
                f"hand={self.describe_cards(board['hand'])}",
                flush=True,
            )
            if self.begin_state_id is not None:
                self.enter_state(self.begin_state_id, "START_MATCH")
            return
        if command == "SET_TURN":
            self.current_turn_slot = message.byte()
            turn_timeout = message.short()
            remain_duration = message.short() if message.remaining() >= 2 else None
            print(
                f"[TURN] slot={self.current_turn_slot} timeout={turn_timeout} "
                f"remain={remain_duration}",
                flush=True,
            )
            if PLAY_ENABLED and self.current_turn_slot == -2 and turn_timeout > 0:
                self.send("SET_READY")
            return
        if command == "ENTER_STATE":
            state_id = message.byte()
            self.enter_state(state_id, "ENTER_STATE")
            return
        if command == "MOVE":
            self.apply_move_event(message)
            return
        if command == "SET_CARDS":
            slot_id = message.byte()
            line_id = message.byte() - 1
            card_count = message.byte()
            cards = message.byte_array() if card_count < 0 else [-1] * card_count
            self.board_lines[(slot_id, line_id)] = cards
            if line_id == 1:
                self._public_event_sequence += 1
                self.rebuild_exposed_melds(slot_id)
                self.record_public_state("set-exposed-cards")
            print(
                f"[CARDS] set {slot_id}:{line_id} "
                f"count={len(cards)} hand={self.describe_cards(self.current_hand())}",
                flush=True,
            )
            return
        if command == "CLEAR_CARDS":
            slot_id = message.byte()
            line_id = message.byte() - 1
            self.board_lines[(slot_id, line_id)] = []
            if line_id == 1:
                self._public_event_sequence += 1
                self.rebuild_exposed_melds(slot_id)
                self.record_public_state("clear-exposed-cards")
            return
        if command == "SHOW_PLAYER_CARD":
            slot_id = message.byte()
            cards = message.byte_array()
            self.board_lines[(slot_id, 0)] = cards
            return
        if command == "GAMEOVER":
            count = self._unsigned_count(message.byte())
            results = []
            for _ in range(count):
                results.append({
                    "slot": message.byte(),
                    "grade": message.byte(),
                    "earn": message.long(),
                })
            result_text = message.string() if message.remaining() >= 2 else ""
            self.games_completed += 1
            self.is_playing = False
            # Keep a just-sent action pending: GameVH can deliver GAMEOVER
            # before the final command acknowledgement (observed for DROP).
            append_jsonl("events.jsonl", utc_record(
                direction="parsed", command=command,
                game=self.games_completed, results=results,
                result_text=result_text,
                public_state=self.public_state_snapshot(),
            ))
            print(
                f"[GAME] OVER #{self.games_completed} results={results} "
                f"text={result_text!r}",
                flush=True,
            )
            if self.games_completed >= MAX_GAMES:
                threading.Timer(1.0, self.close).start()
            else:
                threading.Timer(3.0, lambda: self.send("SET_READY")).start()
            return
        if command == "SLOT_IN_TABLE_CHANGED":
            _full_name = message.string()
            slot_id = message.byte()
            _chip_balance = message.long()
            _score = message.long()
            _level = message.byte()
            _avatar_id = message.short()
            _avatar = message.ascii()
            _tag_id = message.byte()
            owner = message.byte() == 1
            player_id = message.long()
            _star_balance = message.long()
            if player_id == self.player_id:
                self.seated_event.set()
                print(
                    f"[TABLE] seated slot={slot_id} owner={owner} "
                    f"table={self.table_path or '(creating)'}",
                    flush=True,
                )
            return
        if command == "ALERT":
            text = message.string() if message.remaining() >= 2 else ""
            print(f"[ALERT] {text}", flush=True)
            return
        print(f"[WS] {command} bytes_remaining={message.remaining()}", flush=True)

    def on_error(self, ws, error) -> None:
        print(f"[WS] error {type(error).__name__}: {error}", flush=True)

    def on_close(self, ws, code, reason) -> None:
        self.connected = False
        print(f"[WS] closed code={code} reason={reason!r}", flush=True)
        self.stop_event.set()

    def keepalive(self) -> None:
        while not self.stop_event.wait(10):
            if self.connected:
                self.send("PING")

    def run(self) -> None:
        self.fetch_session()
        # A new process and every START_MATCH start a fresh capture set.
        self.reset_capture()
        self.ws = websocket.WebSocketApp(
            WS_URL,
            cookie=self.http_cookie,
            on_open=self.on_open,
            on_message=self.on_message,
            on_error=self.on_error,
            on_close=self.on_close,
        )
        threading.Thread(target=self.keepalive, daemon=True).start()
        self.ws.run_forever(origin="https://gamevh.net", ping_interval=0)

    def close(self) -> None:
        self.stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass


def replay_capture(frames_path: str | Path,
                   events_path: str | Path | None = None) -> dict:
    """Replay public MOVE frames and compare with the normalized final state."""
    frames_path = Path(frames_path)
    if events_path is None:
        events_path = frames_path.with_name("events.jsonl")
    events_path = Path(events_path)
    bot = PhomTableBot(
        "replay", "replay", TARGET_BET,
        capture_enabled=False, quiet=True,
    )
    move_count = 0
    starts = 0
    errors = []
    for line_number, line in enumerate(
            frames_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            frame = json.loads(line)
            command = frame.get("command")
            if command == "START_MATCH":
                starts += 1
                bot.reset_public_state()
                bot.board_lines = {}
            elif command == "MOVE" and frame.get("payload_hex"):
                payload = bytes.fromhex(frame["payload_hex"])
                message = Incoming(Codec.pack_command("MOVE", payload))
                bot.apply_move_event(message)
                move_count += 1
        except Exception as exc:
            errors.append(f"frames line {line_number}: {type(exc).__name__}: {exc}")
    expected = None
    if events_path.exists():
        for line_number, line in enumerate(
                events_path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception as exc:
                errors.append(f"events line {line_number}: {type(exc).__name__}: {exc}")
                continue
            if event.get("command") == "STATE_MANAGER" and isinstance(event.get("state"), dict):
                expected = event["state"]
            elif event.get("command") == "GAMEOVER" and isinstance(event.get("public_state"), dict):
                expected = event["public_state"]
    actual = bot.public_state_snapshot()
    if expected is not None and actual != expected:
        errors.append("replayed public state does not match final normalized state")
    return {
        "ok": not errors,
        "policy_version": AI_POLICY_VERSION,
        "frames_path": str(frames_path),
        "events_path": str(events_path) if events_path.exists() else None,
        "matches_started": starts,
        "move_frames_replayed": move_count,
        "public_state": actual,
        "expected_state_found": expected is not None,
        "errors": errors,
    }


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--replay":
        frames = sys.argv[2] if len(sys.argv) >= 3 else "ws_capture/phom/frames.jsonl"
        events = sys.argv[3] if len(sys.argv) >= 4 else None
        try:
            report = replay_capture(frames, events)
        except Exception as exc:
            print(json.dumps({
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1

    username = os.environ.get("GAMEVH_USER", "").strip()
    password = os.environ.get("GAMEVH_PASSWORD", "")
    if not username or not password:
        print("Set GAMEVH_USER and GAMEVH_PASSWORD in the environment", file=sys.stderr)
        return 2
    bot = PhomTableBot(username, password, TARGET_BET)
    atexit.register(bot.close)
    signal.signal(signal.SIGTERM, lambda *_: bot.close())
    signal.signal(signal.SIGINT, lambda *_: bot.close())
    bot.run()
    return 0 if bot.created else 1


if __name__ == "__main__":
    raise SystemExit(main())
