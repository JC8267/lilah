#!/usr/bin/env python3
"""Normalize multi-tab survey workbook into a single long-form CSV.

This script reads an .xlsx file directly (zip+xml) so it does not require
openpyxl/pandas. It is designed for "one question per sheet" survey exports.
"""

from __future__ import annotations

import argparse
import csv
import re
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from pathlib import Path


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

COL_REF_RE = re.compile(r"([A-Z]+)(\d+)")


BASE_COLUMNS = {
    "Question ID",
    "Demo ID",
    "Demo Level",
    "Question Text",
    "Question Level",
    "Unweighted N",
    "Weighted N",
    "Unweighted Margin of Error",
    "Weighted Margin of Error",
}


def col_to_index(col_ref: str) -> int:
    value = 0
    for ch in col_ref:
        value = value * 26 + (ord(ch) - 64)
    return value - 1


def parse_numeric(value: str) -> float | None:
    if value is None:
        return None
    cleaned = value.strip()
    if cleaned == "":
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def load_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []

    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{{{NS_MAIN}}}si"):
        t = si.find(f"{{{NS_MAIN}}}t")
        if t is not None:
            out.append(t.text or "")
            continue
        pieces: list[str] = []
        for r in si.findall(f"{{{NS_MAIN}}}r"):
            tt = r.find(f"{{{NS_MAIN}}}t")
            if tt is not None and tt.text is not None:
                pieces.append(tt.text)
        out.append("".join(pieces))
    return out


def decode_cell(cell: ET.Element, shared: list[str]) -> str:
    cell_type = cell.attrib.get("t")
    v = cell.find(f"{{{NS_MAIN}}}v")
    inline = cell.find(f"{{{NS_MAIN}}}is")

    if cell_type == "s" and v is not None and v.text is not None:
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return v.text
    if cell_type == "inlineStr" and inline is not None:
        t = inline.find(f"{{{NS_MAIN}}}t")
        return (t.text or "") if t is not None else ""
    if v is not None and v.text is not None:
        return v.text
    return ""


def read_sheet_rows(ws_xml: bytes, shared: list[str]) -> list[list[str]]:
    root = ET.fromstring(ws_xml)
    sheet_data = root.find(f"{{{NS_MAIN}}}sheetData")
    if sheet_data is None:
        return []

    rows_out: list[list[str]] = []
    for row in sheet_data.findall(f"{{{NS_MAIN}}}row"):
        values_by_col: dict[int, str] = {}
        max_idx = -1

        for cell in row.findall(f"{{{NS_MAIN}}}c"):
            ref = cell.attrib.get("r", "")
            m = COL_REF_RE.fullmatch(ref)
            if not m:
                continue
            idx = col_to_index(m.group(1))
            if idx > max_idx:
                max_idx = idx
            values_by_col[idx] = decode_cell(cell, shared)

        if max_idx < 0:
            rows_out.append([])
            continue

        values = [""] * (max_idx + 1)
        for idx, value in values_by_col.items():
            values[idx] = value
        rows_out.append(values)

    return rows_out


