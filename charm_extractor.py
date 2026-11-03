"""モンスターハンターワイルズの護石「装備詳細」画面を録画した動画から、護石一覧CSVを作るツール。

使い方:
    python charm_extractor.py 入力動画.mp4 出力.csv
    python charm_extractor.py 入力動画.mp4 出力.csv --save-flagged-frames   # 要確認の行の画面をPNG保存
    python charm_extractor.py 入力動画.mp4 出力.csv --rebuild-from-cache    # APIを呼ばずにキャッシュからCSVを作り直す
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from google import genai
from google.genai import errors, types

import talisman_rules as rules

# ===== 設定 =====
MODEL_NAME = "gemini-3.8-flash"  # 動画入力対応の安定版。より高精度にしたい場合は "gemini-4-argon"（有料のみ）
CHUNK_SEC = 300                  # 分割の長さ（秒）
OVERLAP_SEC = 5                  # チャンク同士の重なり（秒）
MERGE_GAP_SEC = 3.0              # 同一内容のエントリをこの秒数以内の間隔なら連続とみなして統合
SAMPLING_FPS = 1.0               # Geminiに送るフレームレート（0より大きく24以下）
MEDIA_RESOLUTION = None          # 文字が読めない場合は "MEDIA_RESOLUTION_HIGH" を試す
RECHECK_PAD_SEC = 2.0            # 再確認で切り出す区間の、前後の余白（秒）
CACHE_DIR = Path(__file__).resolve().parent / "cache"
MAX_RETRIES = 6                  # API呼び出しのリトライ回数
RETRY_BASE_SEC = 5.0             # 指数バックオフの初回待ち時間（秒）
RETRY_MAX_SEC = 120.0            # 指数バックオフの待ち時間上限（秒）
FILE_POLL_SEC = 5.0              # アップロード後のACTIVE確認間隔（秒）
FILE_ACTIVE_TIMEOUT_SEC = 600    # ACTIVEになるまで待つ上限（秒）
PROMPT_VERSION = 2               # プロンプトやスキーマを変えたら上げる（キャッシュが作り直される）
FINGERPRINT_BYTES = 8 * 1024 * 1024  # キャッシュ識別用に読む、動画の先頭・末尾のバイト数

logger = logging.getLogger("charm_extractor")
logger.setLevel(logging.INFO)

UNKNOWN = "不明"
NONE_LABEL = "なし"
DUPLICATE_LABEL = "重複の可能性あり"
CONFIDENCE_ORDER = {"高": 2, "中": 1, "低": 0}
RETRYABLE_CODES = {408, 429, 500, 502, 503, 504}
MAX_SKILL_COLUMNS = 3
MAX_ARMOR_COLUMNS = 3

# 検証で見つかる問題の種類
ISSUE_SLOT = "slot"
ISSUE_NAME_RARITY = "name_rarity"
ISSUE_SKILL_COUNT = "skill_count"
ISSUE_SKILL_GROUP = "skill_group"
RECHECK_KINDS = {ISSUE_SLOT, ISSUE_NAME_RARITY}  # Geminiに聞き直す問題

CSV_HEADER = [
    "管理番号", "護石名", "レア度",
    "スキル1名", "スキル1Lv", "スキル2名", "スキル2Lv", "スキル3名", "スキル3Lv",
    "武器スロ", "防具スロ1", "防具スロ2", "防具スロ3", "スロットパターン",
    "重複フラグ", "動画内時刻", "備考",
]


# ===== スロットパターン =====

def sort_slots(values) -> tuple[str, ...]:
    """スロットレベルを大きい順に並べる。0（空き）は除き、不明は末尾に置く。"""
    known = sorted((v for v in values if v not in (UNKNOWN, "0")), key=int, reverse=True)
    return tuple(known) + tuple(v for v in values if v == UNKNOWN)


def parse_pattern(rarity: int, pattern: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """パターン文字列を (武器スロ, 防具スロ) に分ける。先頭桁が武器スロになるのは武器スロットが付くレア度だけ。"""
    digits = list(pattern)
    weapon: list[str] = []
    if rarity in rules.WEAPON_SLOT_RARITIES:
        weapon = [digits.pop(0)]
    return sort_slots(weapon), sort_slots(digits)


def pattern_label(rarity: int, pattern: str) -> str:
    weapon, armor = parse_pattern(rarity, pattern)
    parts = ([f"武器{','.join(weapon)}"] if weapon else []) + ([f"防具{','.join(armor)}"] if armor else [])
    return "＋".join(parts) or "スロットなし"


# ===== プロンプト・スキーマ =====

def describe_rules() -> str:
    weapon_rarities = "・".join(f"レア度{r}" for r in sorted(rules.WEAPON_SLOT_RARITIES))
    lines = [
        "## 護石の仕様（モンスターハンターワイルズ）",
        "- レア度と護石名の対応: " + "、".join(f"レア度{r}＝{n}" for r, n in rules.RARITY_NAMES.items()),
        f"- 武器スロットが付くのは{weapon_rarities}だけで、レベルは必ず1。それ以外のレア度に武器スロットは無い。",
        "- レア度8の防具スロットはレベル1のみ（最大2個）。",
        "- スロットの許容パターン（数字は「武器スロ→防具スロ（大きい順）」を連結したもの。0は空き。"
        f"先頭の桁が武器スロになるのは{weapon_rarities}だけ）:",
    ]
    for rarity, patterns in rules.SLOT_PATTERNS.items():
        lines.append(f"  - レア度{rarity}: " + " / ".join(f"{p}（{pattern_label(rarity, p)}）" for p in patterns))
    for rarity, table in rules.ICON_COUNT_PATTERNS.items():
        lines.append(
            f"- レア度{rarity}は、スロットアイコンの個数でパターンが確定する: "
            + "、".join(f"{n}個なら{p}（{pattern_label(rarity, p)}）" for n, p in table.items())
        )
    lines.append(
        f"- スキル数は{rules.SKILL_COUNT_MIN}個または{rules.SKILL_COUNT_MAX}個。"
        "レア度8は、スキル1が武器系スキル、スキル2・3が防具系スキル。"
    )
    return "\n".join(lines)


SLOT_READING_STEPS = f"""\
## スロットの読み取り手順
スロットは画面右上に並ぶ小さなアイコンです。次の手順で慎重に読んでください。
1. まずスロットアイコンの個数（1〜3個）を数え、slot_icon_count に入れる。アイコンが無ければ "0"。
2. アイコン1つずつについて、剣マーク（武器スロット）か頭マーク（防具スロット）かをよく見て区別する。
3. 各アイコンの中にある〇の数でレベルを判断する（〇1つ＝"1"、〇2つ＝"2"、〇3つ＝"3"）。
4. 剣マークのレベルを weapon_slots に、頭マークのレベルを armor_slots に、左から順に入れる。その種類が無ければ空配列。
5. 結果が上の許容パターンに当てはまるか確認する。当てはまらなければアイコンを読み直す。
   レア度8なら、アイコンの個数で確定するパターンと一致しているか必ず確認する。
