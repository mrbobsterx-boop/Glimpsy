import random

from glimpsy.assembler import best_window, select_pieces
from glimpsy.config import Settings
from glimpsy.recorder.candidates import Candidate, CandidatePool
from glimpsy.recorder.pacing import make_plan, save_probability


def cand(i, t, score, priority=False, dur=8.0):
    return Candidate(id=i, file=f"cand_{i:05d}.ts", wall_start=t, wall_end=t + dur, want_start=t,
                     want_end=t + dur, monitor=1, width=1920, height=1080, score=score,
                     priority=priority, activity=[score] * int(dur))


def test_plan_numbers():
    s = Settings(target_length_s=60, clip_min_s=2, clip_max_s=5, pace="medium").validate()
    p = make_plan(s)
    assert p.clips_needed == 18           # 60 / 3.5
    assert 2 * p.clips_needed <= p.pool_size <= 3 * p.clips_needed
    assert p.candidate_s < s.buffer_s


def test_dynamic_pace_is_faster_and_shorter():
    calm = make_plan(Settings(pace="calm").validate())
    dyn = make_plan(Settings(pace="dynamic").validate())
    assert dyn.speed > calm.speed
    assert dyn.clip_out_s < calm.clip_out_s
    assert dyn.clips_needed > calm.clips_needed


def test_save_interval_grows_with_session():
    p = make_plan(Settings().validate())
    assert p.save_interval(60) == p.min_gap_s
    assert p.save_interval(8 * 3600) > p.save_interval(3600)


def test_active_moments_saved_more_often():
    p = make_plan(Settings().validate())
    assert save_probability(p, 3600, 0.8, 0.3) > save_probability(p, 3600, 0.05, 0.3)


def test_prune_keeps_priority_and_spread(tmp_path):
    pool = CandidatePool(tmp_path)
    for i in range(40):
        pool.add(cand(i, i * 100.0, score=random.Random(i).random()))
    pool.add(cand(99, 555.0, score=0.0, priority=True))
    pool.prune(10)
    assert sum(1 for c in pool.items if not c.priority) == 10
    assert any(c.priority for c in pool.items)
    starts = [c.wall_start for c in pool.items if not c.priority]
    assert max(starts) - min(starts) > 2000      # остались моменты со всего дня, а не с одного куска


def test_pool_persists(tmp_path):
    pool = CandidatePool(tmp_path)
    cid, path = pool.new_file()
    path.write_bytes(b"x")
    pool.add(cand(cid, 0.0, 0.5))
    pool.save()
    again = CandidatePool(tmp_path)
    assert again.count == 1 and again.next_id == 2


def test_select_pieces_fits_target():
    s = Settings(target_length_s=60, clip_min_s=2, clip_max_s=5, pace="medium").validate()
    plan = make_plan(s)
    cands = [cand(i, i * 60.0, score=(i % 5) / 5) for i in range(60)]
    cands.append(cand(100, 1000.0, 0.1, priority=True, dur=15))
    pieces = select_pieces(cands, plan, s, random.Random(1))
    total = sum(p.out_s for p in pieces)
    assert 45 <= total <= 75
    assert any(p.cand.priority for p in pieces)
    assert [p.cand.wall_start for p in pieces] == sorted(p.cand.wall_start for p in pieces)
    for p in pieces:
        assert p.offset >= 0 and p.offset + p.source_s <= p.cand.duration + 1e-6


def test_best_window_finds_activity():
    act = [0, 0, 0, 0, 1, 1, 1, 0, 0, 0]
    assert best_window(act, 3, 10, random.Random(0)) == 4.0
