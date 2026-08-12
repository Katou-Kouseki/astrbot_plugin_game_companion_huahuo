from __future__ import annotations

import pytest
from astrbot_plugin_game_companion.blackjack import (
    BlackjackCard,
    BlackjackGame,
    _best_value,
    fresh_shoe,
)


def fixed_shoe(ranks: list[str]) -> list[BlackjackCard]:
    """Build a shoe whose cards are dealt in the given rank order."""
    suits = ("♠", "♥", "♦", "♣")
    return [
        BlackjackCard(rank=rank, suit=suits[index % 4])
        for index, rank in enumerate(reversed(ranks))
    ]


def test_fresh_shoe_contains_six_complete_decks() -> None:
    shoe = fresh_shoe()

    assert len(shoe) == 6 * 52
    assert len({(card.rank, card.suit) for card in shoe}) == 52
    assert sum(card.rank == "A" for card in shoe) == 24


@pytest.mark.parametrize(
    ("cards", "expected"),
    [
        ([BlackjackCard("A", "♠"), BlackjackCard("7", "♥")], (18, True)),
        ([BlackjackCard("A", "♠"), BlackjackCard("6", "♥")], (17, True)),
        ([BlackjackCard("A", "♠"), BlackjackCard("10", "♥"), BlackjackCard("A", "♦")], (12, False)),
        ([BlackjackCard("K", "♠"), BlackjackCard("Q", "♥"), BlackjackCard("5", "♦")], (25, False)),
    ],
)
def test_best_value_handles_soft_and_bust_totals(
    cards: list[BlackjackCard], expected: tuple[int, bool]
) -> None:
    assert _best_value(cards) == expected


def test_deal_gives_two_cards_each_and_one_hidden_dealer_card() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1, 2],
        shoe=fixed_shoe(["A", "2", "K", "9", "7", "5", "3", "4"]),
    )

    assert list(game.hands) == [1, 2]
    assert [card.rank for card in game.hands[1].cards] == ["A", "2"]
    assert game.hands[1].value == 13
    assert game.dealer_cards[0].rank == "7"
    assert game.dealer_hole is not None
    assert game.dealer_hole.rank == "5"
    snapshot = game.snapshot()
    assert snapshot["dealer_hidden"] is True
    assert len(snapshot["dealer_cards"]) == 1


def test_natural_blackjack_is_recognised_at_deal_time() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1],
        shoe=fixed_shoe(["A", "K", "9", "8"]),
    )

    hand = game.hands[1]
    assert hand.blackjack is True
    assert hand.status == "blackjack"
    assert game.all_players_done() is True


def test_dealer_natural_is_peeked_and_settles_immediately() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1],
        shoe=fixed_shoe(["10", "3", "K", "A"]),
    )

    assert game.dealer_blackjack is True
    assert game.finished is True
    assert game.phase == "settled"
    assert game.results == {1: "loss"}
    assert game.snapshot()["dealer_hidden"] is False


def test_hit_draws_busts_and_auto_stands_at_21() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1],
        shoe=fixed_shoe(["10", "6", "9", "8", "4", "5"]),
    )

    event = game.hit(1)
    assert event["action"] == "hit"
    assert event["value"] == 20
    game.hit(1)
    assert game.hands[1].value == 25
    assert game.hands[1].busted is True
    assert game.hands[1].result == "loss"

    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1],
        shoe=fixed_shoe(["10", "6", "9", "8", "5"]),
    )
    game.hit(1)
    assert game.hands[1].value == 21
    assert game.hands[1].status == "stand"


@pytest.mark.parametrize(
    ("difficulty", "must_hit"),
    [("easy", False), ("normal", False), ("hard", True)],
)
def test_dealer_soft_17_policy_follows_difficulty(
    difficulty: str, must_hit: bool
) -> None:
    game = BlackjackGame.deal(
        difficulty=difficulty,
        player_numbers=[1],
        shoe=fixed_shoe(["9", "5", "A", "6", "2"]),
    )
    game.stand(1)
    game.phase = "dealer_turn"

    assert game.dealer_total == 17
    assert game.dealer_soft is True
    assert game.dealer_must_hit is must_hit


@pytest.mark.parametrize(
    ("ranks", "expected"),
    [
        (["7", "7", "9", "2", "2", "2", "K"], "win"),
        (["10", "10", "10", "10", "2"], "push"),
        (["10", "9", "10", "10", "2"], "loss"),
    ],
)
def test_settlement_compares_dealer_total(ranks: list[str], expected: str) -> None:
    game = BlackjackGame.deal(
        difficulty="normal", player_numbers=[1], shoe=fixed_shoe(ranks)
    )
    game.stand(1)
    game.phase = "dealer_turn"
    while game.dealer_must_hit:
        game.draw_dealer()
    game.settle()

    assert game.results == {1: expected}
    assert game.finished is True


def test_player_blackjack_beats_a_drawn_dealer_21() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1],
        shoe=fixed_shoe(["A", "K", "3", "8", "10"]),
    )
    game.phase = "dealer_turn"
    while game.dealer_must_hit:
        game.draw_dealer()
    game.settle()

    assert game.dealer_total == 21
    assert game.results == {1: "blackjack_win"}


def test_surrender_marks_the_hand_as_a_loss() -> None:
    game = BlackjackGame.deal(
        difficulty="normal",
        player_numbers=[1, 2],
        shoe=fixed_shoe(["10", "6", "10", "8", "9", "8"]),
    )

    game.surrender(1)

    assert game.hands[1].status == "surrendered"
    assert game.hands[1].result == "loss"
    assert game.all_players_done() is False


@pytest.mark.parametrize("player_numbers", [[], [1, 1]])
def test_deal_rejects_empty_or_duplicate_players(player_numbers: list[int]) -> None:
    with pytest.raises(ValueError, match="玩家编号"):
        BlackjackGame.deal(player_numbers=player_numbers)