6. 読み直しても自信がなければ、推測せず "{UNKNOWN}" にする（アイコン自体が読めなければ配列を ["{UNKNOWN}"]、〇の数だけ判別できなければその要素を "{UNKNOWN}"）。
7. slot_confidence にスロット判定の確信度（"高" / "中" / "低"）を入れる。"""

FIELD_GUIDE = f"""\
## 読み取る項目
- name: 護石名（例: 栄世の護石）
- rarity: レア度の数字のみ（例: 「RARE 8」なら "8"）
- skills: 装備スキル（画面の表示順）。name はスキル名のみ（Lvを含めない）、level はレベルの数字のみ
  （例: 「逆襲 Lv1」なら name="逆襲", level="1"）。スキル名だけ・レベルだけが読めない場合は、読めない方だけ "{UNKNOWN}"。
- skill_count: スキルの数（"2" / "3"）。判別できなければ "{UNKNOWN}"
- confidence: その護石の読み取り全体への確信度（"高" / "中" / "低"）
- note: 読み取りに不安がある箇所があれば短く日本語で。無ければ空文字。

## 重要
- 読み取れない・見えない項目は推測せず、必ず "{UNKNOWN}" としてください。"""

PROMPT = f"""\
この動画はモンスターハンターワイルズのゲーム画面の録画で、護石の「装備詳細」画面が1つずつ順番に映っています。
画面に映っている護石を読み取り、JSONで返してください。

## エントリの区切り方
- 画面に映る護石が切り替わるたびに1件のエントリとして記録してください。
- 同じ護石の画面が映り続けている間は1件にまとめ、start_sec〜end_sec でその区間を示してください。
- 一度別の護石に切り替わった後に同じ内容の護石が映った場合も、別のエントリとして記録してください。
- start_sec / end_sec は、この動画の先頭からの秒数です（小数可）。
- 護石の装備詳細画面以外（メニューの切り替え中、護石以外の装備など）は記録しないでください。

