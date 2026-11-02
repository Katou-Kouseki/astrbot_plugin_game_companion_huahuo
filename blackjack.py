from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .gomoku import Difficulty

RANKS: tuple[str, ...] = (
    "A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K",
)
SUITS: tuple[str, ...] = ("♠", "♥", "♦", "♣")
TEN_RANKS = frozenset({"10", "J", "Q", "K"})

HandStatus = Literal["playing", "stand", "bust", "blackjack", "surrendered"]
HandResult = Literal["", "blackjack_win", "win", "push", "loss"]
GamePhase = Literal["player_turns", "dealer_turn", "settled"]


def _best_value(cards: list[BlackjackCard]) -> tuple[int, bool]:
    """Return (best total, whether an ace is counted as 11)."""
    total = 0
    aces = 0
    for card in cards:
        if card.rank == "A":
            aces += 1
            total += 1
        elif card.rank in TEN_RANKS:
            total += 10
        else:
            total += int(card.rank)
    soft = False
    for _index in range(aces):
        if total + 10 <= 21:
            total += 10
            soft = True
    return total, soft


@dataclass(frozen=True, slots=True)
class BlackjackCard:
    """One immutable playing card. Suits are cosmetic; ranks drive the score."""

    rank: str
    suit: str

    def __post_init__(self) -> None:
        if self.rank not in RANKS or self.suit not in SUITS:
            raise ValueError("无效的扑克牌")

    def as_dict(self) -> dict[str, str]:
        return {"rank": self.rank, "suit": self.suit}


@dataclass(slots=True)
class BlackjackHand:
    """一手牌。

    ``uid`` 是房内稳定的访客标识（由访客 token 派生），用于在整局中定位这手牌；
    ``number`` 只是展示用的座位号。坐席在中途变动（掉线回收、换座）后座位号会被
    重编，只有 uid 不会变。
    """

    number: int
    uid: str = ""
    cards: list[BlackjackCard] = field(default_factory=list)
    status: HandStatus = "playing"
    result: HandResult = ""

    def add(self, card: BlackjackCard) -> None:
        self.cards.append(card)

    @property
    def value(self) -> int:
        return _best_value(self.cards)[0]

    @property
    def soft(self) -> bool:
        return _best_value(self.cards)[1]

    @property
    def busted(self) -> bool:
        return self.value > 21

    @property
    def blackjack(self) -> bool:
        return len(self.cards) == 2 and self.value == 21

    def as_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "number": self.number,
            "cards": [card.as_dict() for card in self.cards],
            "value": self.value,
            "soft": self.soft,
            "status": self.status,
            "result": self.result,
            "blackjack": self.blackjack,
            "bust": self.busted,
        }


def fresh_shoe(decks: int = 6) -> list[BlackjackCard]:
    """Build one freshly shuffled multi-deck shoe."""
    shoe = [
        BlackjackCard(rank=rank, suit=suit)
        for _deck in range(decks)
        for suit in SUITS
        for rank in RANKS
    ]
    random.shuffle(shoe)
    return shoe


