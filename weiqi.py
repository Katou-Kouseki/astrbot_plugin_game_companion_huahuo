from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass, field

from .gomoku import BLACK, EMPTY, WHITE, Difficulty

# 围棋盘路：9 路适合快局，13 路是默认值，19 路为标准盘。
SIZES: tuple[int, ...] = (9, 13, 19)
DEFAULT_SIZE = 13
DEFAULT_KOMI = 7.5


def _neighbors(row: int, column: int, size: int):
    if row > 0:
        yield row - 1, column
    if row + 1 < size:
        yield row + 1, column
    if column > 0:
        yield row, column - 1
    if column + 1 < size:
        yield row, column + 1


@dataclass(frozen=True, slots=True)
class _Group:
    stones: tuple[tuple[int, int], ...]
    liberties: tuple[tuple[int, int], ...]


def _group_at(board: list[list[int]], row: int, column: int) -> _Group:
    """返回 (row, column) 所在棋块的全部棋子与气。"""
    color = board[row][column]
    if color == EMPTY:
        return _Group((), ())
    size = len(board)
    stones: list[tuple[int, int]] = []
    liberties: set[tuple[int, int]] = set()
    stack = [(row, column)]
    seen = {(row, column)}
    while stack:
        current_row, current_column = stack.pop()
        stones.append((current_row, current_column))
        for next_row, next_column in _neighbors(current_row, current_column, size):
            value = board[next_row][next_column]
            if value == EMPTY:
                liberties.add((next_row, next_column))
            elif value == color and (next_row, next_column) not in seen:
                seen.add((next_row, next_column))
                stack.append((next_row, next_column))
    return _Group(tuple(stones), tuple(liberties))


def _play_stone(
    board: list[list[int]], row: int, column: int, color: int
) -> tuple[list[tuple[int, int]], list[list[int]]]:
    """在副本上落子并提掉无气的对方棋块，返回 (提掉的点, 新棋盘)。"""
    size = len(board)
    opponent = WHITE if color == BLACK else BLACK
    next_board = [line[:] for line in board]
    next_board[row][column] = color
    captured: list[tuple[int, int]] = []
    for next_row, next_column in _neighbors(row, column, size):
        if next_board[next_row][next_column] != opponent:
            continue
        group = _group_at(next_board, next_row, next_column)
        if group.liberties:
            continue
        for stone_row, stone_column in group.stones:
            next_board[stone_row][stone_column] = EMPTY
        captured.extend(group.stones)
    return captured, next_board


def _position_key(board: list[list[int]], turn: int) -> str:
    flat = "".join(str(cell) for line in board for cell in line)
    return f"{len(board)}:{turn}:{flat}"


