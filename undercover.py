from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any


UCCamp = str  # "civilian" | "undercover" | "whiteboard" | "none"
UCPhase = str  # "idle" | "preparing" | "speech" | "voting" | "pk" | "finished"


def _text_similarity(a: str, b: str) -> float:
    """返回 [0,1] 的文本相似度，用于“发言相似度”检测。"""
    a = "".join((a or "").split())
    b = "".join((b or "").split())
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


DEFAULT_SEED_WORDS: list[tuple[str, str]] = [
    ("牛奶", "豆浆"),
    ("苹果", "雪梨"),
    ("筷子", "叉子"),
    ("老虎", "狮子"),
    ("猫", "狗"),
    ("米饭", "馒头"),
    ("可乐", "雪碧"),
    ("篮球", "足球"),
    ("电影", "电视剧"),
    ("空调", "电风扇"),
    ("微信", "QQ"),
    ("火车", "地铁"),
    ("钢琴", "电子琴"),
    ("红包", "压岁钱"),
    ("绿茶", "红茶"),
    ("牛肉面", "螺蛳粉"),
    ("过山车", "海盗船"),
    ("图书馆", "书店"),
    ("支付宝", "微信支付"),
    ("西瓜", "哈密瓜"),
    ("奥特曼", "铠甲勇士"),
    ("月饼", "粽子"),
    ("钢琴", "小提琴"),
    ("沙发", "床"),
    ("手机", "平板"),
    ("雨伞", "雨衣"),
    ("眼镜", "墨镜"),
    ("咖啡", "奶茶"),
    ("薯片", "薯条"),
    ("春天", "夏天"),
    ("鸭子", "鹅"),
]


@dataclass(slots=True)
class UCPlayer:
    number: int
    qq: str = ""
    display_name: str = ""
    camp: UCCamp = "none"
    word: str = ""
    is_out: bool = False
    player_id: int = 0

    @property
    def label(self) -> str:
        name = self.display_name.strip() or f"{self.number}号"
        return f"{name}（{self.number}号）"


@dataclass(slots=True)
class UCSpeech:
    round_number: int
    player_number: int
    content: str
    at: float


@dataclass(slots=True)
class UCVote:
    round_number: int
    voter_number: int
    target_number: int
    at: float


@dataclass(slots=True)
class UCRound:
    round_number: int
    speech_player_numbers: list[int]
    vote_player_numbers: list[int]
    speeches: list[UCSpeech] = field(default_factory=list)
    votes: list[UCVote] = field(default_factory=list)
    out_player_number: int | None = None
    pk_reason: str = ""

    def has_spoken(self, player_number: int) -> bool:
        return any(s.player_number == player_number for s in self.speeches)

    def has_voted(self, voter_number: int) -> bool:
        return any(v.voter_number == voter_number for v in self.votes)

    def votes_by_target(self) -> dict[int, int]:
        tally: dict[int, int] = {}
        for vote in self.votes:
            tally[vote.target_number] = tally.get(vote.target_number, 0) + 1
        return tally

    def max_vote_targets(self) -> list[int]:
        """Return list of player_numbers with the highest equal vote counts."""
        tally = self.votes_by_target()
        if not tally:
            return []
        top = max(tally.values())
        return [p for p, n in tally.items() if n == top]