def workbook_sheet_map(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    wb = ET.fromstring(zf.read("xl/workbook.xml"))
    rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    rid_to_target = {
        rel.attrib["Id"]: rel.attrib["Target"]
        for rel in rels.findall(f"{{{NS_PKG}}}Relationship")
    }

    sheet_map: list[tuple[str, str]] = []
    for sheet in wb.findall(f"{{{NS_MAIN}}}sheets/{{{NS_MAIN}}}sheet"):
        name = sheet.attrib.get("name", "")
        rid = sheet.attrib.get(f"{{{NS_REL}}}id", "")
        target = rid_to_target.get(rid, "")
        if not target:
            continue
        path = target if target.startswith("xl/") else f"xl/{target}"
        sheet_map.append((name, path))
    return sheet_map


def normalize_workbook(input_path: Path, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    out_long = outdir / "survey_long.csv"
    out_sheets = outdir / "sheet_summary.csv"

    long_fields = [
        "sheet_name",
        "sheet_row_number",
        "question_id",
        "question_group",
        "question_text",
        "question_level",
        "demo_id",
        "demo_level",
        "unweighted_n",
        "weighted_n",
        "unweighted_margin_of_error",
        "weighted_margin_of_error",
        "response_option",
        "response_value",
        "response_value_raw",
    ]
    summary_fields = [
        "sheet_name",
        "rows_in_sheet",
        "header_column_count",
        "response_option_count",
        "header_pattern",
    ]

    written_long_rows = 0
    written_sheet_rows = 0
    skipped_sheets: list[str] = []
    pattern_counts: Counter[str] = Counter()

    with zipfile.ZipFile(input_path) as zf, out_long.open(
        "w", newline="", encoding="utf-8"
    ) as long_fp, out_sheets.open("w", newline="", encoding="utf-8") as sheets_fp:
        shared = load_shared_strings(zf)
        sheets = workbook_sheet_map(zf)

        long_writer = csv.DictWriter(long_fp, fieldnames=long_fields)
        long_writer.writeheader()

        summary_writer = csv.DictWriter(sheets_fp, fieldnames=summary_fields)
        summary_writer.writeheader()

        for sheet_name, sheet_path in sheets:
            rows = read_sheet_rows(zf.read(sheet_path), shared)
            if len(rows) < 2:
                skipped_sheets.append(sheet_name)
                continue

            header = rows[0]
            if len(header) < 4 or header[:4] != [
                "Question ID",
                "Demo ID",
                "Demo Level",
                "Question Text",
            ]:
                skipped_sheets.append(sheet_name)
                continue

            response_columns = [
                col for col in header if col and col not in BASE_COLUMNS
            ]
            pattern = "|".join(header)
            pattern_counts[pattern] += 1

            summary_writer.writerow(
                {
                    "sheet_name": sheet_name,
                    "rows_in_sheet": max(0, len(rows) - 1),
                    "header_column_count": len(header),
                    "response_option_count": len(response_columns),
                    "header_pattern": pattern,
                }
            )
            written_sheet_rows += 1

            # Use the widest observed row width so missing tail columns are filled.
            max_cols = max(len(r) for r in rows)
            normalized_header = header + [""] * (max_cols - len(header))
            idx_by_name = {name: idx for idx, name in enumerate(normalized_header) if name}

            for row_idx, row in enumerate(rows[1:], start=2):
                if len(row) < max_cols:
                    row = row + [""] * (max_cols - len(row))

                question_id = row[idx_by_name.get("Question ID", -1)] if "Question ID" in idx_by_name else ""
                if question_id.strip() == "":
                    continue

                question_group = question_id.split("_", 1)[0]
                base = {
                    "sheet_name": sheet_name,
                    "sheet_row_number": row_idx,
                    "question_id": question_id,
                    "question_group": question_group,
                    "question_text": row[idx_by_name.get("Question Text", -1)] if "Question Text" in idx_by_name else "",
                    "question_level": row[idx_by_name.get("Question Level", -1)] if "Question Level" in idx_by_name else "",
                    "demo_id": row[idx_by_name.get("Demo ID", -1)] if "Demo ID" in idx_by_name else "",
                    "demo_level": row[idx_by_name.get("Demo Level", -1)] if "Demo Level" in idx_by_name else "",
                    "unweighted_n": row[idx_by_name.get("Unweighted N", -1)] if "Unweighted N" in idx_by_name else "",
                    "weighted_n": row[idx_by_name.get("Weighted N", -1)] if "Weighted N" in idx_by_name else "",
                    "unweighted_margin_of_error": row[idx_by_name.get("Unweighted Margin of Error", -1)] if "Unweighted Margin of Error" in idx_by_name else "",
                    "weighted_margin_of_error": row[idx_by_name.get("Weighted Margin of Error", -1)] if "Weighted Margin of Error" in idx_by_name else "",
                }

                for response_name in response_columns:
                    idx = idx_by_name.get(response_name)
                    if idx is None:
                        continue
                    raw = row[idx]
                    if raw == "":
                        continue
                    long_writer.writerow(
                        {
                            **base,
                            "response_option": response_name,
                            "response_value": parse_numeric(raw),
                            "response_value_raw": raw,
                        }
                    )
                    written_long_rows += 1

    print(f"Input workbook: {input_path}")
    print(f"Output long CSV: {out_long}")
    print(f"Output sheet summary: {out_sheets}")
    print(f"Question sheets normalized: {written_sheet_rows}")
    print(f"Rows written (long format): {written_long_rows}")
    print(f"Skipped sheets: {', '.join(skipped_sheets) if skipped_sheets else 'None'}")
    print(f"Unique header patterns (question sheets): {len(pattern_counts)}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize multi-tab survey workbook into long-form CSV."
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Path to input .xlsx workbook.",
    )
    parser.add_argument(
        "--outdir",
        required=True,
        help="Directory for normalized outputs.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    normalize_workbook(Path(args.input), Path(args.outdir))


if __name__ == "__main__":
    main()
