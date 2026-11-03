from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from typing import Literal

BOARD_SIZE = 15
EMPTY = 0
BLACK = 1
WHITE = 2

Difficulty = Literal["easy", "normal", "hard"]


@dataclass(frozen=True, slots=True)
class GomokuMoveThreat:
    """A threat created by one concrete move, not a whole-board estimate."""

    kind: Literal["win", "multiple", "single", ""]
    row: int
    column: int
    color: int
    winning_points: tuple[tuple[int, int], ...] = ()


@dataclass(slots=True)
class GomokuGame:
    """A freestyle Gomoku game with a bounded heuristic search opponent."""

    human_color: int = BLACK
    difficulty: Difficulty = "normal"
    board: list[list[int]] = field(
        default_factory=lambda: [
            [EMPTY for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
    )
    turn: int = BLACK
    winner: int = EMPTY
    draw: bool = False
    history: list[tuple[int, int, int]] = field(default_factory=list)

    @property
    def bot_color(self) -> int:
        return WHITE if self.human_color == BLACK else BLACK

    @property
    def finished(self) -> bool:
        return self.winner != EMPTY or self.draw

    def place(self, row: int, column: int, color: int) -> None:
        """Place one legal stone and update the result.

        Args:
            row: Zero-based board row.
            column: Zero-based board column.
            color: BLACK or WHITE.

        Raises:
            ValueError: If the move is out of turn, invalid, or the game ended.
        """
        if self.finished:
            raise ValueError("对局已经结束")
        if color not in {BLACK, WHITE} or color != self.turn:
            raise ValueError("现在还没有轮到这一方")
        if not (0 <= row < BOARD_SIZE and 0 <= column < BOARD_SIZE):
            raise ValueError("落子位置超出棋盘")
        if self.board[row][column] != EMPTY:
            raise ValueError("这个位置已经有棋子")
        self.board[row][column] = color
        self.history.append((row, column, color))
        if self._is_win(row, column, color):
            self.winner = color
            return
        if len(self.history) == BOARD_SIZE * BOARD_SIZE:
            self.draw = True
            return
        self.turn = WHITE if color == BLACK else BLACK

    def choose_bot_move(
        self, *, seed: int | None = None, time_limit: float = 0.45
    ) -> tuple[int, int]:
        """Choose a move for the configured difficulty within a time budget.

        Args:
            seed: Optional deterministic random seed used by tests and easy mode.
            time_limit: Maximum hard-mode search time in seconds.

        Returns:
            A legal ``(row, column)`` move.

        Raises:
            ValueError: If it is not the Bot's turn or no move is available.
        """
        if self.finished or self.turn != self.bot_color:
            raise ValueError("现在不是 Bot 的回合")
        candidates = self._candidate_moves()
        if not candidates:
            raise ValueError("棋盘上没有可落子位置")
        rng = random.Random(seed)
        winning = self._immediate_move(self.bot_color, candidates)
        if winning is not None:
            return winning
        blocking = self._immediate_move(self.human_color, candidates)
        if blocking is not None and self.difficulty != "easy":
            return blocking

        ranked = sorted(
            candidates,
            key=lambda move: self._move_score(move[0], move[1], self.bot_color),
            reverse=True,
        )
        if self.difficulty == "easy":
            if blocking is not None and rng.random() < 0.58:
                return blocking
            pool = ranked[: min(10, len(ranked))]
            weights = list(range(len(pool), 0, -1))
            return rng.choices(pool, weights=weights, k=1)[0]
        if self.difficulty == "normal" or len(self.history) < 2:
            return ranked[0]

        deadline = time.monotonic() + max(0.05, min(time_limit, 1.5))
        # 困难：先找「一手做出双威胁」的杀着（双四 / 四三 / 双活三），能直接锁定胜局。
        # 旧实现只做「最小化对手收益」的两层搜索，价值里没有自己的进攻分，
        # 结果反而比普通档的贪心更被动（用户实测困难等于送棋）。
        for row, column in ranked[:12]:
            self.board[row][column] = self.bot_color
            threat = self.move_threat(row, column, self.bot_color)
            self.board[row][column] = EMPTY
            if len(threat.winning_points) >= 2:
                return row, column
            fours, threes = self._threat_shape(row, column, self.bot_color)
            if (fours >= 1 and threes >= 1) or threes >= 2:
                return row, column
        # 否则做一层「自己收益 − 对手最强回应」的评估，取综合最优
        best_move = ranked[0]
        best_value = -math.inf
        for row, column in ranked[:10]:
            if time.monotonic() >= deadline:
                break
            attack = sum(self._direction_scores(row, column, self.bot_color))
            self.board[row][column] = self.bot_color
            value = attack - 1.2 * self._best_reply_gain(deadline)
            self.board[row][column] = EMPTY
            if value > best_value:
                best_value = value
                best_move = (row, column)
        return best_move

    def undo_round(self) -> int:
        """Undo the latest human move and a following Bot response.

        Returns:
            Number of removed stones.

        Raises:
            ValueError: If no human move exists to undo.
        """
        human_index = next(
            (
                index
                for index in range(len(self.history) - 1, -1, -1)
                if self.history[index][2] == self.human_color
            ),
            -1,
        )
        if human_index < 0:
            raise ValueError("还没有可以悔掉的玩家落子")
        removed = self.history[human_index:]
        for row, column, _color in removed:
            self.board[row][column] = EMPTY
        del self.history[human_index:]
        self.winner = EMPTY
        self.draw = False
        self.turn = self.human_color
        return len(removed)

    def tactical_state(self, color: int) -> str:
        """Return a compact tactical label for commentary throttling.

        Args:
            color: Side to inspect.

        Returns:
            ``win``, ``four``, ``three``, or an empty string.
        """
        if self.winner == color:
            return "win"
        best = 0
        for row, column in self._candidate_moves():
            best = max(best, self._line_potential(row, column, color))
        if best >= 4:
            return "four"
        if best >= 3:
            return "three"
        return ""

    def move_threat(self, row: int, column: int, color: int) -> GomokuMoveThreat:
        """Describe immediate winning points created through the given move."""
        if (
            color not in {BLACK, WHITE}
            or not (0 <= row < BOARD_SIZE and 0 <= column < BOARD_SIZE)
            or self.board[row][column] != color
        ):
            return GomokuMoveThreat("", row, column, color)
        if self._is_win(row, column, color):
            return GomokuMoveThreat("win", row, column, color)

        winning_points: set[tuple[int, int]] = set()
        for delta_row, delta_column in ((1, 0), (0, 1), (1, 1), (1, -1)):
            for offset in range(-4, 1):
                cells = [
                    (
                        row + (offset + step) * delta_row,
                        column + (offset + step) * delta_column,
                    )
                    for step in range(5)
                ]
                if not all(
                    0 <= scan_row < BOARD_SIZE
                    and 0 <= scan_column < BOARD_SIZE
                    for scan_row, scan_column in cells
                ):
                    continue
                values = [self.board[scan_row][scan_column] for scan_row, scan_column in cells]
                if values.count(color) == 4 and values.count(EMPTY) == 1:
                    winning_points.add(cells[values.index(EMPTY)])

        ordered_points = tuple(sorted(winning_points))
        kind: Literal["multiple", "single", ""] = (
            "multiple" if len(ordered_points) >= 2 else "single" if ordered_points else ""
        )
        return GomokuMoveThreat(kind, row, column, color, ordered_points)

    def snapshot(self) -> dict[str, object]:
        """Return the browser-safe game state."""
        return {
            "board": self.board,
            "turn": self.turn,
            "winner": self.winner,
            "draw": self.draw,
            "human_color": self.human_color,
            "bot_color": self.bot_color,
            "difficulty": self.difficulty,
            "move_count": len(self.history),
            "last_move": list(self.history[-1][:2]) if self.history else None,
        }

    def _is_win(self, row: int, column: int, color: int) -> bool:
        for delta_row, delta_column in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for direction in (-1, 1):
                step = 1
                while True:
                    scan_row = row + direction * step * delta_row
                    scan_column = column + direction * step * delta_column
                    if not (
                        0 <= scan_row < BOARD_SIZE
                        and 0 <= scan_column < BOARD_SIZE
                        and self.board[scan_row][scan_column] == color
                    ):
                        break
                    count += 1
                    step += 1
            if count >= 5:
                return True
        return False

    def _candidate_moves(self) -> list[tuple[int, int]]:
        if not self.history:
            return [(BOARD_SIZE // 2, BOARD_SIZE // 2)]
        candidates: set[tuple[int, int]] = set()
        for row, column, _color in self.history:
            for delta_row in range(-2, 3):
                for delta_column in range(-2, 3):
                    scan_row = row + delta_row
                    scan_column = column + delta_column
                    if (
                        0 <= scan_row < BOARD_SIZE
                        and 0 <= scan_column < BOARD_SIZE
                        and self.board[scan_row][scan_column] == EMPTY
                    ):
                        candidates.add((scan_row, scan_column))
        return list(candidates)

    def _immediate_move(
        self, color: int, candidates: list[tuple[int, int]]
    ) -> tuple[int, int] | None:
        for row, column in candidates:
            self.board[row][column] = color
            won = self._is_win(row, column, color)
            self.board[row][column] = EMPTY
            if won:
                return row, column
        return None

    def _move_score(self, row: int, column: int, color: int) -> float:
        attack = self._pattern_score(row, column, color)
        defense = self._pattern_score(row, column, WHITE if color == BLACK else BLACK)
        center = BOARD_SIZE - abs(row - BOARD_SIZE // 2) - abs(column - BOARD_SIZE // 2)
        return attack * 1.12 + defense + center * 0.2

    def _pattern_score(self, row: int, column: int, color: int) -> float:
        return float(sum(self._direction_scores(row, column, color)))

    @staticmethod
    def _run_score(count: int, open_ends: int) -> float:
        """单方向棋型分值：连子数 + 空端数 → 价值。

        排序（从高到低）：成五 > 活四 > 活三 > 冲四 > 眠三 > 活二。
        关键在「活三 > 冲四」：单独冲四只是逼对手封一手、封完就死，
        活三下一手能变活四（对手无法双封），威胁更大；死四（两端都被堵）
        完全是白费一手，分值归零。
        """
        if count >= 5:
            return 1_000_000.0
        if count == 4:
            if open_ends == 2:
                return 200_000.0  # 活四：必胜
            if open_ends == 1:
                return 10_000.0  # 冲四：逼应，但单独不致命
            return 0.0  # 死四：两端都被堵，纯属浪费
        if count == 3:
            if open_ends == 2:
                return 15_000.0  # 活三：强威胁，高于冲四
            if open_ends == 1:
                return 1_200.0  # 眠三
            return 100.0
        if count == 2:
            if open_ends == 2:
                return 500.0
            if open_ends == 1:
                return 120.0
            return 20.0
        return float(open_ends * 4)

    def _direction_runs(
        self, row: int, column: int, color: int
    ) -> list[tuple[int, int]]:
        """临时落子后，四个方向各自的 (连子数, 空端数)，结束时恢复空位。"""
        self.board[row][column] = color
        runs: list[tuple[int, int]] = []
        for delta_row, delta_column in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            open_ends = 0
            for direction in (-1, 1):
                step = 1
                while True:
                    scan_row = row + direction * step * delta_row
                    scan_column = column + direction * step * delta_column
                    if not (
                        0 <= scan_row < BOARD_SIZE and 0 <= scan_column < BOARD_SIZE
                    ):
                        break
                    if self.board[scan_row][scan_column] == color:
                        count += 1
                        step += 1
                        continue
                    if self.board[scan_row][scan_column] == EMPTY:
                        open_ends += 1
                    break
            runs.append((count, open_ends))
        self.board[row][column] = EMPTY
        return runs

    def _direction_scores(self, row: int, column: int, color: int) -> list[float]:
        return [
            self._run_score(count, open_ends)
            for count, open_ends in self._direction_runs(row, column, color)
        ]

    def _threat_shape(self, row: int, column: int, color: int) -> tuple[int, int]:
        """该点成四的条数与活三条数（用于识别双威胁：四三 / 双活三）。"""
        fours = 0
        threes = 0
        for count, open_ends in self._direction_runs(row, column, color):
            if count == 4 and open_ends >= 1:
                fours += 1
            elif count == 3 and open_ends == 2:
                threes += 1
        return fours, threes

    def _best_reply_gain(self, deadline: float) -> float:
        """对手一步能拿到的最好收益；用来惩罚「给对手送机会」的落点。

        调用前当前落子应已在盘面上（由调用方放置/还原）。
        """
        replies = sorted(
            self._candidate_moves(),
            key=lambda move: self._move_score(move[0], move[1], self.human_color),
            reverse=True,
        )[:8]
        worst = 0.0
        for row, column in replies:
            if time.monotonic() >= deadline:
                break
            self.board[row][column] = self.human_color
            won = self._is_win(row, column, self.human_color)
            self.board[row][column] = EMPTY
            if won:
                return 2_000_000.0
            worst = max(worst, sum(self._direction_scores(row, column, self.human_color)))
        return worst

    def _line_potential(self, row: int, column: int, color: int) -> int:
        self.board[row][column] = color
        longest = 1
        for delta_row, delta_column in ((1, 0), (0, 1), (1, 1), (1, -1)):
            count = 1
            for direction in (-1, 1):
                step = 1
                while True:
                    scan_row = row + direction * step * delta_row
                    scan_column = column + direction * step * delta_column
                    if not (
                        0 <= scan_row < BOARD_SIZE
                        and 0 <= scan_column < BOARD_SIZE
                        and self.board[scan_row][scan_column] == color
                    ):
                        break
                    count += 1
                    step += 1
            longest = max(longest, count)
        self.board[row][column] = EMPTY
        return longest
