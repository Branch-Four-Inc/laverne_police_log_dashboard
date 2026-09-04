#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas"]
# ///
import pandas as pd
try:
    from rapidfuzz import process, fuzz
    USE_RAPIDFUZZ = True
except ImportError:
    import difflib
    USE_RAPIDFUZZ = False

def fuzzy_match(nature, choices, threshold=85):
    if pd.isna(nature):
        return None
    if USE_RAPIDFUZZ:
        match = process.extractOne(nature, choices, scorer=fuzz.WRatio)
        if match and match[1] >= threshold:
            return match[0]  # matched known nature string
        return None
    else:
        matches = difflib.get_close_matches(nature, choices, n=1, cutoff=threshold/100)
        return matches[0] if matches else None

# --------------------------------------------------------------------------------
# MATCH NATURE TO DEFINED GROUPED NATURE
# --------------------------------------------------------------------------------

desc = pd.read_excel("nature_descriptions.xlsx", index_col = 0)
desc = desc.rename(columns = {"Code": "nature", 
                       "Incident Type": "grouped_nature"})

# Build a dict mapping nature -> grouped_nature
desc_map = dict(zip(desc["nature"], desc["grouped_nature"]))


df = pd.read_csv("daily_logs.csv")
#df["grouped_nature"] = df["nature"].map(nature_to_group).fillna(df["nature"])
#df["grouped_nature"] = df["nature"].map(desc_map).fillna(df["nature"])
df["grouped_nature"] = df["nature"].map(desc_map)

df[~df['nature'].isin(desc_map)]


# --- Fuzzy matching for natures missing in legend fil/grouped nature 

known_natures = list(desc_map.keys())


# Identify rows where grouped_nature is still missing (no exact match)
unmatched_mask = df["grouped_nature"].isna()

# Build a cache so we don't re-run fuzzy matching on the same unmatched string repeatedly
unique_unmatched = df.loc[unmatched_mask, "nature"].unique()
fuzzy_cache = {}

for val in unique_unmatched:
    best_match = fuzzy_match(val, known_natures, threshold=85)
    fuzzy_cache[val] = desc_map[best_match] if best_match else None

# Apply the fuzzy-matched groupings
df.loc[unmatched_mask, "grouped_nature"] = df.loc[unmatched_mask, "nature"].map(fuzzy_cache)

# Anything still unmatched: put as "Other"
df["grouped_nature"] = df["grouped_nature"].fillna("Other")

# Inspect what remains unresolved by fuzzy matching (optional sanity check)
still_unmatched = df[df["grouped_nature"] == df["nature"]]
print(still_unmatched["nature"].unique())




#df2 = df.merge(desc[["nature", "grouped_nature"]], on ="nature", how = "left")
#df2['grouped_nature'] = df2['grouped_nature'].fillna(df2['nature'])
df.to_csv("daily_logs.csv", index=False)

print(df["grouped_nature"].value_counts().to_string())
print(f"\nUnmapped natures: {df[~df['nature'].isin(desc_map)]['nature'].dropna().unique().tolist()}")