class UndercoverGame:
    """多人社交推理：谁是卧底。纯内存实现，不抛异常。"""

    def __init__(
        self,
        *,
        camp_scales: tuple[int, int, int] = (4, 1, 0),
        first_round_non_voting: int = 3,
        similarity: int = 0,
        reveal_identity: bool = True,
    ) -> None:
        self.camp_scales = camp_scales
        self.first_round_non_voting = max(2, int(first_round_non_voting))
        # 发言相似度（0~100）：超过阈值判定为“与历史发言雷同”，驳回并提示换说法
        self.similarity = max(0, min(int(similarity), 100))
        # 是否在开场发放身份（身份/词条卡），受管理台“告知身份”开关控制
        self.reveal_identity = bool(reveal_identity)
        # 最近一次被驳回发言的原因，供 room_manager / main 返回给前端提示
        self.last_speech_reject_reason: str | None = None
        self.last_speech_masked: bool = False
        self.players: list[UCPlayer] = []
        self.words: tuple[str, str] = ("", "")
        self.civilian_word: str = ""
        self.undercover_word: str = ""
        self.rounds: list[UCRound] = []
        self.phase: UCPhase = "idle"
        self.finished: bool = False
        self.winner: dict[str, Any] | None = None
        self.winner_camp: UCCamp | None = None
        self.winner_message: str = ""
        self.speaking_order: list[int] = []
        self.current_speaker_index: int = 0
        self.pending_round: UCRound | None = None
        # 每次游戏过程中为玩家分配稳定的 player_id（投票列表展示 1..N 用）
        self._next_player_id: int = 1

    # ---------------------- Setup ----------------------

    def attach_players(self, seat_infos: list[tuple[int, str, str]]) -> None:
        """seat_infos: [(seat_number, qq, display_name), ...]。调用前先清旧。"""
        self.players = []
        for number, qq, name in seat_infos:
            self.players.append(
                UCPlayer(
                    number=int(number),
                    qq=str(qq or ""),
                    display_name=str(name or ""),
                    player_id=self._next_player_id,
                )
            )
            self._next_player_id += 1

    def assign_words(self, word_pair: tuple[str, str]) -> None:
        if len(self.players) < 3:
            raise ValueError("玩家不足 3 人，无法开始谁是卧底")
        civ_count, uc_count, wb_count = self._calculate_real_counts(len(self.players))
        # 随机交换 word1/word2 作为卧底词
        if random.random() < 0.5:
            civ_word, uc_word = word_pair[0], word_pair[1]
        else:
            civ_word, uc_word = word_pair[1], word_pair[0]
        self.words = (civ_word, uc_word)
        self.civilian_word = civ_word
        self.undercover_word = uc_word
        pools = self.players[:]
        random.shuffle(pools)
        # 前两名玩家不能是白板（规则来自 Theresa3rd）
        non_top_two = [p for p in pools if p.number not in (pools[0].number, pools[1].number)] if len(pools) >= 3 else pools[:]
        wb_players: list[UCPlayer] = []
        for _index in range(wb_count):
            if not non_top_two:
                break
            pick = non_top_two.pop(random.randrange(len(non_top_two)))
            if pick in pools:
                pools.remove(pick)
            pick.camp = "whiteboard"
            pick.word = ""
            wb_players.append(pick)
        civ_players: list[UCPlayer] = []
        for _index in range(civ_count):
            if not pools:
                break
            pick = pools.pop(random.randrange(len(pools)))
            pick.camp = "civilian"
            pick.word = civ_word
            civ_players.append(pick)
        uc_players: list[UCPlayer] = []
        for _index in range(uc_count):
            if not pools:
                break
            pick = pools.pop(random.randrange(len(pools)))
            pick.camp = "undercover"
            pick.word = uc_word
            uc_players.append(pick)
        # 若有剩余（极端情况整数除法没分完），剩下默认塞回平民
        for pick in pools:
            pick.camp = "civilian"
            pick.word = civ_word
            civ_players.append(pick)
        self.phase = "preparing"
        self._start_new_round()

    def _calculate_real_counts(self, total: int) -> tuple[int, int, int]:
        """按 camp_scales 的比例把 total 人分成（平民,卧底,白板）。平民至少 1 人，卧底至少 1 人。"""
        civ_s, uc_s, wb_s = self.camp_scales
        base = civ_s + uc_s + wb_s
        if base <= 0:
            civ_s, uc_s, wb_s = 4, 1, 0
            base = 5
        if total <= 0:
            return 0, 0, 0
        groups = total // base
        extra = total - groups * base
        civ = groups * civ_s + extra  # 不足一组时，多余名额先给平民
        uc = groups * uc_s
        wb = groups * wb_s
        # 保证至少 1 个卧底
        if uc < 1:
            uc = 1
        # 剩余人数 = total - uc，再分给平民+白板
        remaining = total - uc
        if remaining < 1:
            # 不可能（total >= 2，uc >=1，所以 remaining>=1），兜底
            civ = total - uc
            wb = 0
        else:
            # 平民至少 1：如果 civ 算出来 < 1，把白板的人挪给平民
            if civ < 1:
                need_from_wb = 1 - civ
                take = min(need_from_wb, wb)
                wb -= take
                civ += take
            # 如果还是不够 1 平民（没白板可挪 or 挪完还不够），直接强制 civ=1，wb=剩余
            if civ < 1:
                civ = 1
                wb = remaining - civ
            # 保证 civ + wb == remaining，且 wb >= 0
            wb = max(0, min(wb, remaining - civ))
            civ = remaining - wb
        return civ, uc, wb

    # ---------------------- Rounds ----------------------

    def _live_player_numbers(self) -> list[int]:
        return [p.number for p in self.players if not p.is_out]

    def _start_new_round(self, pk_from_targets: list[int] | None = None) -> None:
        live = self._live_player_numbers()
        round_number = len(self.rounds) + 1
        if pk_from_targets is None:
            if self.rounds and self.speaking_order:
                # 顺序发言：以上一轮发言顺序为基础，剔除出局者并把上轮首位发言人移到本轮末尾。
                # 例如第 1 轮顺序 1,2,3 → 第 2 轮 2,3,1 → 第 3 轮 3,1,2，轮流打头（第 2 轮 2 号先讲）。
                # 用 dict.fromkeys 去重（本轮顺序里每个号码只保留一次）
                ordered = list(dict.fromkeys(n for n in self.speaking_order if n in live))
                if ordered:
                    ordered = ordered[1:] + [ordered[0]]
                speech = ordered + [n for n in live if n not in ordered]
            else:
                # 首轮：按座位号从小到大顺序发言（1 号先讲）
                speech = sorted(live)
            vote = live[:]
            rnd = UCRound(round_number, speech, vote)
            self.rounds.append(rnd)
            self.pending_round = rnd
            # 首轮人数 <= first_round_non_voting → 发言结束后直接跳过投票进入下一轮
            if round_number == 1 and len(live) <= self.first_round_non_voting:
                self.phase = "speech"
                self.speaking_order = speech[:]
                self.current_speaker_index = 0
                return
            self.phase = "speech"
            self.speaking_order = speech[:]
            self.current_speaker_index = 0
        else:
            # PK 子轮：只允许平票玩家发言 + 所有存活玩家（含平票玩家）投
            speech = pk_from_targets[:]
            random.shuffle(speech)
            vote = live[:]
            rnd = UCRound(round_number, speech, vote, pk_reason=f"平票PK:{pk_from_targets}")
            self.rounds.append(rnd)
            self.pending_round = rnd
            self.phase = "pk"
            self.speaking_order = speech[:]
            self.current_speaker_index = 0

    # ---------------------- Speech ----------------------

    @property
    def current_round(self) -> UCRound | None:
        return self.rounds[-1] if self.rounds else None

    def rounds_speeches(self) -> list[dict[str, Any]]:
        """展开所有轮次（含 PK 子轮）的发言记录，供相似度检测用。"""
        out: list[dict[str, Any]] = []
        for rnd in self.rounds:
            for s in rnd.speeches:
                out.append(
                    {
                        "round_number": rnd.round_number,
                        "player_number": s.player_number,
                        "content": s.content,
                    }
                )
        return out

    @property
    def current_round_number(self) -> int:
        """当前进行到的轮次号（尚未开局时返回 0）。供 room_manager / main.py / 前端展示用。"""
        return len(self.rounds)

    @property
    def expected_speaker_number(self) -> int | None:
        if self.phase not in ("speech", "pk"):
            return None
        if not self.speaking_order:
            return None
        if self.current_speaker_index >= len(self.speaking_order):
            return None
        return self.speaking_order[self.current_speaker_index]

    def submit_speech(self, player_number: int, content: str) -> bool:
        if self.phase not in ("speech", "pk"):
            return False
        self.last_speech_reject_reason = None
        self.last_speech_masked = False
        if self.pending_round is None:
            return False
        content = str(content or "").strip()
        if len(content) == 0 or len(content) > 500:
            return False
        expected = self.expected_speaker_number
        if expected is None or int(player_number) != int(expected):
            return False
        if self.pending_round.has_spoken(player_number):
            return False
        if any(p.number == player_number and p.is_out for p in self.players):
            return False
        player = next(
            (p for p in self.players if p.number == int(player_number)), None
        )
        # 词条打码：非白板玩家发言中出现任一当前词条（平民词/卧底词）时，
        # 自动把词条屏蔽为「***」再发言，防止 AI/真人说漏嘴把词条暴露到公屏；白板不受限
        if player is not None and player.camp != "whiteboard":
            for word in (self.civilian_word, self.undercover_word):
                if word and word in content:
                    content = content.replace(word, "***")
                    self.last_speech_masked = True
        # 相似度拦截：发言时即判定。
        # 1) 与同轮其他玩家已发言内容高度相似 → 直接驳回本次发言（不回弹对方发言、不要求任何人重讲），
        #    恶意重复相同发言只会一直被拒，无法挤掉其他玩家已有的发言。
        # 2) 与任意玩家任意轮次（含自己之前发言）雷同 → 驳回。
        if self.similarity > 0:
            threshold = self.similarity / 100.0
            # 1) 本轮冲突：与同轮其他玩家已发言内容高度相似 → 仅拒绝本次发言
            for s in self.pending_round.speeches:
                if s.player_number == int(player_number) or not s.content:
                    continue
                if _text_similarity(content, s.content) > threshold:
                    self.last_speech_reject_reason = (
                        f"你的发言与{s.player_number}号的发言相似度超过了{self.similarity}%，"
                        "请换一种描述后再提交。"
                    )
                    return False
            # 2) 全局历史：与所有轮次（含自己之前发言）雷同
            for s in self.rounds_speeches():
                if _text_similarity(content, s.get("content") or "") > threshold:
                    self.last_speech_reject_reason = (
                        f"你的发言与历史发言相似度超过了{self.similarity}%，请换个说法。"
                    )
                    return False
        self.pending_round.speeches.append(
            UCSpeech(
                round_number=self.pending_round.round_number,
                player_number=int(player_number),
                content=content,
                at=time.time(),
            )
        )
        # 白板说词条：直接获胜（规则：白板发言内容中包含任一词条即白板获胜）
        if player is not None and player.camp == "whiteboard" and not self.finished:
            for w in (self.civilian_word, self.undercover_word):
                if w and w in content:
                    self.finished = True
                    self.phase = "finished"
                    self.winner_camp = "whiteboard"
                    self.winner_message = "白板直接说出了词条，白板获胜！"
                    self.winner = {
                        "camp": "whiteboard",
                        "message": self.winner_message,
                        "civilian_word": self.civilian_word,
                        "undercover_word": self.undercover_word,
                    }
                    break
        if self.finished:
            return True
        # 推进到下一个发言者
        self.current_speaker_index += 1
        # 所有待发言玩家发言完毕
        while (
            self.current_speaker_index < len(self.speaking_order)
            and any(
                p.number == self.speaking_order[self.current_speaker_index] and p.is_out
                for p in self.players
            )
        ):
            self.current_speaker_index += 1
        if self.current_speaker_index >= len(self.speaking_order):
            # PK 子轮或普通轮：发言结束 → 投票（但首轮人数阈值内跳过投票）
            rnd = self.pending_round
            assert rnd is not None
            if (
                rnd.round_number == 1
                and self.phase == "speech"
                and len(self._live_player_numbers()) <= self.first_round_non_voting
            ):
                # 不投票，继续下一轮 speech
                self._start_new_round()
                self._check_winner()
            else:
                self.phase = "voting"
        return True

    # ---------------------- Voting ----------------------

    def vote_tally_view(self) -> dict[int, int]:
        if self.pending_round is None or self.phase not in ("voting",):
            return {}
        return self.pending_round.votes_by_target()

    def all_voted(self) -> bool:
        if self.pending_round is None or self.phase != "voting":
            return True
        need = [n for n in self.pending_round.vote_player_numbers if not any(p.number == n and p.is_out for p in self.players)]
        return all(self.pending_round.has_voted(n) for n in need)

    def submit_vote(self, voter_number: int, target_number: int) -> dict[str, Any]:
        """返回：{ok, need_pk, pk_targets, out_player, winner_camp?, winner_message?}"""
        if self.phase != "voting" or self.pending_round is None:
            return {"ok": False, "reason": "not_voting_phase"}
        voter = next((p for p in self.players if p.number == voter_number), None)
        target = next((p for p in self.players if p.number == target_number), None)
        if voter is None or target is None:
            return {"ok": False, "reason": "player_not_found"}
        if voter.is_out:
            return {"ok": False, "reason": "voter_is_out"}
        if target.is_out:
            return {"ok": False, "reason": "target_is_out"}
        if voter.number == target.number:
            return {"ok": False, "reason": "cannot_vote_self"}
        if voter.number not in self.pending_round.vote_player_numbers:
            return {"ok": False, "reason": "not_vote_eligible"}
        if self.pending_round.has_voted(voter.number):
            return {"ok": False, "reason": "already_voted"}
        self.pending_round.votes.append(
            UCVote(
                round_number=self.pending_round.round_number,
                voter_number=voter.number,
                target_number=target.number,
                at=time.time(),
            )
        )
        if not self.all_voted():
            return {"ok": True, "need_pk": False, "pk_targets": None}
        top_targets = self.pending_round.max_vote_targets()
        if len(top_targets) > 1:
            # 进入 PK 子轮
            return {
                "ok": True,
                "need_pk": True,
                "pk_targets": top_targets,
                "out_player": None,
            }
        # 淘汰唯一最高票
        out_number = top_targets[0]
        out_player = next(p for p in self.players if p.number == out_number)
        out_player.is_out = True
        self.pending_round.out_player_number = out_number
        result: dict[str, Any] = {
            "ok": True,
            "need_pk": False,
            "pk_targets": None,
            "out_player": {
                "number": out_player.number,
                "display_name": out_player.display_name or f"{out_player.number}号",
            },
        }
        # 检查胜负
        win_info = self._check_winner()
        if win_info["finished"]:
            self.phase = "finished"
            self.finished = True
            self.winner_camp = win_info["winner_camp"]
            self.winner_message = win_info["message"]
            civilian_word = self.civilian_word
            undercover_word = self.undercover_word
            self.winner = {
                "camp": self.winner_camp,
                "message": self.winner_message,
                "civilian_word": civilian_word,
                "undercover_word": undercover_word,
            }
            result["winner_camp"] = self.winner_camp
            result["winner_message"] = self.winner_message
        else:
            # 开启下一轮
            self._start_new_round()
        return result

    def continue_pk(self, pk_targets: list[int]) -> None:
        """submit_vote 返回 need_pk 后调用，开启 PK 子轮（speech->vote 的循环）。"""
        self._start_new_round(pk_from_targets=list(pk_targets))

    # ---------------------- Winner ----------------------

    def _check_winner(self) -> dict[str, Any]:
        live = [p for p in self.players if not p.is_out]
        non_civs = [p for p in self.players if p.camp != "civilian"]
        if all(p.is_out for p in non_civs):
            # 还有白板存活？先判白板胜（Theresa3rd 规则：所有卧底出局+白板存活即白板胜）
            live_wb = [p for p in live if p.camp == "whiteboard"]
            # 但是注意：非平民只有卧底+白板，如果全出局且白板活着也意味着非平民没全出局，不会走这一分支。
            # 所以这里就是平民胜
            return {"finished": True, "winner_camp": "civilian", "message": "所有卧底和白板都已出局，平民获胜！"}
        non_undercovers = [p for p in self.players if p.camp != "undercover"]
        if all(p.is_out for p in non_undercovers):
            return {"finished": True, "winner_camp": "undercover", "message": "所有平民和白板都已出局，卧底获胜！"}
        live_undercovers = [p for p in live if p.camp == "undercover"]
        if len(live) <= 2 and len(live_undercovers) >= 1:
            return {"finished": True, "winner_camp": "undercover", "message": "仅剩 2 人存活且卧底仍在，卧底获胜！"}
        live_wb = [p for p in live if p.camp == "whiteboard"]
        undercovers = [p for p in self.players if p.camp == "undercover"]
        if all(p.is_out for p in undercovers) and len(live_wb) >= 1:
            return {"finished": True, "winner_camp": "whiteboard", "message": "所有卧底都已出局且白板仍存活，白板获胜！"}
        return {"finished": False, "winner_camp": None, "message": ""}

    # ---------------------- Snapshots ----------------------

    def _camp_count_all(self) -> dict[str, int]:
        counts = {"civilian": 0, "undercover": 0, "whiteboard": 0}
        for p in self.players:
            if p.camp in counts:
                counts[p.camp] += 1
        return counts

    def _camp_count_live(self) -> dict[str, int]:
        counts = {"civilian": 0, "undercover": 0, "whiteboard": 0}
        for p in self.players:
            if p.is_out or p.camp not in counts:
                continue
            counts[p.camp] += 1
        return counts

    def camp_counts(self, live_only: bool = False) -> dict[str, int]:
        """返回 {civilian, undercover, whiteboard} 的存活/总数分布字典。
        提供给 main.py _live_game_state 等外部调用方。"""
        return self._camp_count_live() if live_only else self._camp_count_all()

    def snapshot(self, visitor_player_number: int | None = None) -> dict[str, Any]:
        visitor = next(
            (p for p in self.players if p.number == visitor_player_number), None
        )
        reveal_all = self.phase == "finished"
        live_count = len([p for p in self.players if not p.is_out])
        counts_all = self._camp_count_all()
        counts_live = self._camp_count_live()
        players_public: list[dict[str, Any]] = []
        for p in self.players:
            info: dict[str, Any] = {
                "number": p.number,
                "player_number": p.number,
                "player_id": p.player_id,
                "display_name": p.display_name or f"{p.number}号",
                "is_out": p.is_out,
                "is_current_speaking": (
                    self.phase in ("speech", "pk")
                    and self.expected_speaker_number == p.number
                ),
                "is_expected_voter": (
                    self.phase == "voting"
                    and not p.is_out
                    and p.number in (self.pending_round.vote_player_numbers if self.pending_round else [])
                    and bool(self.pending_round and not self.pending_round.has_voted(p.number))
                ),
            }
            if reveal_all:
                info["camp"] = p.camp
                info["word"] = p.word or ""
            players_public.append(info)
        # 每轮完整发言（所有玩家都可以看）
        rounds_public: list[dict[str, Any]] = []
        for rnd in self.rounds:
            speeches = [
                {
                    "round_number": s.round_number,
                    "player_number": s.player_number,
                    "player_display": next(
                        (
                            p.display_name or f"{p.number}号"
                            for p in self.players
                            if p.number == s.player_number
                        ),
                        f"{s.player_number}号",
                    ),
                    "content": s.content,
                    "at": s.at,
                }
                for s in rnd.speeches
            ]
            vote_tally = []
            if self.phase == "voting" and rnd is self.pending_round:
                tally = rnd.votes_by_target()
                for pnum, count in tally.items():
                    vote_tally.append({"player_number": pnum, "votes": count})
            elif rnd.out_player_number is not None:
                tally = rnd.votes_by_target()
                for pnum, count in tally.items():
                    vote_tally.append({"player_number": pnum, "votes": count})
            rounds_public.append(
                {
                    "round_number": rnd.round_number,
                    "is_pk": bool(rnd.pk_reason),
                    "pk_reason": rnd.pk_reason,
                    "out_player_number": rnd.out_player_number,
                    "speech_order": list(rnd.speech_player_numbers),
                    "vote_player_numbers": list(rnd.vote_player_numbers),
                    "speeches": speeches,
                    "votes": [
                        {
                            "round_number": v.round_number,
                            "voter_number": v.voter_number,
                            "target_number": v.target_number,
                            "at": v.at,
                        }
                        for v in rnd.votes
                    ],
                    "vote_tally": vote_tally,
                }
            )
        # 我的身份：只有游戏开始后访问者有座位且身份已派发才显示
        my_info: dict[str, Any] = {
            "camp": None,
            "word": "",
            "is_player": False,
            "player_number": None,
        }
        if visitor is not None:
            my_info["is_player"] = True
            my_info["player_number"] = visitor.number
            if visitor.camp != "none":
                my_info["camp"] = visitor.camp
                my_info["word"] = visitor.word or ""
        winner_info: dict[str, Any] | None = None
        if self.phase == "finished" and self.winner_camp:
            winner_info = {
                "camp": self.winner_camp,
                "message": self.winner_message,
                "civilian_word": self.civilian_word,
                "undercover_word": self.undercover_word,
            }
        camp_info = {
            "show_real": reveal_all,
            "live_total": live_count,
            "total": len(self.players),
            # 各阵营总人数是公开信息（不直接标注具体玩家归属），始终返回供前端展示；
            # 存活分布 counts_live 会暴露被淘汰者的阵营，故进行中隐藏、结算时返回。
            "counts_all": counts_all,
            "counts_live": counts_live if reveal_all else None,
        }
        voted_this_round_player_numbers: list[int] = []
        if self.phase == "voting" and self.pending_round:
            voted_this_round_player_numbers = [
                v.voter_number for v in self.pending_round.votes
            ]
        pending_pk_targets: list[int] = []
        if self.pending_round is not None and self.pending_round.pk_reason:
            pending_pk_targets = list(self.pending_round.speech_player_numbers)
        return {
            "phase": self.phase,
            "round_number": len(self.rounds),
            "current_round_number": len(self.rounds),
            "camp_info": camp_info,
            "players": players_public,
            "players_public": players_public,
            "rounds": rounds_public,
            "rounds_public": rounds_public,
            "my": my_info,
            "winner": winner_info,
            "civilian_word_label": self.civilian_word if reveal_all else "",
            "undercover_word_label": self.undercover_word if reveal_all else "",
            "expected_speaker_number": self.expected_speaker_number,
            # 轮到时，真正的下一位发言人；结束后为 None
            "next_speaker_number": (
                self.speaking_order[self.current_speaker_index + 1]
                if self.phase in ("speech", "pk")
                and self.speaking_order
                and self.current_speaker_index + 1 < len(self.speaking_order)
                else None
            ),
            # 本轮剩余/待发言的完整队列，供前端展示“接下来是谁”
            "speech_queue": (
                list(self.speaking_order[self.current_speaker_index:])
                if self.phase in ("speech", "pk") and self.speaking_order
                else []
            ),
            "voter_player_number": (
                self.pending_round.vote_player_numbers[0]
                if self.phase == "voting"
                and self.pending_round
                and self.pending_round.vote_player_numbers
                else None
            ),
            "voting_all_voted": self.all_voted() if self.phase == "voting" else False,
            "vote_tally_live": (
                {
                    str(pnum): count
                    for pnum, count in self.vote_tally_view().items()
                }
                if self.phase == "voting" and self.pending_round
                else {}
            ),
            "voted_this_round_player_numbers": voted_this_round_player_numbers,
            "pending_pk_targets": pending_pk_targets,
            "last_pk_targets": pending_pk_targets,
            "first_round_non_voting": self.first_round_non_voting,
            "reveal_identity": self.reveal_identity,
            "similarity": self.similarity,
        }

    def progress(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "round_number": len(self.rounds),
            "players_total": len(self.players),
            "players_live": len([p for p in self.players if not p.is_out]),
            "winner": self.winner_camp,
        }
