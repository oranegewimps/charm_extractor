"""護石の変換・検証のテスト（APIは使わない）。

実行: python -m unittest test_validation -v
"""

import csv
import tempfile
import unittest
from pathlib import Path

import charm_extractor as ce

# 旧形式の出力（画像から書き起こしたもの）。全行レア度8・栄世の護石・0:00から1秒刻み。
LEGACY_ROWS = [
    ("貫通弾・竜の矢強化Lv1 / 水耐性Lv3", "武器:なし / 防具:1", ""),
    ("攻撃Lv3 / 納刀術Lv2 / ボマーLv1", "武器:2 / 防具:2", ""),
    ("匠Lv3 / 納刀術Lv2 / 植生学Lv2", "武器:なし / 防具:1,1,1", ""),
    ("雷属性攻撃強化Lv3 / 耳栓Lv1 / 気絶耐性Lv1", "武器:なし / 防具:1,1,1", ""),
    ("業物Lv3 / 裂傷耐性Lv2 / 早食いLv1", "武器:なし / 防具:1", ""),
    ("飛燕Lv1 / 集中Lv1 / 龍耐性Lv3", "武器:2 / 防具:2", ""),
    ("高速変形Lv3 / 属性変換Lv1", "武器:2 / 防具:2", ""),
    ("見切りLv3 / 力の解放Lv1", "武器:なし / 防具:1,1,1", ""),
    ("砲弾装填Lv2 / スタミナ奪取Lv1 / 渾身Lv1", "武器:なし / 防具:1,1,1", ""),
    ("匠Lv3 / 不明Lv1 / 不明Lv1", "武器:2 / 防具:2", "ノイズあり"),
    ("ガード性能Lv3 / 拘束耐性Lv2 / 広域化Lv2", "武器:2 / 防具:2", ""),
]


def legacy_entries() -> list[ce.Entry]:
    return [ce.legacy_entry("栄世の護石", "8", skills, slots, float(i), note)
            for i, (skills, slots, note) in enumerate(LEGACY_ROWS)]


def entry(rarity, weapon=(), armor=(), name=None, skills=(("攻撃", "1"), ("耳栓", "1"))) -> ce.Entry:
    if name is None:
        name = ce.rules.RARITY_NAMES[int(rarity)] if rarity != ce.UNKNOWN else "栄世の護石"
    return ce.Entry(
        name=name, rarity=rarity, skills=skills, weapon_slots=ce.sort_slots(weapon), armor_slots=ce.sort_slots(armor),
        start=0, end=0,
    )


def slot_messages(e: ce.Entry) -> list[str]:
    return [i.message for i in ce.validate(e) if i.kind == ce.ISSUE_SLOT]


class LegacyConversionTest(unittest.TestCase):
    def test_skills_split_into_name_and_level(self):
        self.assertEqual(ce.parse_legacy_skills("匠Lv3 / 不明Lv1 / 不明Lv1"),
                         (("匠", "3"), ("不明", "1"), ("不明", "1")))
        self.assertEqual(ce.parse_legacy_skills("貫通弾・竜の矢強化Lv1 / 水耐性Lv3"),
                         (("貫通弾・竜の矢強化", "1"), ("水耐性", "3")))

    def test_slots(self):
        self.assertEqual(ce.parse_legacy_slots("武器:なし / 防具:1,1,1"), ((), ("1", "1", "1")))
        self.assertEqual(ce.parse_legacy_slots("武器:2 / 防具:2"), (("2",), ("2",)))
        self.assertEqual(ce.parse_legacy_slots("武器:なし / 防具:1,2"), ((), ("2", "1")))  # 大きい順に詰める

    def test_csv_cells(self):
        e = legacy_entries()
        # 行10: 「不明Lv1」は スキル名=不明、Lv=1 に分かれる
        self.assertEqual(ce.skill_cells(e[9]), ["匠", "3", "不明", "1", "不明", "1"])
        # 行7: スキル3は存在しないので空欄（不明ではない）
        self.assertEqual(ce.skill_cells(e[6]), ["高速変形", "3", "属性変換", "1", "", ""])
        # 行1: レア度8なので武器の桁を必ず書く
        self.assertEqual(ce.slot_cells(e[0]), ["0", "1", "0", "0", "01"])
        self.assertEqual(ce.slot_cells(e[1]), ["2", "2", "0", "0", "22"])
        self.assertEqual(ce.slot_cells(e[2]), ["0", "1", "1", "1", "0111"])


