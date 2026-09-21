import ast
import sys
from pathlib import Path

import pandas as pd

EXP_DIR = Path("experiments/full_run_real_models")

# IFT
ift = pd.read_csv(EXP_DIR / "ift_2poj431skazt_2026-09-08_12-54-57_unified_results.csv", nrows=5)
print("=== IFT columns ===")
print(ift.columns.tolist())
print()

# EMA
ema = pd.read_csv(EXP_DIR / "ema_6m576vu02eud_2026-09-08_13-58-45_unified_results.csv", nrows=5)
print("=== EMA columns ===")
print(ema.columns.tolist())
print()

# IFT row 0
print("=== IFT row 0 ===")
for col in ift.columns:
    val = ift.iloc[0][col]
    if pd.isna(val) or val == "":
        print(f"  {col}: (empty/NaN)")
    else:
        print(f"  {col}: {str(val)[:200]}...")

print()

# EMA rows
for i in range(min(3, len(ema))):
    expected = ema.iloc[i]["expected_class"]
    actual = ema.iloc[i]["actual_class"]
    print(f"=== EMA row {i}: expected_class={expected}, actual_class={actual} ===")
    for col in ema.columns:
        val = ema.iloc[i][col]
        if pd.isna(val) or val == "":
            print(f"  {col}: (empty/NaN)")
        else:
            print(f"  {col}: {str(val)[:200]}...")
    print()
