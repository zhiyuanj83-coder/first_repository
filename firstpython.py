#!/usr/bin/env python3
"""
Warcraft Logs 自动抓取与对比工具

功能：
1. 抓取指定 Report + Fight 的施法明细（casts）与伤害占比（damage done）并导出 CSV。
2. 自动根据该 Fight 的 Boss、职业/专精、战斗时长（±20s）去搜索高分日志。
3. 抓取匹配到的高分日志的施法与伤害占比 CSV。
4. 自动生成对比表（每个角色一份 casts 对比 + damage 对比）。

说明：
- 采用 Warcraft Logs v1 API（JSON），本脚本会本地转换为 CSV。
- 需要先在 Warcraft Logs 获取 API Key。
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE = "https://www.warcraftlogs.com/v1"


class WCLClient:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def get(self, path: str, **params: Any) -> Any:
        params["api_key"] = self.api_key
        query = urlencode(params)
        url = f"{API_BASE}{path}?{query}"
        req = Request(url, method="GET")
        with urlopen(req, timeout=60) as resp:
            payload = resp.read().decode("utf-8")
        return json.loads(payload)

    def get_report_fights(self, report_code: str) -> dict[str, Any]:
        return self.get(f"/report/fights/{report_code}")

    def get_casts_table(self, report_code: str, start: int, end: int, sourceid: int) -> dict[str, Any]:
        return self.get(
            f"/report/tables/casts/{report_code}",
            start=start,
            end=end,
            sourceid=sourceid,
        )

    def get_damage_done_table(self, report_code: str, start: int, end: int, sourceid: int) -> dict[str, Any]:
        return self.get(
            f"/report/tables/damage-done/{report_code}",
            start=start,
            end=end,
            sourceid=sourceid,
        )

    def get_encounter_rankings(self, encounter_id: int, metric: str = "dps", difficulty: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"metric": metric}
        if difficulty is not None:
            params["difficulty"] = difficulty
        return self.get(f"/rankings/encounter/{encounter_id}", **params)


@dataclasses.dataclass
class Fighter:
    id: int
    name: str
    klass: str
    spec: str


@dataclasses.dataclass
class FightInfo:
    id: int
    encounter_id: int
    name: str
    start_time: int
    end_time: int
    difficulty: int

    @property
    def duration_sec(self) -> float:
        return (self.end_time - self.start_time) / 1000.0


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def dump_json(path: Path, payload: Any) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def flatten_entries_for_csv(entries: list[dict[str, Any]]) -> tuple[list[str], list[dict[str, Any]]]:
    columns: set[str] = set()
    rows: list[dict[str, Any]] = []

    for e in entries:
        row: dict[str, Any] = {}
        for k, v in e.items():
            if isinstance(v, (str, int, float, bool)) or v is None:
                row[k] = v
                columns.add(k)
            else:
                row[k] = json.dumps(v, ensure_ascii=False)
                columns.add(k)
        rows.append(row)

    ordered = sorted(columns)
    return ordered, rows


def write_entries_csv(path: Path, entries: list[dict[str, Any]]) -> None:
    cols, rows = flatten_entries_for_csv(entries)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)


def parse_fight(report_fights: dict[str, Any], fight_id: int) -> FightInfo:
    fights = report_fights.get("fights", [])
    fight = next((f for f in fights if f.get("id") == fight_id), None)
    if not fight:
        raise ValueError(f"在 report 中未找到 fight_id={fight_id}。")

    return FightInfo(
        id=fight["id"],
        encounter_id=fight.get("boss", 0),
        name=fight.get("name", "Unknown"),
        start_time=fight["start_time"],
        end_time=fight["end_time"],
        difficulty=fight.get("difficulty", 0),
    )


def parse_friendlies(report_fights: dict[str, Any]) -> list[Fighter]:
    friendlies = report_fights.get("friendlies", [])
    out: list[Fighter] = []
    for f in friendlies:
        if f.get("type") != "Player":
            continue
        out.append(
            Fighter(
                id=f.get("id", 0),
                name=f.get("name", "Unknown"),
                klass=f.get("class", "Unknown"),
                spec=f.get("specs", ["Unknown"])[0] if f.get("specs") else "Unknown",
            )
        )
    return out


def match_rankings(target: FightInfo, fighter: Fighter, rankings: list[dict[str, Any]], tolerance_sec: int) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    for r in rankings:
        cls = r.get("class") or r.get("spec", {}).get("class")
        spec = r.get("spec") if isinstance(r.get("spec"), str) else r.get("spec", {}).get("spec")
        duration = r.get("duration") or r.get("fightDuration")

        if cls and str(cls).lower() != fighter.klass.lower():
            continue
        if spec and str(spec).lower() != fighter.spec.lower():
            continue
        if duration is None:
            continue

        dur_sec = float(duration) / 1000.0 if float(duration) > 500 else float(duration)
        if abs(dur_sec - target.duration_sec) > tolerance_sec:
            continue

        matched.append(r)

    matched.sort(key=lambda x: x.get("rankPercent", x.get("percentile", 0)), reverse=True)
    return matched


def extract_rank_report_and_fight(rank_row: dict[str, Any]) -> tuple[str | None, int | None]:
    report_code = rank_row.get("reportID") or rank_row.get("reportCode")
    fight_id = rank_row.get("fightID") or rank_row.get("fight")

    if report_code is None and "report" in rank_row and isinstance(rank_row["report"], dict):
        report_code = rank_row["report"].get("code")
    if fight_id is None and "fight" in rank_row and isinstance(rank_row["fight"], dict):
        fight_id = rank_row["fight"].get("id")

    return report_code, int(fight_id) if fight_id is not None else None


def compare_sum(entries: list[dict[str, Any]], key_name: str, value_name: str) -> dict[str, float]:
    result: dict[str, float] = {}
    for e in entries:
        k = str(e.get(key_name, "Unknown"))
        v = float(e.get(value_name, 0) or 0)
        result[k] = result.get(k, 0.0) + v
    return result


def write_comparison_csv(path: Path, base_map: dict[str, float], ref_map: dict[str, float], base_col: str, ref_col: str) -> None:
    keys = sorted(set(base_map) | set(ref_map))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["metric", base_col, ref_col, "diff", "diff_pct"])
        writer.writeheader()
        for k in keys:
            b = base_map.get(k, 0.0)
            r = ref_map.get(k, 0.0)
            diff = b - r
            diff_pct = (diff / r * 100.0) if r else 0.0
            writer.writerow({"metric": k, base_col: b, ref_col: r, "diff": diff, "diff_pct": diff_pct})


def main() -> None:
    parser = argparse.ArgumentParser(description="抓取 Warcraft Logs CSV 并生成对比表。")
    parser.add_argument("--api-key", default=os.getenv("WCL_API_KEY"), help="Warcraft Logs API Key")
    parser.add_argument("--report-code", required=True, help="目标 report code")
    parser.add_argument("--fight-id", required=True, type=int, help="目标 fight id")
    parser.add_argument("--top-n", type=int, default=1, help="每个职业/专精匹配的高分日志数量")
    parser.add_argument("--tolerance-sec", type=int, default=20, help="战斗时长误差秒数")
    parser.add_argument("--output-dir", default="output", help="输出目录")
    parser.add_argument("--sleep", type=float, default=0.2, help="请求间隔，降低限流风险")
    args = parser.parse_args()

    if not args.api_key:
        raise SystemExit("请通过 --api-key 或环境变量 WCL_API_KEY 提供 API Key")

    client = WCLClient(args.api_key)
    out_root = Path(args.output_dir)
    ensure_dir(out_root)

    base_report = client.get_report_fights(args.report_code)
    base_fight = parse_fight(base_report, args.fight_id)
    fighters = parse_friendlies(base_report)

    metadata_path = out_root / "base_report_fights.json"
    dump_json(metadata_path, base_report)

    base_dir = out_root / f"base_{args.report_code}_fight_{args.fight_id}"
    ensure_dir(base_dir)

    print(f"[INFO] 目标战斗: {base_fight.name} (encounter={base_fight.encounter_id}, duration={base_fight.duration_sec:.1f}s)")
    rankings = client.get_encounter_rankings(base_fight.encounter_id, difficulty=base_fight.difficulty)

    for fighter in fighters:
        print(f"[INFO] 处理角色: {fighter.name} ({fighter.klass}/{fighter.spec})")
        role_dir = base_dir / f"{fighter.name}_{fighter.id}_{fighter.klass}_{fighter.spec}"
        ensure_dir(role_dir)

        base_casts = client.get_casts_table(args.report_code, base_fight.start_time, base_fight.end_time, fighter.id)
        base_damage = client.get_damage_done_table(args.report_code, base_fight.start_time, base_fight.end_time, fighter.id)

        base_casts_entries = base_casts.get("entries", [])
        base_damage_entries = base_damage.get("entries", [])

        write_entries_csv(role_dir / "base_casts.csv", base_casts_entries)
        write_entries_csv(role_dir / "base_damage_share.csv", base_damage_entries)

        matches = match_rankings(base_fight, fighter, rankings, args.tolerance_sec)[: args.top_n]
        if not matches:
            print("  [WARN] 未找到匹配高分日志")
            continue

        for idx, m in enumerate(matches, start=1):
            report_code, fight_id = extract_rank_report_and_fight(m)
            if not report_code or not fight_id:
                print("  [WARN] ranking 行缺少 report/fight 信息，跳过")
                continue

            try:
                ref_fights = client.get_report_fights(report_code)
                ref_fight = parse_fight(ref_fights, fight_id)
            except Exception as exc:
                print(f"  [WARN] 读取匹配 report/fight 失败: {exc}")
                continue

            ref_dir = role_dir / f"rank_{idx}_{report_code}_fight_{fight_id}"
            ensure_dir(ref_dir)
            dump_json(ref_dir / "ranking_row.json", m)

            # 同名优先匹配，找不到则使用同职业第一个
            ref_friendlies = parse_friendlies(ref_fights)
            ref_source = next((rf for rf in ref_friendlies if rf.name == fighter.name and rf.klass == fighter.klass), None)
            if ref_source is None:
                ref_source = next((rf for rf in ref_friendlies if rf.klass == fighter.klass and rf.spec == fighter.spec), None)
            if ref_source is None:
                print("  [WARN] 匹配日志中无同职业/专精玩家，跳过")
                continue

            ref_casts = client.get_casts_table(report_code, ref_fight.start_time, ref_fight.end_time, ref_source.id)
            ref_damage = client.get_damage_done_table(report_code, ref_fight.start_time, ref_fight.end_time, ref_source.id)

            ref_casts_entries = ref_casts.get("entries", [])
            ref_damage_entries = ref_damage.get("entries", [])

            write_entries_csv(ref_dir / "ref_casts.csv", ref_casts_entries)
            write_entries_csv(ref_dir / "ref_damage_share.csv", ref_damage_entries)

            # 生成对比表
            base_casts_map = compare_sum(base_casts_entries, "name", "total")
            ref_casts_map = compare_sum(ref_casts_entries, "name", "total")
            write_comparison_csv(
                ref_dir / "compare_casts.csv",
                base_casts_map,
                ref_casts_map,
                f"base_{fighter.name}",
                f"ref_{ref_source.name}",
            )

            base_damage_map = compare_sum(base_damage_entries, "name", "total")
            ref_damage_map = compare_sum(ref_damage_entries, "name", "total")
            write_comparison_csv(
                ref_dir / "compare_damage_share.csv",
                base_damage_map,
                ref_damage_map,
                f"base_{fighter.name}",
                f"ref_{ref_source.name}",
            )

            print(f"  [OK] 已生成对比: {ref_dir}")
            time.sleep(args.sleep)

    print(f"[DONE] 输出目录: {out_root.resolve()}")


if __name__ == "__main__":
    main()