class ValidationTest(unittest.TestCase):
    def test_all_legacy_rows_violate_slot_pattern(self):
        for i, e in enumerate(legacy_entries(), start=1):
            with self.subTest(row=i):
                messages = slot_messages(e)
                self.assertEqual(len(messages), 1)
                self.assertTrue(messages[0].startswith("スロットがパターン外：レア度8で武器スロ"))

    def test_specific_messages(self):
        e = legacy_entries()
        self.assertEqual(slot_messages(e[0]), ["スロットがパターン外：レア度8で武器スロなし（01）"])
        self.assertEqual(slot_messages(e[1]), ["スロットがパターン外：レア度8で武器スロ=2（22）"])

    def test_valid_patterns_have_no_issues(self):
        valid = [
            entry("8", ["1"]), entry("8", ["1"], ["1"]), entry("8", ["1"], ["1", "1"]),
            entry("5", [], ["1", "1"]), entry("5", [], ["2"]), entry("5", [], ["2", "1"]), entry("5", [], ["3"]),
            entry("6", [], ["1"]), entry("6", [], ["2", "1"]),
            entry("7", [], ["1"]), entry("7", [], ["3"]),
        ]
        for e in valid:
            with self.subTest(rarity=e.rarity, pattern=ce.slot_pattern(e)):
                self.assertEqual(ce.validate(e), [])
                e.issues = ce.validate(e)
                self.assertEqual(ce.note_cell(e), "")

    def test_invalid_patterns(self):
        self.assertEqual(slot_messages(entry("6", [], ["3"])), ["スロットがパターン外：レア度6で防具スロ=3（30）"])
        self.assertEqual(slot_messages(entry("5", ["1"], ["2", "1"])), ["スロットがパターン外：レア度5に武器スロ=1（121）"])
        self.assertEqual(slot_messages(entry("8", ["1"], ["2"])), ["スロットがパターン外：レア度8で防具スロ=2（12）"])
        self.assertEqual(slot_messages(entry("7", [], ["2", "1"])), ["スロットがパターン外：レア度7で防具スロ=2,1（21）"])

    def test_pattern_strings(self):
        self.assertEqual(ce.slot_pattern(entry("5", [], ["2"])), "20")
        self.assertEqual(ce.slot_pattern(entry("6", [], ["1"])), "10")
        self.assertEqual(ce.slot_pattern(entry("8", ["1"])), "10")
        self.assertEqual(ce.slot_pattern(entry("8", [], ["1"])), "01")
        self.assertEqual(ce.slot_pattern(entry("8", ["1"], ["1", "1"])), "111")

    def test_name_rarity_mismatch(self):
        e = entry("8", ["1"], name="秘歴の護石")
        self.assertEqual([i.message for i in ce.validate(e)], ["護石名とレア度が不一致（レア度8は栄世の護石）"])

    def test_skill_count(self):
        one = entry("8", ["1"], skills=(("攻撃", "1"),))
        four = entry("8", ["1"], skills=(("a", "1"), ("b", "1"), ("c", "1"), ("d", "1")))
        self.assertEqual([i.message for i in ce.validate(one)], ["スキル数が範囲外（1個）"])
        self.assertEqual([i.message for i in ce.validate(four)], ["スキル数が範囲外（4個）"])

    def test_multiple_issues_joined(self):
        e = entry("8", ["2"], name="秘歴の護石", skills=(("攻撃", "1"),))
        e.issues = ce.validate(e)
        self.assertEqual(ce.note_cell(e), "護石名とレア度が不一致（レア度8は栄世の護石）; "
                                          "スキル数が範囲外（1個）; スロットがパターン外：レア度8で武器スロ=2（20）")

    def test_unknown_skips_check(self):
        self.assertEqual(ce.validate(entry(ce.UNKNOWN, ["2"], ["3"])), [])  # レア度不明 → スロット検証しない
        self.assertEqual(ce.validate(entry("8", [ce.UNKNOWN], ["1"])), [])   # スロット不明 → スロット検証しない
        self.assertEqual(ce.validate(entry("8", ["1"], name=ce.UNKNOWN)), [])