@dataclass(slots=True)
class WeiqiGame:
    """13 路（可配 9/19 路）围棋：本地规则引擎 + 启发式 Bot + 实时胜率估算。

    规则采用中国规则：数子法（子 + 空），黑贴 `komi` 目；禁止自杀、禁止全局同形
    （简单劫与超级劫一并按「局面重复」判非法）；双方连续停一手即终局。
    终局不做死活标记，死子需要在停手前吃净——这条写进了界面规则说明。
    """

    human_color: int = BLACK
    difficulty: Difficulty = "normal"
    size: int = DEFAULT_SIZE
    komi: float = DEFAULT_KOMI
    board: list[list[int]] = field(default_factory=list)
    turn: int = BLACK
    winner: int = EMPTY
    draw: bool = False
    history: list[tuple[int, int, int]] = field(default_factory=list)
    captures: dict[int, int] = field(default_factory=dict)
    ko_point: tuple[int, int] | None = None
    consecutive_passes: int = 0
    last_move: tuple[int, int] | None = None
    last_captured: int = 0
    # 最近一手提掉的坐标：前端据此在被提位置画渐隐残影 + 「提 N 子」气泡
    last_captured_points: list[tuple[int, int]] = field(default_factory=list)
    # 最近一手提掉的棋子颜色：前端用它决定残影用黑/白，避免「白提黑」时画成白影，
    # 让人误以为白方在提自己的子（用户反馈：4 白包 1 黑被提走时看起来像白被提）。
    last_captured_color: int = EMPTY
    result: dict[str, object] = field(default_factory=dict)
    _positions: set[str] = field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        if self.size not in SIZES:
            self.size = DEFAULT_SIZE
        if not self.board:
            self.board = [[EMPTY for _ in range(self.size)] for _ in range(self.size)]
        if not self.captures:
            self.captures = {BLACK: 0, WHITE: 0}
        self._positions.add(_position_key(self.board, self.turn))

    @property
    def bot_color(self) -> int:
        return WHITE if self.human_color == BLACK else BLACK

    @property
    def finished(self) -> bool:
        return self.winner != EMPTY or self.draw

    @property
    def area(self) -> int:
        return self.size * self.size

    # ---------------------------------------------------------------- 走子

    def is_legal(self, row: int, column: int, color: int | None = None) -> bool:
        if self.finished:
            return False
        if not (0 <= row < self.size and 0 <= column < self.size):
            return False
        if self.board[row][column] != EMPTY:
            return False
        if self.ko_point == (row, column):
            return False
        side = self.turn if color is None else color
        # 快速通道：只要还有一个空邻点，落子后自身就有气，且不会产生提子，
        # 也就不可能触发自杀或全局同形，直接放行（绝大多数点走这条）。
        neighbors = list(_neighbors(row, column, self.size))
        if any(self.board[next_row][next_column] == EMPTY for next_row, next_column in neighbors):
            return True
        _captured, next_board = _play_stone(self.board, row, column, side)
        if not _group_at(next_board, row, column).liberties:
            return False
        return _position_key(next_board, WHITE if side == BLACK else BLACK) not in self._positions

    def legal_moves(self, color: int | None = None) -> list[list[int]]:
        side = self.turn if color is None else color
        return [
            [row, column]
            for row in range(self.size)
            for column in range(self.size)
            if self.is_legal(row, column, side)
        ]

    def place(self, row: int, column: int, color: int) -> int:
        """落一子，返回本手提子数。"""
        if self.finished:
            raise ValueError("本局已经结束")
        if color != self.turn:
            raise ValueError("还没轮到你落子")
        if not self.is_legal(row, column, color):
            raise ValueError("这里不能落子")
        captured, next_board = _play_stone(self.board, row, column, color)
        self.board = next_board
        self.last_captured = len(captured)
        self.last_captured_points = list(captured)
        # 提掉的是「对手」颜色的棋子：记录颜色供前端画同色残影
        self.last_captured_color = (WHITE if color == BLACK else BLACK) if captured else EMPTY
        self.captures[color] = self.captures.get(color, 0) + len(captured)
        self.history.append((row, column, color))
        self.last_move = (row, column)
        self.consecutive_passes = 0
        self.turn = WHITE if color == BLACK else BLACK
        self._positions.add(_position_key(self.board, self.turn))
        self.ko_point = self._ko_point(row, column, color, captured)
        return len(captured)

    def pass_turn(self, color: int | None = None) -> bool:
        """停一手；双方连续停手则结算。返回本局是否因此结束。"""
        if self.finished:
            raise ValueError("本局已经结束")
        side = self.turn if color is None else color
        if side != self.turn:
            raise ValueError("还没轮到你停手")
        self.history.append((-1, -1, side))
        self.turn = WHITE if side == BLACK else BLACK
        self.consecutive_passes += 1
        self.ko_point = None
        self.last_move = None
        self.last_captured = 0
        self.last_captured_points = []
        self.last_captured_color = EMPTY
        if self.consecutive_passes >= 2:
            self._settle()
            return True
        return False

    def _ko_point(
        self,
        row: int,
        column: int,
        color: int,
        captured: list[tuple[int, int]],
    ) -> tuple[int, int] | None:
        """单子提单子且自身只剩一气时形成劫，禁着点即被提的那一点。"""
        if len(captured) != 1:
            return None
        group = _group_at(self.board, row, column)
        if len(group.stones) != 1 or len(group.liberties) != 1:
            return None
        return captured[0]

    # ---------------------------------------------------------------- 结算

    def territories(self) -> tuple[int, int, int]:
        """返回 (黑空, 白空, 单官/未定)。按「空区只被一方包围」判归属。"""
        visited = [[False for _ in range(self.size)] for _ in range(self.size)]
        black_territory = 0
        white_territory = 0
        neutral = 0
        for row in range(self.size):
            for column in range(self.size):
                if visited[row][column] or self.board[row][column] != EMPTY:
                    continue
                region: list[tuple[int, int]] = []
                border: set[int] = set()
                stack = [(row, column)]
                visited[row][column] = True
                while stack:
                    current_row, current_column = stack.pop()
                    region.append((current_row, current_column))
                    for next_row, next_column in _neighbors(
                        current_row, current_column, self.size
                    ):
                        value = self.board[next_row][next_column]
                        if value == EMPTY:
                            if not visited[next_row][next_column]:
                                visited[next_row][next_column] = True
                                stack.append((next_row, next_column))
                        else:
                            border.add(value)
                if border == {BLACK}:
                    black_territory += len(region)
                elif border == {WHITE}:
                    white_territory += len(region)
                else:
                    neutral += len(region)
        return black_territory, white_territory, neutral

    def area_score(self) -> dict[str, float]:
        black_stones = sum(
            cell == BLACK for line in self.board for cell in line
        )
        white_stones = sum(
            cell == WHITE for line in self.board for cell in line
        )
        black_territory, white_territory, neutral = self.territories()
        black = black_stones + black_territory
        white = white_stones + white_territory + self.komi
        return {
            "black": float(black),
            "white": float(white),
            "black_stones": float(black_stones),
            "white_stones": float(white_stones),
            "black_territory": float(black_territory),
            "white_territory": float(white_territory),
            "neutral": float(neutral),
            "margin": float(black - white),
        }

    def _settle(self) -> None:
        score = self.area_score()
        margin = float(score["margin"])
        self.result = {
            "margin": margin,
            "black": score["black"],
            "white": score["white"],
            "komi": self.komi,
        }
        if margin > 0:
            self.winner = BLACK
        elif margin < 0:
            self.winner = WHITE
        else:
            self.draw = True

    # ---------------------------------------------------------------- 胜率

    def evaluate(self) -> dict[str, object]:
        """实时胜率估算：空区归属先按「被谁包围」判定，未定区域按影响力折算。

        这是一个轻量评估，不是引擎搜索：目的是让观战者看到「谁现在领先、领先多少」，
        数值会随提子与围空发生明显摆动，但不宣称等同职业棋力判断。
        """
        black_stones = 0
        white_stones = 0
        far = self.size + 4
        black_distance: list[list[int]] = [[far for _ in range(self.size)] for _ in range(self.size)]
        white_distance: list[list[int]] = [[far for _ in range(self.size)] for _ in range(self.size)]
        for row in range(self.size):
            for column in range(self.size):
                value = self.board[row][column]
                if value == BLACK:
                    black_stones += 1
                    black_distance[row][column] = 0
                elif value == WHITE:
                    white_stones += 1
                    white_distance[row][column] = 0

        # 多源 BFS：每个空点到最近黑子 / 白子的棋盘距离，用来近似影响力扩散。
        for distances in (black_distance, white_distance):
            queue = deque(
                (row, column)
                for row in range(self.size)
                for column in range(self.size)
                if distances[row][column] == 0
            )
            while queue:
                row, column = queue.popleft()
                for next_row, next_column in _neighbors(row, column, self.size):
                    if distances[next_row][next_column] > distances[row][column] + 1:
                        distances[next_row][next_column] = distances[row][column] + 1
                        queue.append((next_row, next_column))

        black_territory, white_territory, neutral = self.territories()
        black_influence = 0.0
        white_influence = 0.0
        unsettled = 0
        for row in range(self.size):
            for column in range(self.size):
                if self.board[row][column] != EMPTY:
                    continue
                black_gap = black_distance[row][column]
                white_gap = white_distance[row][column]
                weight = 1.0 / (1.0 + math.exp((black_gap - white_gap) / 1.6))
                black_influence += weight
                white_influence += 1.0 - weight
                if abs(black_gap - white_gap) <= 2:
                    unsettled += 1

        black_score = black_stones + black_territory + black_influence
        white_score = white_stones + white_territory + self.komi + white_influence
        # 轮到谁走本身就有价值：贴目被「先手」部分对冲，否则开局会永远显示黑方大劣。
        tempo = self.komi * (0.75 if self.turn == BLACK else 0.35)
        margin = black_score - white_score + tempo
        empty_total = max(1, self.area - black_stones - white_stones)
        unsettled_fraction = min(1.0, unsettled / empty_total)
        scale = 0.55 * math.sqrt(self.area) * (1.0 + 1.4 * unsettled_fraction)
        winrate = 1.0 / (1.0 + math.exp(-margin / max(1.0, scale)))
        winrate = min(0.98, max(0.02, winrate))
        return {
            "black": round(winrate * 100, 1),
            "white": round((1.0 - winrate) * 100, 1),
            "lead": round(margin, 1),
            "unsettled": round(unsettled_fraction, 2),
            "scored": self.finished,
        }

    # ---------------------------------------------------------------- Bot

    def choose_bot_move(self, *, seed: int | None = None, time_limit: float = 0.6):
        """返回 Bot 的落点；None 表示停一手。"""
        if self.finished or self.turn != self.bot_color:
            return None
        rng = random.Random(seed)
        candidates = self._candidates()
        if not candidates:
            return None
        ranked = self._rank(candidates, self.bot_color)
        if not ranked:
            return None
        if self._should_pass(ranked):
            return None
        if self.difficulty == "easy":
            pool = ranked[: min(6, len(ranked))]
            weights = [max(0.4, score + 1.0) for _point, score in pool]
            return rng.choices([point for point, _ in pool], weights=weights, k=1)[0]
        if self.difficulty == "normal":
            best = ranked[0][1]
            pool = [point for point, score in ranked if score >= best - 0.35]
            return rng.choice(pool[:3])

        deadline = time.monotonic() + max(0.2, min(time_limit, 1.6))
        best_point = ranked[0][0]
        best_value = -math.inf
        base_margin = float(self.area_score()["margin"])
        for point, score in ranked[: min(10, len(ranked))]:
            if time.monotonic() >= deadline:
                break
            _captured, next_board = _play_stone(
                self.board, point[0], point[1], self.bot_color
            )
            probe = WeiqiGame(
                human_color=self.human_color,
                difficulty=self.difficulty,
                size=self.size,
                komi=self.komi,
                board=next_board,
                turn=WHITE if self.bot_color == BLACK else BLACK,
            )
            margin_gain = float(probe.area_score()["margin"]) - base_margin
            value = score + 0.45 * margin_gain
            if value > best_value:
                best_value = value
                best_point = point
        return best_point

    def _candidates(self) -> list[tuple[int, int]]:
        """候选点：已有棋子附近两格内的空点；开局无子时下天元。"""
        has_stone = any(cell != EMPTY for line in self.board for cell in line)
        if not has_stone:
            middle = self.size // 2
            return [(middle, middle)]
        points: list[tuple[int, int]] = []
        for row in range(self.size):
            for column in range(self.size):
                if self.board[row][column] != EMPTY:
                    continue
                if not self.is_legal(row, column):
                    continue
                near = False
                for next_row in range(max(0, row - 2), min(self.size, row + 3)):
                    for next_column in range(
                        max(0, column - 2), min(self.size, column + 3)
                    ):
                        if self.board[next_row][next_column] != EMPTY:
                            near = True
                            break
                    if near:
                        break
                if near:
                    points.append((row, column))
        return points

    def _rank(
        self,
        points: list[tuple[int, int]] | None = None,
        color: int | None = None,
    ) -> list[tuple[tuple[int, int], float]]:
        side = self.turn if color is None else color
        pool = points if points is not None else self._candidates()
        last = self.last_move
        black_territory, white_territory, _neutral = self.territories()
        own_territory = black_territory if side == BLACK else white_territory
        territory_heavy = own_territory >= self.area * 0.18
        ranked: list[tuple[tuple[int, int], float]] = []
        for row, column in pool:
            if not self.is_legal(row, column, side):
                continue
            captured, next_board = _play_stone(self.board, row, column, side)
            own = _group_at(next_board, row, column)
            score = 2.4 * len(captured)
            if len(own.liberties) == 1 and not captured:
                score -= 4.0
            elif len(own.liberties) >= 2:
                score += 0.4 * min(len(own.liberties), 4)
            saved = 0
            for next_row, next_column in _neighbors(row, column, self.size):
                if self.board[next_row][next_column] != side:
                    continue
                group = _group_at(self.board, next_row, next_column)
                if len(group.liberties) == 1:
                    saved += len(group.stones)
            score += 1.6 * saved
            adjacent_opponent = sum(
                1
                for next_row, next_column in _neighbors(row, column, self.size)
                if self.board[next_row][next_column] not in (EMPTY, side)
            )
            score += 0.5 * adjacent_opponent
            if last is not None:
                distance = max(abs(row - last[0]), abs(column - last[1]))
                score += max(0.0, 1.2 - 0.3 * distance)
            center = (self.size - 1) / 2
            distance_center = max(abs(row - center), abs(column - center))
            if len(self.history) < self.size:
                score += 0.35 * (1.0 - distance_center / max(1.0, center))
            if self._fills_own_eye(row, column, side):
                score -= 7.0
            elif territory_heavy and adjacent_opponent == 0:
                score -= 1.5
            ranked.append(((row, column), score))
        ranked.sort(key=lambda item: item[1], reverse=True)
        return ranked

    def _fills_own_eye(self, row: int, column: int, color: int) -> bool:
        """四邻全为同色且斜角至少三处同色，视为自己的眼。"""
        for next_row, next_column in _neighbors(row, column, self.size):
            if self.board[next_row][next_column] != color:
                return False
        own = 0
        for delta_row in (-1, 1):
            for delta_column in (-1, 1):
                next_row, next_column = row + delta_row, column + delta_column
                if 0 <= next_row < self.size and 0 <= next_column < self.size:
                    if self.board[next_row][next_column] == color:
                        own += 1
        return own >= 3

    def _should_pass(self, ranked: list[tuple[tuple[int, int], float]]) -> bool:
        if len(self.history) < self.size + 4:
            return False
        if not ranked:
            return True
        best = ranked[0][1]
        if best > 1.2:
            return False
        for row in range(self.size):
            for column in range(self.size):
                if self.board[row][column] != self.bot_color:
                    continue
                group = _group_at(self.board, row, column)
                if len(group.liberties) <= 1:
                    return False
        return True

    # ---------------------------------------------------------------- 快照

    def tactical_state(self, _side: object = None) -> str:
        if self.finished:
            return "win"
        if self.consecutive_passes:
            return "pass"
        if self.last_captured >= 3:
            return "big_capture"
        if self.last_captured:
            return "capture"
        return ""

    def snapshot(self) -> dict[str, object]:
        return {
            "kind": "weiqi",
            "size": self.size,
            "board": [line[:] for line in self.board],
            "turn": self.turn,
            "human_color": self.human_color,
            "bot_color": self.bot_color,
            "winner": self.winner,
            "draw": self.draw,
            "move_count": len(self.history),
            "last_move": list(self.last_move) if self.last_move else None,
            "last_captured": self.last_captured,
            "last_captured_points": [list(point) for point in self.last_captured_points],
            "last_captured_color": self.last_captured_color,
            "captures": {
                "black": int(self.captures.get(BLACK, 0)),
                "white": int(self.captures.get(WHITE, 0)),
            },
            "passes": self.consecutive_passes,
            "komi": self.komi,
            "legal_moves": [] if self.finished else self.legal_moves(self.turn),
            "eval": self.evaluate(),
            "result": dict(self.result),
        }

    # ---------------------------------------------------------------- 悔棋

    def undo_round(self) -> int:
        """撤销最近一组「Bot 一手 + 真人一手」；无法整组回退时退到真人回合。"""
        if not self.history:
            raise ValueError("还没有可以悔掉的落子")
        human_color = self.human_color
        cut = len(self.history)
        for index in range(len(self.history) - 1, -1, -1):
            if self.history[index][2] == human_color:
                cut = index
                break
        if cut >= len(self.history):
            raise ValueError("还没有可以悔掉的落子")
        removed = self.history[cut:]
        kept = self.history[:cut]
        self._replay(kept)
        return len(removed)

    def _replay(self, history: list[tuple[int, int, int]]) -> None:
        self.board = [[EMPTY for _ in range(self.size)] for _ in range(self.size)]
        self.turn = BLACK
        self.winner = EMPTY
        self.draw = False
        self.captures = {BLACK: 0, WHITE: 0}
        self.ko_point = None
        self.consecutive_passes = 0
        self.last_move = None
        self.last_captured = 0
        self.last_captured_points = []
        self.last_captured_color = EMPTY
        self.result = {}
        self._positions = {_position_key(self.board, self.turn)}
        self.history = []
        for row, column, color in history:
            if row < 0:
                self.pass_turn(color)
                continue
            self.place(row, column, color)
