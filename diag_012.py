"""Throwaway: show exactly what the classifier sees for one PDF."""

import sys
from pathlib import Path

sys.path.insert(0, "src")

import classifier
from config_loader import load_config
from ocr import extract_text

pdf = Path(sys.argv[1])
text, source = extract_text(pdf.read_bytes(), pdf.name)
print(f"source={source} chars={len(text)} raw_lines={len(text.splitlines())}\n")

lines = classifier.document_lines(text)
print(f"--- {len(lines)} normalized lines ---")
for index, line in enumerate(lines):
    print(f"{index:3} {line!r}")

rules = classifier.load_rules(load_config()["paths"]["excel"])
print("\n--- rules that contain 'demand' or 'notice' ---")
for rule in rules:
    if "demand" in rule.normalized or "notice" in rule.normalized:
        hits = [l for l in lines if l.startswith(rule.normalized)]
        anywhere = [l for l in lines if rule.normalized in l]
        print(f"row {rule.row} {rule.phrase!r} startswith={len(hits)} anywhere={len(anywhere)}")
        for l in anywhere[:3]:
            print(f"      in: {l!r}")