{describe_rules()}

{SLOT_READING_STEPS}

{FIELD_GUIDE}
"""

RECHECK_PROMPT_TEMPLATE = f"""\
この動画はモンスターハンターワイルズの護石の「装備詳細」画面を映した短い区間です。
映っている護石1つを、特にスロットと護石名・レア度に集中して、厳密に読み取ってください。

前回の読み取りでは、次の問題が見つかりました: {{issues}}
前回の結果を前提にせず、画面を見て読み直してください。

{describe_rules()}

{SLOT_READING_STEPS}

{FIELD_GUIDE}
"""

CHARM_PROPERTIES = {
    "name": {"type": "string", "description": f"護石名。読めなければ「{UNKNOWN}」"},
    "rarity": {"type": "string", "description": f"レア度の数字のみ。読めなければ「{UNKNOWN}」"},
    "skills": {
        "type": "array",
        "description": "装備スキル（表示順）",
        "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "スキル名のみ（Lvを含めない）"},
                "level": {"type": "string", "description": "レベルの数字のみ"},
            },
            "required": ["name", "level"],
        },
    },
    "skill_count": {"type": "string", "description": f"スキルの数。判別できなければ「{UNKNOWN}」"},
    "slot_icon_count": {"type": "string", "description": f"スロットアイコンの個数（0〜3）。判別できなければ「{UNKNOWN}」"},
    "weapon_slots": {
        "type": "array",
        "description": "剣マークのスロット。アイコン内の〇の数を左から順に",
        "items": {"type": "string"},
    },
    "armor_slots": {
        "type": "array",
        "description": "頭マークのスロット。アイコン内の〇の数を左から順に",
        "items": {"type": "string"},
    },
    "slot_confidence": {"type": "string", "enum": ["高", "中", "低"]},
    "confidence": {"type": "string", "enum": ["高", "中", "低"]},
    "note": {"type": "string", "description": "読み取りに不安がある点（無ければ空文字）"},
}

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "charms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    **CHARM_PROPERTIES,
                    "start_sec": {"type": "number", "description": "この画面が映り始めた秒数"},
                    "end_sec": {"type": "number", "description": "この画面が映り終わった秒数"},
                },
                "required": [*CHARM_PROPERTIES, "start_sec", "end_sec"],
            },
        }
    },
    "required": ["charms"],
}

RECHECK_SCHEMA = {
    "type": "object",
    "properties": CHARM_PROPERTIES,
    "required": list(CHARM_PROPERTIES),
}


# ===== データ =====

@dataclass
class Chunk:
    index: int
    start: float
    duration: float


@dataclass
class Issue:
    kind: str
    message: str


@dataclass
class Entry:
    name: str
    rarity: str
    skills: tuple[tuple[str, str], ...]
    weapon_slots: tuple[str, ...]
    armor_slots: tuple[str, ...]
    start: float
    end: float
    confidence: str = "高"
    slot_confidence: str = "高"
    icon_count: int | None = None
    notes: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    needs_review: bool = False  # 補正や再確認の失敗など、目視で確かめたほうがよい行
    duplicate: bool = False

    @property
    def key(self) -> tuple:
        return (self.name, self.rarity, self.skills, self.weapon_slots, self.armor_slots)

    @property
    def flagged(self) -> bool:
        """要確認（検証に引っかかった、または補正・再確認の失敗があった）か。"""
        return bool(self.issues) or self.needs_review


# ===== ffmpeg =====

def probe_duration(video: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def plan_chunks(duration: float) -> list[Chunk]:
    chunks = []
    start = 0.0
    while True:
        end = min(start + CHUNK_SEC + OVERLAP_SEC, duration)
        chunks.append(Chunk(len(chunks), start, end - start))
        if end >= duration:
            return chunks
        start += CHUNK_SEC


def cut_clip(video: Path, start: float, duration: float, out_path: Path) -> None:
    # 開始時刻を正確にするため、ストリームコピーではなく再エンコードする
    tmp_path = out_path.with_name(out_path.stem + ".part.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(video),
         "-t", f"{duration:.3f}", "-an", "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "18", "-pix_fmt", "yuv420p", str(tmp_path)],
        check=True,
    )
    tmp_path.replace(out_path)


def save_frame(video: Path, sec: float, out_path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-ss", f"{sec:.3f}", "-i", str(video),
         "-frames:v", "1", str(out_path)],
        check=True,
    )


# ===== Gemini =====

def with_retry(label: str, func):
    for attempt in range(MAX_RETRIES + 1):
        try:
            return func()
        except errors.APIError as e:
            if e.code not in RETRYABLE_CODES or attempt == MAX_RETRIES:
                raise
            reason = f"{e.code} {e.status}"
        except (json.JSONDecodeError, ConnectionError, TimeoutError, OSError) as e:
            if attempt == MAX_RETRIES:
                raise
            reason = f"{type(e).__name__}: {e}"
        wait = min(RETRY_BASE_SEC * 2 ** attempt, RETRY_MAX_SEC) * random.uniform(0.8, 1.2)
        logger.info(f"  {label} 失敗（{reason}）。{wait:.0f}秒後にリトライ {attempt + 1}/{MAX_RETRIES}")
        time.sleep(wait)


def wait_until_active(client: genai.Client, file: types.File) -> types.File:
    deadline = time.monotonic() + FILE_ACTIVE_TIMEOUT_SEC
    while file.state != types.FileState.ACTIVE:
        if file.state == types.FileState.FAILED:
            raise RuntimeError(f"Files API での動画処理に失敗しました: {file.name}")
        if time.monotonic() > deadline:
            raise TimeoutError(f"動画が ACTIVE になりませんでした: {file.name}")
        time.sleep(FILE_POLL_SEC)
        file = with_retry("状態確認", lambda: client.files.get(name=file.name))
    return file


def ask_gemini(client: genai.Client, clip_path: Path, prompt: str, schema: dict) -> dict:
    file = with_retry("アップロード", lambda: client.files.upload(file=str(clip_path)))
    try:
        file = wait_until_active(client, file)
        video_part = types.Part(
            file_data=types.FileData(file_uri=file.uri, mime_type=file.mime_type),
            video_metadata=types.VideoMetadata(fps=SAMPLING_FPS),
        )
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema,
            media_resolution=MEDIA_RESOLUTION,
        )

        def generate() -> dict:
            response = client.models.generate_content(
                model=MODEL_NAME, contents=[video_part, prompt], config=config,
            )
            if not response.text:
                raise json.JSONDecodeError("空の応答", "", 0)
            return json.loads(response.text)

        return with_retry("解析", generate)
    finally:
        try:
            client.files.delete(name=file.name)
        except errors.APIError:
            pass  # 削除できなくても48時間で自動削除される


# ===== 正規化 =====

def normalize_number(value) -> str:
    match = re.search(r"\d+", str(value))
    return match.group() if match else UNKNOWN


def normalize_text(value) -> str:
    text = str(value or "").strip()
    return text if text else UNKNOWN


def normalize_slots(values) -> tuple[str, ...]:
    return sort_slots([normalize_number(v) for v in values or []])


def normalize_confidence(value) -> str:
    return value if value in CONFIDENCE_ORDER else "低"


def parse_charm(raw: dict) -> dict:
    """Geminiの応答1件を、Entryの内容部分（時刻以外）に変換する。"""
    skills = [(normalize_text(s.get("name")), normalize_number(s.get("level"))) for s in raw.get("skills") or []]
    notes = []
    count = normalize_number(raw.get("skill_count"))
    if count != UNKNOWN and int(count) > len(skills):
        skills += [(UNKNOWN, UNKNOWN)] * (int(count) - len(skills))
    elif count != UNKNOWN and int(count) < len(skills):
        notes.append(f"スキル数の読み取りが食い違い（{count}個と回答）")
    note = str(raw.get("note") or "").strip()
    if note:
        notes.append(note)
    icon_count = normalize_number(raw.get("slot_icon_count"))
    return dict(
        name=normalize_text(raw.get("name")),
        rarity=normalize_number(raw.get("rarity")),
        skills=tuple(skills),
        weapon_slots=normalize_slots(raw.get("weapon_slots")),
        armor_slots=normalize_slots(raw.get("armor_slots")),
        confidence=normalize_confidence(raw.get("confidence")),
        slot_confidence=normalize_confidence(raw.get("slot_confidence")),
        icon_count=None if icon_count == UNKNOWN else int(icon_count),
        notes=notes,
    )


def to_entry(raw: dict, chunk: Chunk) -> Entry:
    start = min(max(float(raw.get("start_sec") or 0), 0), chunk.duration)
    end = min(max(float(raw.get("end_sec") or start), start), chunk.duration)
    return Entry(**parse_charm(raw), start=chunk.start + start, end=chunk.start + end)


# ===== 旧形式（スキル・スロットが1セルの文字列）からの変換 =====

LEGACY_SPLIT_RE = re.compile(r"\s*[/／]\s*")
LEGACY_SKILL_RE = re.compile(r"^(.*?)\s*Lv\s*(\S*)$", re.IGNORECASE)


def parse_legacy_skills(text: str) -> tuple[tuple[str, str], ...]:
    """「匠Lv3 / 不明Lv1」→ (("匠","3"), ("不明","1"))"""
    text = text.strip()
    if text in ("", UNKNOWN):
        return ()
    skills = []
    for part in LEGACY_SPLIT_RE.split(text):
        match = LEGACY_SKILL_RE.match(part)
        name, level = (match.group(1), match.group(2)) if match else (part, "")
        skills.append((normalize_text(name), normalize_number(level)))
    return tuple(skills)


def parse_legacy_slots(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """「武器:2 / 防具:1,1」→ (("2",), ("1","1"))"""
    text = text.strip()
    if text == UNKNOWN:
        return (UNKNOWN,), (UNKNOWN,)
    weapon: tuple[str, ...] = ()
    armor: tuple[str, ...] = ()
    for part in LEGACY_SPLIT_RE.split(text):
        label, _, value = part.replace("：", ":").partition(":")
        value = value.strip()
        slots = () if value in ("", NONE_LABEL) else normalize_slots(value.split(","))
        if "武器" in label:
            weapon = slots
        elif "防具" in label:
            armor = slots
    return weapon, armor


def legacy_entry(name: str, rarity: str, skills: str, slots: str, start_sec: float, note: str = "") -> Entry:
    weapon, armor = parse_legacy_slots(slots)
    return Entry(
        name=normalize_text(name),
        rarity=normalize_number(rarity),
        skills=parse_legacy_skills(skills),
        weapon_slots=weapon,
        armor_slots=armor,
        start=start_sec,
        end=start_sec,
        notes=[note] if note else [],
    )


# ===== 検証 =====

def has_unknown_slot(e: Entry) -> bool:
    return UNKNOWN in e.weapon_slots or UNKNOWN in e.armor_slots


def slot_pattern(e: Entry) -> str:
    """CSV用のパターン文字列。武器スロットが付くレア度、または武器スロがある行は武器の桁を必ず書く。"""
    if has_unknown_slot(e):
        return UNKNOWN
    has_weapon_rarity = e.rarity != UNKNOWN and int(e.rarity) in rules.WEAPON_SLOT_RARITIES
    digits = list(e.weapon_slots or ("0",)) if has_weapon_rarity or e.weapon_slots else []
    return "".join(digits + list(e.armor_slots)).ljust(2, "0")


def format_slot_reading(e: Entry) -> str:
    def group(slots):
        return ",".join(slots) if slots else NONE_LABEL
    return f"武器:{group(e.weapon_slots)} / 防具:{group(e.armor_slots)}"


def check_name_rarity(e: Entry) -> list[Issue]:
    if e.rarity == UNKNOWN:
        return []
    expected = rules.RARITY_NAMES.get(int(e.rarity))
    if expected is None:
        return [Issue(ISSUE_NAME_RARITY, f"レア度{e.rarity}は対象外")]
    if e.name != UNKNOWN and e.name != expected:
        return [Issue(ISSUE_NAME_RARITY, f"護石名とレア度が不一致（レア度{e.rarity}は{expected}）")]
    return []


def check_skill_count(e: Entry) -> list[Issue]:
    count = len(e.skills)
    if count == 0:  # スキル欄が読めていない
        return []
    if not rules.SKILL_COUNT_MIN <= count <= rules.SKILL_COUNT_MAX:
        return [Issue(ISSUE_SKILL_COUNT, f"スキル数が範囲外（{count}個）")]
    return []


def check_skill_groups(e: Entry) -> list[Issue]:
    """スキル位置ごとのグループの検証。talisman_rules.SKILL_GROUPS が空の間は何もしない。"""
    if not rules.SKILL_GROUPS or e.rarity == UNKNOWN:
        return []
    positions = rules.SKILL_POSITION_GROUPS.get(int(e.rarity), [])
    issues = []
    for i, ((name, _), group) in enumerate(zip(e.skills, positions), start=1):
        if name != UNKNOWN and name not in rules.SKILL_GROUPS.get(group, set()):
            issues.append(Issue(ISSUE_SKILL_GROUP, f"スキル{i}（{name}）が{group}系スキルではない"))
    return issues


def check_slots(e: Entry) -> list[Issue]:
    if e.rarity == UNKNOWN or has_unknown_slot(e):
        return []
    rarity = int(e.rarity)
    patterns = rules.SLOT_PATTERNS.get(rarity)
    if patterns is None:
        return []
    if (e.weapon_slots, e.armor_slots) in {parse_pattern(rarity, p) for p in patterns}:
        return []
    if rarity in rules.WEAPON_SLOT_RARITIES and not e.weapon_slots:
        detail = f"レア度{rarity}で武器スロなし"
    elif rarity in rules.WEAPON_SLOT_RARITIES and e.weapon_slots != ("1",):
        detail = f"レア度{rarity}で武器スロ={','.join(e.weapon_slots)}"
    elif rarity not in rules.WEAPON_SLOT_RARITIES and e.weapon_slots:
        detail = f"レア度{rarity}に武器スロ={','.join(e.weapon_slots)}"
    else:
        detail = f"レア度{rarity}で防具スロ={','.join(e.armor_slots) or NONE_LABEL}"
    return [Issue(ISSUE_SLOT, f"スロットがパターン外：{detail}（{slot_pattern(e)}）")]


VALIDATORS: list[Callable[[Entry], list[Issue]]] = [
    check_name_rarity, check_skill_count, check_skill_groups, check_slots,
]


def validate(e: Entry) -> list[Issue]:
    return [issue for check in VALIDATORS for issue in check(e)]


# ===== 補正・再確認 =====

def correct_by_icon_count(e: Entry) -> bool:
    """アイコンの個数からパターンが確定するレア度なら、スロットを置き換える。置き換えたら True。"""
    if e.rarity == UNKNOWN:
        return False
    rarity = int(e.rarity)
    table = rules.ICON_COUNT_PATTERNS.get(rarity)
    if not table:
        return False
    count = e.icon_count
    if count is None and not has_unknown_slot(e):
        count = len(e.weapon_slots) + len(e.armor_slots)
    if count not in table:
        return False
    before = format_slot_reading(e)
    e.weapon_slots, e.armor_slots = parse_pattern(rarity, table[count])
    e.notes.append(f"個数からパターンを補正（元の読み取り {before}、アイコン{count}個）")
    e.needs_review = True
    return True


def recheck_entry(e: Entry, issues: list[Issue], recheck: Callable[[Entry, list[Issue]], dict | None]) -> None:
    """Geminiに1回だけ聞き直し、パターンに合えば採用する。"""
    raw = recheck(e, issues)
    if raw is None:
        e.notes.append("再確認未実施（キャッシュなし）")
        return
    candidate = Entry(**parse_charm(raw), start=e.start, end=e.end)
    if {i.kind for i in validate(candidate)} & RECHECK_KINDS:
        e.notes.append("再確認しても不正")
        e.needs_review = True
        return
    before = f"{e.name} レア度{e.rarity} {format_slot_reading(e)}"
    e.name, e.rarity = candidate.name, candidate.rarity
    e.weapon_slots, e.armor_slots = candidate.weapon_slots, candidate.armor_slots
    e.icon_count, e.slot_confidence = candidate.icon_count, candidate.slot_confidence
    e.notes.append(f"再確認で修正（元の読み取り {before}）")


def review_entries(entries: list[Entry], recheck: Callable[[Entry, list[Issue]], dict | None]) -> list[Entry]:
    """検証 → 個数による補正 → 不正行の再確認 → 再統合 → 最終検証・重複フラグ。"""
    for e in entries:
        issues = validate(e)
        kinds = {i.kind for i in issues}
        if ISSUE_SLOT in kinds and ISSUE_NAME_RARITY not in kinds and correct_by_icon_count(e):
            continue
        if kinds & RECHECK_KINDS:
            recheck_entry(e, [i for i in issues if i.kind in RECHECK_KINDS], recheck)
    entries = merge_consecutive(entries)
    for e in entries:
        e.issues = validate(e)
    flag_duplicates(entries)
    return entries


# ===== 統合・重複 =====

def merge_consecutive(entries: list[Entry]) -> list[Entry]:
    """同一内容で時間的に連続しているエントリ（同じ画面の映り続け・チャンクの重なり）を統合する。"""
    merged: list[Entry] = []
    for entry in sorted(entries, key=lambda e: (e.start, e.end)):
        last = merged[-1] if merged else None
        if last and last.key == entry.key and entry.start - last.end <= MERGE_GAP_SEC:
            last.end = max(last.end, entry.end)
            if CONFIDENCE_ORDER[entry.confidence] < CONFIDENCE_ORDER[last.confidence]:
                last.confidence = entry.confidence
            if CONFIDENCE_ORDER[entry.slot_confidence] < CONFIDENCE_ORDER[last.slot_confidence]:
                last.slot_confidence = entry.slot_confidence
            last.notes += [n for n in entry.notes if n not in last.notes]
            last.needs_review = last.needs_review or entry.needs_review
        else:
            merged.append(entry)
    return merged


def flag_duplicates(entries: list[Entry]) -> None:
    counts = Counter(e.key for e in entries)
    for entry in entries:
        entry.duplicate = counts[entry.key] >= 2


# ===== CSV =====

def numbered(entries: list[Entry]) -> list[tuple[str, Entry]]:
    width = max(3, len(str(len(entries))))
    return [(str(i).zfill(width), e) for i, e in enumerate(entries, start=1)]


def format_time(sec: float) -> str:
    total = int(sec)
    return f"{total // 60:02d}:{total % 60:02d}"


def skill_cells(e: Entry) -> list[str]:
    if not e.skills:
        return [UNKNOWN] * (MAX_SKILL_COLUMNS * 2)
    cells = []
    for i in range(MAX_SKILL_COLUMNS):
        cells += list(e.skills[i]) if i < len(e.skills) else ["", ""]  # 存在しないスキルは空欄
    return cells


def slot_cells(e: Entry) -> list[str]:
    weapon = UNKNOWN if UNKNOWN in e.weapon_slots else (e.weapon_slots[0] if e.weapon_slots else "0")
    if e.armor_slots == (UNKNOWN,):
        armor = [UNKNOWN] * MAX_ARMOR_COLUMNS
    else:
        armor = list(e.armor_slots[:MAX_ARMOR_COLUMNS])
        armor += ["0"] * (MAX_ARMOR_COLUMNS - len(armor))
    return [weapon, *armor, slot_pattern(e)]


def note_cell(e: Entry) -> str:
    notes = [i.message for i in e.issues]
    if e.confidence == "低":
        notes.append("確信度:低")
    if e.slot_confidence == "低":
        notes.append("スロット確信度:低")
    return "; ".join(notes + e.notes)


def csv_row(number: str, e: Entry) -> list[str]:
    return [
        number, e.name, e.rarity, *skill_cells(e), *slot_cells(e),
        DUPLICATE_LABEL if e.duplicate else "", format_time(e.start), note_cell(e),
    ]


def write_csv(entries: list[Entry], out_path: Path) -> None:
    with out_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        writer.writerows(csv_row(number, e) for number, e in numbered(entries))


def save_flagged_frames(video: Path, entries: list[Entry], frames_dir: Path) -> int:
    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("*.png"):  # 管理番号が変わるため、前回の画像は消す
        old.unlink()
    saved = 0
    for number, e in numbered(entries):
        if e.flagged:
            save_frame(video, (e.start + e.end) / 2, frames_dir / f"{number}.png")
            saved += 1
    return saved


# ===== メイン =====

def cache_dir_for(video: Path) -> Path:
    # ファイル名や保存場所が変わっても同じ動画ならキャッシュを再利用できるよう、中身から識別する
    size = video.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with video.open("rb") as f:
        h.update(f.read(FINGERPRINT_BYTES))
        f.seek(max(size - FINGERPRINT_BYTES, 0))
        h.update(f.read(FINGERPRINT_BYTES))
    h.update(json.dumps([
        MODEL_NAME, CHUNK_SEC, OVERLAP_SEC, SAMPLING_FPS, MEDIA_RESOLUTION, PROMPT_VERSION,
    ]).encode())
    return CACHE_DIR / h.hexdigest()[:16]


def check_environment(api_key: str | None, need_api: bool = True) -> str | None:
    """実行に必要なものが揃っていなければ、その理由を返す。"""
    if need_api and not api_key:
        return "Gemini APIキーが設定されていません。README.md を参照してください。"
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            return f"{tool} が見つかりません。README.md を参照してインストールしてください。"
    return None


def make_rechecker(client: genai.Client | None, video: Path, duration: float, cache_dir: Path):
    def recheck(e: Entry, issues: list[Issue]) -> dict | None:
        start = max(e.start - RECHECK_PAD_SEC, 0)
        end = min(e.end + RECHECK_PAD_SEC, duration)
        json_path = cache_dir / f"recheck_{int(start * 1000):09d}_{int(end * 1000):09d}.json"
        if json_path.exists():
            return json.loads(json_path.read_text(encoding="utf-8"))
        if client is None:
            return None
        label = f"[再確認] {format_time(e.start)}"
        logger.info(f"{label} {' / '.join(i.message for i in issues)}")
        clip_path = json_path.with_suffix(".mp4")
        cut_clip(video, start, end - start, clip_path)
        prompt = RECHECK_PROMPT_TEMPLATE.format(issues="、".join(i.message for i in issues))
        result = ask_gemini(client, clip_path, prompt, RECHECK_SCHEMA)
        json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        clip_path.unlink()
        return result
    return recheck


def run(video: Path, output: Path, api_key: str | None, *,
        rebuild_from_cache: bool = False, frames_dir: Path | None = None) -> list[Entry]:
    """動画を解析してCSVを書き出す。rebuild_from_cache ならAPIを呼ばない。frames_dir を渡すと要確認の行の画面を保存する。"""
    client = None if rebuild_from_cache else genai.Client(api_key=api_key)
    duration = probe_duration(video)
    chunks = plan_chunks(duration)
    cache_dir = cache_dir_for(video)
    cache_dir.mkdir(parents=True, exist_ok=True)
    logger.info(f"動画の長さ: {format_time(duration)} / チャンク数: {len(chunks)} / キャッシュ: {cache_dir}")

    entries: list[Entry] = []
    for chunk in chunks:
        label = f"[{chunk.index + 1}/{len(chunks)}] {format_time(chunk.start)}〜{format_time(chunk.start + chunk.duration)}"
        json_path = cache_dir / f"chunk_{chunk.index:03d}.json"
        if json_path.exists():
            logger.info(f"{label} キャッシュを使用")
            result = json.loads(json_path.read_text(encoding="utf-8"))
        elif rebuild_from_cache:
            raise FileNotFoundError(f"{label} のキャッシュがありません。先に通常モードで解析してください: {json_path}")
        else:
            logger.info(f"{label} 分割中...")
            chunk_path = cache_dir / f"chunk_{chunk.index:03d}.mp4"
            if not chunk_path.exists():
                cut_clip(video, chunk.start, chunk.duration, chunk_path)
            logger.info(f"{label} Geminiで解析中...")
            result = ask_gemini(client, chunk_path, PROMPT, RESPONSE_SCHEMA)
            json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            chunk_path.unlink()
        raw_charms = result.get("charms") or []
        logger.info(f"{label} {len(raw_charms)}件検出")
        entries += [to_entry(raw, chunk) for raw in raw_charms]

    entries = review_entries(merge_consecutive(entries), make_rechecker(client, video, duration, cache_dir))
    write_csv(entries, output)
    dup_count = sum(e.duplicate for e in entries)
    flagged_count = sum(e.flagged for e in entries)
    logger.info(f"完了: {len(entries)}件を書き出しました（重複の可能性あり: {dup_count}件 / 要確認: {flagged_count}件） -> {output}")
    if frames_dir is not None:
        saved = save_flagged_frames(video, entries, frames_dir)
        logger.info(f"要確認の行の画面を{saved}枚保存しました -> {frames_dir}")
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description="モンハンワイルズの護石の装備詳細画面の動画からCSVを作成します。")
    parser.add_argument("input", type=Path, help="入力動画のパス")
    parser.add_argument("output", type=Path, help="出力CSVのパス")
    parser.add_argument("--rebuild-from-cache", action="store_true",
                        help="APIを呼ばずに、キャッシュ済みの応答からCSVを作り直す")
    parser.add_argument("--save-flagged-frames", action="store_true",
                        help="要確認の行の画面をPNGで保存する（出力CSVと同じ場所の <動画名>\\flagged\\）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if not args.input.is_file():
        print(f"入力動画が見つかりません: {args.input}", file=sys.stderr)
        return 1
    api_key = os.environ.get("GEMINI_API_KEY")
    problem = check_environment(api_key, need_api=not args.rebuild_from_cache)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    frames_dir = args.output.parent / args.input.stem / "flagged" if args.save_flagged_frames else None
    run(args.input, args.output, api_key, rebuild_from_cache=args.rebuild_from_cache, frames_dir=frames_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