class CorrectionTest(unittest.TestCase):
    def test_icon_count_correction(self):
        entries = ce.review_entries(legacy_entries(), recheck=lambda e, issues: self.fail("再確認は呼ばれないはず"))
        patterns = [ce.slot_pattern(e) for e in entries]
        # アイコン個数: 1,2,3,3,1,2,2,3,3,2,2 → 10 / 11 / 111
        self.assertEqual(patterns, ["10", "11", "111", "111", "10", "11", "11", "111", "111", "11", "11"])
        for e in entries:
            self.assertEqual(e.issues, [])
            self.assertTrue(e.flagged)  # 並び順の確認のため要確認にする
            self.assertTrue(any(n.startswith("個数からパターンを補正") for n in e.notes))
        self.assertIn("ノイズあり", ce.note_cell(entries[9]))

    def test_gemini_icon_count_preferred(self):
        e = entry("8", ["2"], ["2"])
        e.icon_count = 3
        self.assertTrue(ce.correct_by_icon_count(e))
        self.assertEqual(ce.slot_pattern(e), "111")

    def test_no_correction_for_other_rarities(self):
        self.assertFalse(ce.correct_by_icon_count(entry("6", [], ["3"])))


class RecheckTest(unittest.TestCase):
    RAW_OK = {"name": "栄世の護石", "rarity": "8", "skills": [], "skill_count": "不明",
              "slot_icon_count": "2", "weapon_slots": ["1"], "armor_slots": ["1"],
              "slot_confidence": "高", "confidence": "高", "note": ""}

    def test_recheck_adopted(self):
        e = entry("8", ["2"], ["2"])
        e.icon_count = 5  # 個数補正ができない → 再確認へ
        [result] = ce.review_entries([e], recheck=lambda e, issues: self.RAW_OK)
        self.assertEqual(ce.slot_pattern(result), "11")
        self.assertEqual(result.issues, [])
        self.assertFalse(result.flagged)
        self.assertEqual(result.skills, (("攻撃", "1"), ("耳栓", "1")))  # スキルは元の読み取りを残す
        self.assertTrue(any(n.startswith("再確認で修正") for n in result.notes))

    def test_recheck_still_invalid(self):
        e = entry("8", ["2"], ["2"])
        e.icon_count = 5
        calls = []

        def recheck(e, issues):
            calls.append(issues)
            return dict(self.RAW_OK, weapon_slots=["3"], slot_icon_count="不明")

        [result] = ce.review_entries([e], recheck=recheck)
        self.assertEqual(len(calls), 1)  # 1回だけ
        self.assertEqual(ce.slot_pattern(result), "22")
        self.assertIn("再確認しても不正", ce.note_cell(result))
        self.assertIn("スロットがパターン外", ce.note_cell(result))
        self.assertTrue(result.flagged)

    def test_name_mismatch_rechecked(self):
        e = entry("8", ["1"], name="秘歴の護石")
        [result] = ce.review_entries([e], recheck=lambda e, issues: self.RAW_OK)
        self.assertEqual(result.name, "栄世の護石")
        self.assertEqual(result.issues, [])


class CsvTest(unittest.TestCase):
    def test_write_csv(self):
        entries = ce.review_entries(legacy_entries(), recheck=lambda e, issues: None)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "out.csv"
            ce.write_csv(entries, path)
            self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            with path.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0], ce.CSV_HEADER)
        self.assertEqual(rows[1][:14], ["001", "栄世の護石", "8", "貫通弾・竜の矢強化", "1", "水耐性", "3", "", "",
                                        "1", "0", "0", "0", "10"])
        self.assertEqual(rows[1][15], "00:00")
        self.assertEqual(rows[11][15], "00:10")

    def test_duplicates_kept_and_flagged(self):
        a1, b, a2 = entry("8", ["1"]), entry("8", ["1"], ["1"]), entry("8", ["1"])
        a1.start = a1.end = 0
        b.start = b.end = 5
        a2.start = a2.end = 10
        entries = ce.review_entries([a1, b, a2], recheck=lambda e, issues: None)
        self.assertEqual(len(entries), 3)
        self.assertEqual([e.duplicate for e in entries], [True, False, True])


if __name__ == "__main__":
    unittest.main()