@dataclass(slots=True)
class BlackjackGame:
    """Server-authoritative one-round Blackjack with the Bot as dealer.

    Rules implemented for this companion version:
    - A fresh six-deck shoe is shuffled for every round.
    - Ace counts as 1 or 11, whichever gives the best non-bust total.
    - A two-card 21 is a natural Blackjack and wins over a drawn 21.
    - The dealer checks its hole card immediately when its upcard is an ace or ten.
    - Players may only hit or stand; no doubling, splitting or insurance.
    - After all hands finish, the dealer reveals its hole card and draws to 17.
    - easy/normal stand on soft 17; hard hits soft 17 (a slightly stronger house).
    """

    difficulty: Difficulty = "normal"
    shoe: list[BlackjackCard] = field(default_factory=fresh_shoe)
    # 键是玩家 uid（稳定访客标识），不是座位号：座位号会在中途重编，uid 不会。
    hands: dict[str, BlackjackHand] = field(default_factory=dict)
    dealer_cards: list[BlackjackCard] = field(default_factory=list)
    dealer_hole: BlackjackCard | None = None
    phase: GamePhase = "player_turns"
    dealer_blackjack: bool = False
    finished: bool = False
    started_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def deal(
        cls,
        *,
        difficulty: Difficulty = "normal",
        player_numbers: list[int],
        player_uids: list[str] | None = None,
        shoe: list[BlackjackCard] | None = None,
    ) -> BlackjackGame:
        """Deal a complete round; deterministic tests may inject a shoe."""
        deck = list(shoe) if shoe is not None else fresh_shoe()
        numbers = [int(number) for number in player_numbers]
        if not numbers or len(set(numbers)) != len(numbers):
            raise ValueError("玩家编号不能为空或重复")
        # 未显式给 uid 时退回用编号字符串，保证旧调用与单元测试仍可用；
        # 真实房间一律传访客 uid，座位移位也不会认错手牌。
        uids = (
            [str(uid) for uid in player_uids]
            if player_uids is not None
            else [str(number) for number in numbers]
        )
        if len(uids) != len(numbers) or not all(uids):
            raise ValueError("玩家标识数量必须与玩家数量一致")
        if len(set(uids)) != len(uids):
            raise ValueError("玩家标识不能重复")
        if len(deck) < len(numbers) * 2 + 2:
            raise ValueError("牌堆不足，无法完成开局")
        game = cls(difficulty=difficulty, shoe=deck)
        for number, uid in zip(numbers, uids):
            hand = BlackjackHand(number=number, uid=uid)
            hand.add(game._draw())
            hand.add(game._draw())
            hand.status = "blackjack" if hand.blackjack else "playing"
            game.hands[uid] = hand
        game.dealer_cards.append(game._draw())
        game.dealer_hole = game._draw()
        if game._dealer_peeks_blackjack():
            game.dealer_blackjack = True
            game.reveal_dealer()
            game.settle()
        return game

    @property
    def dealer_upcard(self) -> BlackjackCard | None:
        return self.dealer_cards[0] if self.dealer_cards else None

    @property
    def dealer_total(self) -> int:
        cards = list(self.dealer_cards)
        if self.dealer_hole is not None:
            cards.append(self.dealer_hole)
        return _best_value(cards)[0]

    @property
    def dealer_soft(self) -> bool:
        cards = list(self.dealer_cards)
        if self.dealer_hole is not None:
            cards.append(self.dealer_hole)
        return _best_value(cards)[1]

    @property
    def dealer_must_hit(self) -> bool:
        if self.finished or self.phase != "dealer_turn":
            return False
        total, soft = self.dealer_total, self.dealer_soft
        if total > 17:
            return False
        if total == 17:
            return soft and self.difficulty == "hard"
        return True

    @property
    def results(self) -> dict[str, HandResult]:
        """按玩家 uid 返回结算结果。"""
        return {uid: hand.result for uid, hand in self.hands.items()}

    def results_by_number(self) -> dict[int, HandResult]:
        """按座位号返回结算结果，仅用于展示与历史文案。"""
        return {hand.number: hand.result for hand in self.hands.values()}

    def hand_for_uid(self, uid: str) -> BlackjackHand | None:
        return self.hands.get(str(uid))

    def hand_for_number(self, number: int) -> BlackjackHand | None:
        return next(
            (hand for hand in self.hands.values() if hand.number == int(number)),
            None,
        )

    def all_players_done(self) -> bool:
        return all(hand.status != "playing" for hand in self.hands.values())

    def has_pending_hands(self) -> bool:
        """Return whether the dealer still needs to play for any comparison."""
        return any(
            not hand.result and hand.status in {"stand", "blackjack"}
            for hand in self.hands.values()
        )

    def reveal_dealer(self) -> None:
        if self.dealer_hole is None:
            return
        self.dealer_cards.append(self.dealer_hole)
        self.dealer_hole = None

    def draw_dealer(self) -> BlackjackCard:
        card = self._draw()
        self.dealer_cards.append(card)
        return card

    def hit(self, key: str | int) -> dict[str, Any]:
        hand = self._hand(key)
        if hand.status != "playing":
            raise ValueError("这手牌已经不需要再要牌")
        card = self._draw()
        hand.add(card)
        event: dict[str, Any] = {
            "action": "hit",
            "uid": hand.uid,
            "number": hand.number,
            "card": card.as_dict(),
            "value": hand.value,
            "soft": hand.soft,
        }
        if hand.busted:
            hand.status = "bust"
            hand.result = "loss"
            event["bust"] = True
        elif hand.value == 21:
            hand.status = "stand"
            event["at_21"] = True
        self._append_event(event)
        return event

    def stand(self, key: str | int) -> dict[str, Any]:
        hand = self._hand(key)
        if hand.status != "playing":
            raise ValueError("这手牌已经停牌")
        hand.status = "stand"
        event = {
            "action": "stand",
            "uid": hand.uid,
            "number": hand.number,
            "value": hand.value,
        }
        self._append_event(event)
        return event

    def surrender(self, key: str | int) -> dict[str, Any]:
        hand = self._hand(key)
        if hand.status not in {"playing", "stand", "blackjack"}:
            raise ValueError("这手牌已经结算")
        hand.status = "surrendered"
        hand.result = "loss"
        event = {
            "action": "surrender",
            "uid": hand.uid,
            "number": hand.number,
            "value": hand.value,
        }
        self._append_event(event)
        return event

    def settle(self) -> dict[str, HandResult]:
        """Reveal and settle every hand. Idempotent after the first call."""
        if self.finished:
            return self.results
        self.reveal_dealer()
        dealer_total = self.dealer_total
        for hand in self.hands.values():
            if hand.result:
                continue
            if self.dealer_blackjack:
                hand.result = "push" if hand.blackjack else "loss"
            elif hand.blackjack:
                hand.result = "blackjack_win"
            elif dealer_total > 21 or hand.value > dealer_total:
                hand.result = "win"
            elif hand.value == dealer_total:
                hand.result = "push"
            else:
                hand.result = "loss"
        for hand in self.hands.values():
            if hand.status == "playing":
                hand.status = "stand"
        self.phase = "settled"
        self.finished = True
        self._append_event(
            {
                "action": "settle",
                "dealer_total": dealer_total,
                "dealer_soft": self.dealer_soft,
                "results": {
                    str(number): value
                    for number, value in self.results_by_number().items()
                },
            }
        )
        return self.results

    def snapshot(self) -> dict[str, Any]:
        hidden = self.phase == "player_turns" and not self.finished
        dealer_cards = [card.as_dict() for card in self.dealer_cards]
        if hidden:
            visible = dealer_cards[:1]
            dealer_total, dealer_soft = (
                _best_value(self.dealer_cards[:1])
                if self.dealer_cards
                else (0, False)
            )
        else:
            visible = dealer_cards
            dealer_total, dealer_soft = self.dealer_total, self.dealer_soft
        return {
            "kind": "blackjack",
            "phase": self.phase,
            "finished": self.finished,
            "dealer_blackjack": self.dealer_blackjack,
            "dealer_cards": visible,
            "dealer_hidden": hidden,
            "dealer_total": dealer_total,
            "dealer_soft": dealer_soft,
            "hands": {
                uid: hand.as_dict() for uid, hand in self.hands.items()
            },
            "results": {str(key): value for key, value in self.results.items()},
            "results_by_number": {
                str(number): value
                for number, value in self.results_by_number().items()
            },
            "history": self.history[-24:],
        }

    def _dealer_peeks_blackjack(self) -> bool:
        upcard = self.dealer_upcard
        return bool(
            upcard
            and upcard.rank in {"A", *TEN_RANKS}
            and self.dealer_hole is not None
            and _best_value([upcard, self.dealer_hole])[0] == 21
        )

    def _draw(self) -> BlackjackCard:
        if not self.shoe:
            raise ValueError("牌堆已经用完")
        return self.shoe.pop()

    def _hand(self, key: str | int) -> BlackjackHand:
        """按 uid 定位手牌；传座位号时仅在能唯一命中时回退，兼容旧调用。"""
        if isinstance(key, str):
            hand = self.hands.get(key)
            if hand is not None:
                return hand
            try:
                number = int(key)
            except (TypeError, ValueError):
                raise ValueError("这手牌不属于本局玩家") from None
        else:
            number = int(key)
        hand = self.hand_for_number(number)
        if hand is None:
            raise ValueError("这手牌不属于本局玩家")
        return hand

    def _append_event(self, event: dict[str, Any]) -> None:
        self.history.append({**event, "at": time.time()})
